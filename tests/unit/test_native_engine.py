import json
from pathlib import Path

import pytest

from pe_review_agent.config import ReviewSettings
from pe_review_agent.domain import ChangedLine, ReviewContext
from pe_review_agent.llm.client import LlmCompletion, ToolCall
from pe_review_agent.repos.tools import RepositoryToolExecutor
from pe_review_agent.review.native import NativeFirmwareReviewEngine


class FakeLlm:
    def __init__(self, completions: list[LlmCompletion]) -> None:
        self.completions = list(completions)
        self.settings = type("Settings", (), {"model": "Qwen3.6-27B"})()
        self.seen_messages: list[list[dict]] = []

    async def complete(self, *, messages, tools=None, **kwargs):
        self.seen_messages.append(list(messages))
        return self.completions.pop(0)


def _completion(content: str, *calls: ToolCall) -> LlmCompletion:
    return LlmCompletion(
        content=content,
        tool_calls=tuple(calls),
        input_tokens=10,
        output_tokens=5,
        finish_reason="stop" if not calls else "tool_calls",
        raw_message={},
    )


def _review_json(confidence: float = 0.96) -> str:
    return json.dumps(
        {
            "summary": "One timeout bug found.",
            "findings": [
                {
                    "severity": "P1",
                    "category": "timeout",
                    "title": "Timeout result is ignored",
                    "message": "A timeout still advances the training state.",
                    "impact": "The next stage consumes stale data.",
                    "evidence": "poll_done() returns -ETIMEDOUT but its result is discarded.",
                    "remediation": "Handle or propagate the timeout.",
                    "location": {
                        "path": "fw/train.c",
                        "start_line": 2,
                        "start_character": 0,
                    },
                    "confidence": confidence,
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_two_pass_engine_uses_tools_then_verifies(tmp_path: Path) -> None:
    target = tmp_path / "fw" / "train.c"
    target.parent.mkdir(parents=True)
    target.write_text("int rc = poll_done();\nadvance();\n", encoding="utf-8")
    tool_call = ToolCall(
        id="call-1",
        name="read_file",
        arguments={"path": "fw/train.c"},
        raw_arguments='{"path":"fw/train.c"}',
    )
    llm = FakeLlm(
        [
            _completion("", tool_call),
            _completion(_review_json()),
            _completion(_review_json()),
        ]
    )
    settings = ReviewSettings(min_confidence=0.82)
    engine = NativeFirmwareReviewEngine(llm, settings)  # type: ignore[arg-type]
    context = ReviewContext(
        project="soc/fw",
        change_number=11,
        patchset_number=3,
        revision_sha="a" * 40,
        diff="+advance();",
        changed_files=["fw/train.c"],
        changed_lines=[ChangedLine(path="fw/train.c", line=2, text="advance();")],
        policy_text="firmware policy",
        repository_root=str(tmp_path),
    )
    tools = RepositoryToolExecutor(tmp_path, settings)

    result = await engine.review(context, tools)

    assert len(result.findings) == 1
    assert result.findings[0].location.start_line == 2
    assert result.review_metadata["candidate_count"] == 1
    assert result.review_metadata["verified_model_count"] == 1
    # Tool result is present in the second candidate request.
    assert any(message.get("role") == "tool" for message in llm.seen_messages[1])
    assert result.input_tokens == 30
    assert result.output_tokens == 15


@pytest.mark.asyncio
async def test_verifier_can_drop_candidate(tmp_path: Path) -> None:
    target = tmp_path / "fw.c"
    target.write_text("changed();\n", encoding="utf-8")
    candidate = json.dumps(
        {
            "summary": "candidate",
            "findings": [
                {
                    "severity": "P2",
                    "category": "style",
                    "title": "Rename variable",
                    "message": "style",
                    "impact": "none",
                    "evidence": "name",
                    "location": {"path": "fw.c", "start_line": 1},
                    "confidence": 0.9,
                }
            ],
        }
    )
    verified = json.dumps({"summary": "", "findings": []})
    llm = FakeLlm([_completion(candidate), _completion(verified)])
    settings = ReviewSettings()
    engine = NativeFirmwareReviewEngine(llm, settings)  # type: ignore[arg-type]
    context = ReviewContext(
        project="soc/fw",
        change_number=1,
        patchset_number=1,
        revision_sha="b" * 40,
        diff="+changed();",
        changed_files=["fw.c"],
        changed_lines=[ChangedLine(path="fw.c", line=1, text="changed();")],
        policy_text="policy",
        repository_root=str(tmp_path),
    )

    result = await engine.review(context, RepositoryToolExecutor(tmp_path, settings))

    assert result.findings == []
    assert result.summary.startswith("No actionable")
