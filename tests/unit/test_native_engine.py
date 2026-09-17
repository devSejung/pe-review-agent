import json
from pathlib import Path

import pytest

from pe_review_agent.config import ReviewSettings
from pe_review_agent.domain import ChangedLine, ReviewContext, ReviewResult
from pe_review_agent.llm.client import LlmCompletion, ToolCall
from pe_review_agent.repos.tools import RepositoryToolExecutor
from pe_review_agent.retry import ContextLengthError
from pe_review_agent.review.native import NativeFirmwareReviewEngine


class FakeLlm:
    def __init__(self, completions: list[LlmCompletion | Exception]) -> None:
        self.completions = list(completions)
        self.settings = type("Settings", (), {"model": "Qwen3.6-27B"})()
        self.seen_messages: list[list[dict]] = []

    async def complete(self, *, messages, tools=None, **kwargs):
        self.seen_messages.append(list(messages))
        result = self.completions.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


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
        changed_files=["fw/train.c", *(f"unrelated/{index}.c" for index in range(2_000))],
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
    assert "unrelated/1999.c" not in llm.seen_messages[0][1]["content"]
    assert "natural Korean" in llm.seen_messages[0][1]["content"]
    assert "natural Korean" in llm.seen_messages[2][1]["content"]
    assert result.input_tokens == 30
    assert result.output_tokens == 15


@pytest.mark.asyncio
async def test_review_language_can_be_switched_to_english(tmp_path: Path) -> None:
    target = tmp_path / "fw.c"
    target.write_text("changed();\n", encoding="utf-8")
    llm = FakeLlm([_completion(json.dumps({"summary": "", "findings": []}))])
    settings = ReviewSettings(output_language="en-US")
    engine = NativeFirmwareReviewEngine(llm, settings)  # type: ignore[arg-type]
    context = ReviewContext(
        project="soc/fw",
        change_number=12,
        patchset_number=1,
        revision_sha="9" * 40,
        diff="+changed();",
        changed_files=["fw.c"],
        changed_lines=[ChangedLine(path="fw.c", line=1, text="changed();")],
        policy_text="policy",
        repository_root=str(tmp_path),
    )

    await engine.review(context, RepositoryToolExecutor(tmp_path, settings))

    assert "clear English" in llm.seen_messages[0][1]["content"]
    assert "natural Korean" not in llm.seen_messages[0][1]["content"]


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


@pytest.mark.asyncio
async def test_context_overflow_splits_diff_instead_of_retrying_same_prompt(tmp_path: Path) -> None:
    target = tmp_path / "fw" / "train.c"
    target.parent.mkdir(parents=True)
    target.write_text("int rc;\nadvance();\n", encoding="utf-8")
    diff = (
        "diff --git a/fw/train.c b/fw/train.c\n"
        "--- a/fw/train.c\n"
        "+++ b/fw/train.c\n"
        "@@ -1 +1,600 @@\n" + "+advance(); /* changed */\n" * 600
    )
    llm = FakeLlm(
        [
            ContextLengthError("maximum context length"),
            _completion(_review_json()),
            _completion(_review_json()),
            _completion(_review_json()),
        ]
    )
    settings = ReviewSettings(max_diff_chunk_chars=20_000)
    engine = NativeFirmwareReviewEngine(llm, settings)  # type: ignore[arg-type]
    context = ReviewContext(
        project="soc/fw",
        change_number=7,
        patchset_number=1,
        revision_sha="c" * 40,
        diff=diff,
        changed_files=["fw/train.c"],
        changed_lines=[ChangedLine(path="fw/train.c", line=2, text="advance();")],
        policy_text="policy",
        repository_root=str(tmp_path),
    )

    result = await engine.review(context, RepositoryToolExecutor(tmp_path, settings))

    assert len(result.findings) == 1
    assert result.review_metadata["initial_diff_chunks"] == 1
    assert result.review_metadata["diff_chunks"] == 2
    assert len(llm.seen_messages) == 4
    first_prompt = llm.seen_messages[0][1]["content"]
    retry_prompt = llm.seen_messages[1][1]["content"]
    assert len(retry_prompt) < len(first_prompt)


@pytest.mark.asyncio
async def test_no_reviewable_text_skips_llm_entirely(tmp_path: Path) -> None:
    llm = FakeLlm([])
    settings = ReviewSettings()
    engine = NativeFirmwareReviewEngine(llm, settings)  # type: ignore[arg-type]
    context = ReviewContext(
        project="soc/fw",
        change_number=8,
        patchset_number=1,
        revision_sha="d" * 40,
        diff="",
        changed_files=[],
        changed_lines=[],
        policy_text="policy",
        repository_root=str(tmp_path),
    )

    result = await engine.review(context, RepositoryToolExecutor(tmp_path, settings))

    assert result.findings == []
    assert result.input_tokens == 0
    assert llm.seen_messages == []


@pytest.mark.asyncio
async def test_explicit_merge_skip_publishes_summary_without_llm(tmp_path: Path) -> None:
    llm = FakeLlm([])
    settings = ReviewSettings()
    engine = NativeFirmwareReviewEngine(llm, settings)  # type: ignore[arg-type]
    context = ReviewContext(
        project="soc/fw",
        change_number=8,
        patchset_number=2,
        revision_sha="f" * 40,
        diff="",
        changed_files=[],
        changed_lines=[],
        policy_text="policy",
        repository_root=str(tmp_path),
        skip_reason="Automated review skipped for this merge commit.",
    )

    result = await engine.review(context, RepositoryToolExecutor(tmp_path, settings))

    assert result.summary == "Automated review skipped for this merge commit."
    assert result.review_metadata["skipped_reason"] == result.summary
    assert llm.seen_messages == []


@pytest.mark.asyncio
async def test_previous_finding_is_forced_through_verifier_when_candidate_pass_misses_it(
    tmp_path: Path,
) -> None:
    target = tmp_path / "fw" / "train.c"
    target.parent.mkdir(parents=True)
    target.write_text("int rc = poll_done();\nadvance();\n", encoding="utf-8")
    semantic_id = "d" * 32
    previous = json.loads(_review_json())
    previous["findings"][0]["semantic_id"] = semantic_id
    previous_finding = ReviewResult.model_validate(previous).findings[0]
    missed = json.dumps({"summary": "No candidates", "findings": []})
    verified = _review_json()
    verified_payload = json.loads(verified)
    verified_payload["findings"][0]["semantic_id"] = semantic_id
    llm = FakeLlm([_completion(missed), _completion(json.dumps(verified_payload))])
    settings = ReviewSettings()
    engine = NativeFirmwareReviewEngine(llm, settings)  # type: ignore[arg-type]
    context = ReviewContext(
        project="soc/fw",
        change_number=9,
        patchset_number=2,
        revision_sha="e" * 40,
        diff="+advance();",
        changed_files=["fw/train.c"],
        changed_lines=[ChangedLine(path="fw/train.c", line=2, text="advance();")],
        policy_text="policy",
        repository_root=str(tmp_path),
        previous_findings=[previous_finding],
        previous_patchset_number=1,
    )

    result = await engine.review(context, RepositoryToolExecutor(tmp_path, settings))

    assert len(result.findings) == 1
    assert result.findings[0].semantic_id == semantic_id
    verifier_prompt = llm.seen_messages[1][1]["content"]
    assert semantic_id in verifier_prompt
