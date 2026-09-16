import json

import httpx
import pytest

from pe_review_agent.config import LlmSettings
from pe_review_agent.llm.client import LlmClient
from pe_review_agent.retry import TransientError


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


@pytest.mark.asyncio
async def test_llm_client_classifies_429_as_transient() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="busy", headers={"Retry-After": "7"})

    client = LlmClient(
        LlmSettings(base_url="https://llm.example/v1"), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(TransientError) as error:
            await client.complete(messages=[{"role": "user", "content": "review"}])
    finally:
        await client.aclose()

    assert error.value.retry_after_seconds == 7
