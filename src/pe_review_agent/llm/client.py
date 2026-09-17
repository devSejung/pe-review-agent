from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import httpx

from pe_review_agent.config import LlmSettings
from pe_review_agent.observability.metrics import METRICS
from pe_review_agent.retry import ContextLengthError, PermanentError, TransientError


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str


@dataclass(frozen=True, slots=True)
class LlmCompletion:
    content: str
    tool_calls: tuple[ToolCall, ...]
    input_tokens: int | None
    output_tokens: int | None
    finish_reason: str | None
    raw_message: dict[str, Any]


class LlmClient:
    """Minimal OpenAI-compatible chat client with explicit failure classification."""

    def __init__(
        self, settings: LlmSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        timeout = httpx.Timeout(
            settings.request_timeout_seconds,
            connect=settings.connect_timeout_seconds,
        )
        headers = {"Content-Type": "application/json"}
        secret = settings.api_key
        if secret:
            headers["Authorization"] = f"Bearer {secret.get_secret_value()}"
        self._client = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/") + "/",
            headers=headers,
            timeout=timeout,
            transport=transport,
            verify=str(settings.ca_bundle_path) if settings.ca_bundle_path else True,
        )
        self._semaphore = asyncio.Semaphore(settings.concurrency)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def check_connection(self) -> tuple[str, ...]:
        """Verify the OpenAI-compatible endpoint without spending generation tokens."""

        try:
            response = await self._client.get("models")
        except (
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
            httpx.RemoteProtocolError,
            httpx.HTTPError,
        ) as exc:
            message = f"LLM connection check failed: {type(exc).__name__}: {exc}"
            raise TransientError(message) from exc
        if response.status_code >= 400:
            self._raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise TransientError("LLM /models endpoint returned invalid JSON") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise TransientError(
                "LLM /models endpoint returned an invalid OpenAI-compatible payload"
            )
        models = []
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                models.append(item["id"])
        return tuple(models)

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LlmCompletion:
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.settings.max_output_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        async with self._semaphore:
            started = perf_counter()
            try:
                response = await self._client.post("chat/completions", json=payload)
            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.ConnectTimeout,
                httpx.RemoteProtocolError,
            ) as exc:
                raise TransientError(f"LLM transport failure: {type(exc).__name__}: {exc}") from exc
            except httpx.HTTPError as exc:
                raise TransientError(f"LLM HTTP transport failure: {exc}") from exc
            finally:
                METRICS.llm_latency_seconds.observe(perf_counter() - started)

        if response.status_code >= 400:
            self._raise_for_status(response)
        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise TransientError(
                f"LLM returned an invalid OpenAI-compatible response: {response.text[:1000]}"
            ) from exc

        calls: list[ToolCall] = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            raw_arguments = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                detail = f"{function.get('name')}: {raw_arguments[:500]}"
                raise TransientError(f"LLM emitted invalid tool arguments for {detail}") from exc
            if not isinstance(arguments, dict):
                raise TransientError("LLM tool arguments must decode to an object")
            calls.append(
                ToolCall(
                    id=str(call.get("id") or ""),
                    name=str(function.get("name") or ""),
                    arguments=arguments,
                    raw_arguments=raw_arguments,
                )
            )
        usage = body.get("usage") or {}
        return LlmCompletion(
            content=str(message.get("content") or ""),
            tool_calls=tuple(calls),
            input_tokens=_int_or_none(usage.get("prompt_tokens") or usage.get("input_tokens")),
            output_tokens=_int_or_none(
                usage.get("completion_tokens") or usage.get("output_tokens")
            ),
            finish_reason=choice.get("finish_reason"),
            raw_message=message,
        )

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        detail = response.text[:2000]
        retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
        message = f"LLM HTTP {status}: {detail}"
        if status in (400, 413, 422) and _looks_like_context_length_error(detail):
            raise ContextLengthError(message)
        if status == 429 or 500 <= status <= 599:
            raise TransientError(message, retry_after_seconds=retry_after)
        if status in (408, 409, 425):
            raise TransientError(message, retry_after_seconds=retry_after)
        if status in (400, 401, 403, 404, 422):
            raise PermanentError(message)
        raise PermanentError(message)


def assistant_message_for_tool_loop(completion: LlmCompletion) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": completion.content or None}
    if completion.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.raw_arguments},
            }
            for call in completion.tool_calls
        ]
    return message


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _looks_like_context_length_error(detail: str) -> bool:
    normalized = detail.lower()
    return any(
        marker in normalized
        for marker in (
            "context length",
            "context_length",
            "maximum context",
            "max_model_len",
            "too many tokens",
            "prompt is too long",
            "maximum sequence length",
        )
    )
