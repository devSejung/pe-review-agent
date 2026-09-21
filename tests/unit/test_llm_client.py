import asyncio
import json

import httpx
import pytest

from pe_review_agent.config import LlmSettings
from pe_review_agent.llm.client import LlmClient, assistant_message_for_tool_loop
from pe_review_agent.retry import ContextLengthError, ProviderUnavailableError


@pytest.mark.asyncio
async def test_llm_client_parses_tool_call_and_usage() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == "Qwen3.6-27B"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning": "I need to inspect fw.c before concluding.",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"fw.c"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            },
        )

    client = LlmClient(
        LlmSettings(base_url="https://llm.example/v1"), transport=httpx.MockTransport(handler)
    )
    try:
        result = await client.complete(messages=[{"role": "user", "content": "review"}])
    finally:
        await client.aclose()

    assert result.tool_calls[0].name == "read_file"
    assert result.tool_calls[0].arguments == {"path": "fw.c"}
    assert result.input_tokens == 10
    assert result.output_tokens == 4
    assert assistant_message_for_tool_loop(result)["reasoning"] == (
        "I need to inspect fw.c before concluding."
    )


@pytest.mark.asyncio
async def test_llm_client_classifies_429_as_provider_unavailable() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            text="busy; Try again in 5 seconds",
            headers={"Retry-After": "7"},
        )

    client = LlmClient(
        LlmSettings(base_url="https://llm.example/v1"), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ProviderUnavailableError) as error:
            await client.complete(messages=[{"role": "user", "content": "review"}])
    finally:
        await client.aclose()

    assert error.value.retry_after_seconds == 7


@pytest.mark.asyncio
async def test_llm_client_uses_provider_body_retry_hint_when_header_is_missing() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "error": {
                    "message": (
                        "No deployments available for selected model, Try again in 5 seconds."
                    )
                }
            },
        )

    client = LlmClient(
        LlmSettings(base_url="https://llm.example/v1"), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ProviderUnavailableError) as error:
            await client.complete(messages=[{"role": "user", "content": "review"}])
    finally:
        await client.aclose()

    assert error.value.retry_after_seconds == 5


@pytest.mark.parametrize("status_code", [500, 503])
@pytest.mark.asyncio
async def test_llm_client_classifies_5xx_as_provider_unavailable(status_code: int) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="temporary inference deployment failure")

    client = LlmClient(
        LlmSettings(base_url="https://llm.example/v1"), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ProviderUnavailableError):
            await client.complete(messages=[{"role": "user", "content": "review"}])
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_llm_client_classifies_transport_failure_as_provider_unavailable() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("deployment gateway unavailable", request=request)

    client = LlmClient(
        LlmSettings(base_url="https://llm.example/v1"), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ProviderUnavailableError, match="transport failure"):
            await client.complete(messages=[{"role": "user", "content": "review"}])
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_llm_client_classifies_context_overflow_for_adaptive_chunking() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "prompt exceeds maximum context length"}},
        )

    client = LlmClient(
        LlmSettings(base_url="https://llm.example/v1"), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ContextLengthError):
            await client.complete(messages=[{"role": "user", "content": "review"}])
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_llm_client_enforces_configured_concurrency() -> None:
    active = 0
    peak = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}
                ]
            },
        )

    client = LlmClient(
        LlmSettings(base_url="https://llm.example/v1", concurrency=1),
        transport=httpx.MockTransport(handler),
    )
    try:
        await asyncio.gather(
            *(client.complete(messages=[{"role": "user", "content": "review"}]) for _ in range(3))
        )
    finally:
        await client.aclose()

    assert peak == 1
