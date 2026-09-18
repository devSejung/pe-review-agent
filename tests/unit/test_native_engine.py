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
        self.seen_tools: list[list[dict] | None] = []

    async def complete(self, *, messages, tools=None, **kwargs):
        self.seen_messages.append(list(messages))
        self.seen_tools.append(tools)
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
            "summary": "legacy test summary",
            "change_summary": "- training timeout handling을 변경합니다.",
            "review_summary": "Timeout handling defect가 확인되었습니다.",
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
        subject="Handle training timeout",
        branch="main",
        commit_message="Handle training timeout\n\nPropagate poll_done() failures.",
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
    assert '"subject": "Handle training timeout"' in llm.seen_messages[0][1]["content"]
    assert '"branch": "main"' in llm.seen_messages[0][1]["content"]
    assert "Propagate poll_done() failures." in llm.seen_messages[0][1]["content"]
    assert "변경 요약" in result.summary
    assert "training timeout handling을 변경합니다." in result.summary
    assert "리뷰 결과" in result.summary
    assert result.input_tokens == 30
    assert result.output_tokens == 15


@pytest.mark.asyncio
async def test_tool_round_limit_forces_final_json_instead_of_failing(tmp_path: Path) -> None:
    target = tmp_path / "fw.c"
    target.write_text("changed();\n", encoding="utf-8")
    first = ToolCall(
        id="call-1",
        name="read_file",
        arguments={"path": "fw.c"},
        raw_arguments='{"path":"fw.c"}',
    )
    second = ToolCall(
        id="call-2",
        name="search_text",
        arguments={"query": "changed"},
        raw_arguments='{"query":"changed"}',
    )
    llm = FakeLlm(
        [
            _completion("", first),
            _completion("", second),
            _completion(
                json.dumps({"change_summary": "- changed() 호출을 수정합니다.", "findings": []})
            ),
        ]
    )
    settings = ReviewSettings(max_tool_rounds=1)
    engine = NativeFirmwareReviewEngine(llm, settings)  # type: ignore[arg-type]
    context = ReviewContext(
        project="soc/fw",
        change_number=13,
        patchset_number=1,
        revision_sha="1" * 40,
        diff="+changed();",
        changed_files=["fw.c"],
        changed_lines=[ChangedLine(path="fw.c", line=1, text="changed();")],
        policy_text="policy",
        repository_root=str(tmp_path),
    )
    trace: list[dict] = []

    async def capture(event: dict) -> None:
        trace.append(event)

    result = await engine.review(
        context,
        RepositoryToolExecutor(tmp_path, settings),
        tool_trace=capture,
    )

    assert result.findings == []
    assert len(llm.seen_messages) == 3
    assert llm.seen_tools[-1] is None
    assert any(
        event.get("tool") == "search_text" and event.get("status") == "round_limit_suppressed"
        for event in trace
    )
    assert any(
        event.get("event") == "forced_finalization" and event.get("reason") == "max_tool_rounds"
        for event in trace
    )


@pytest.mark.asyncio
async def test_duplicate_repository_tool_call_is_suppressed_and_finalized(tmp_path: Path) -> None:
    target = tmp_path / "fw.c"
    target.write_text("changed();\n", encoding="utf-8")
    duplicate = ToolCall(
        id="call-1",
        name="read_file",
        arguments={"path": "fw.c", "start_line": 1},
        raw_arguments='{"path":"fw.c","start_line":1}',
    )
    duplicate_again = ToolCall(
        id="call-2",
        name="read_file",
        arguments={"path": "fw.c", "start_line": 1},
        raw_arguments='{"path":"fw.c","start_line":1}',
    )
    llm = FakeLlm(
        [
            _completion("", duplicate),
            _completion("", duplicate_again),
            _completion(
                json.dumps({"change_summary": "- changed() 호출을 수정합니다.", "findings": []})
            ),
        ]
    )
    settings = ReviewSettings(max_tool_rounds=8)
    engine = NativeFirmwareReviewEngine(llm, settings)  # type: ignore[arg-type]
    context = ReviewContext(
        project="soc/fw",
        change_number=14,
        patchset_number=1,
        revision_sha="2" * 40,
        diff="+changed();",
        changed_files=["fw.c"],
        changed_lines=[ChangedLine(path="fw.c", line=1, text="changed();")],
        policy_text="policy",
        repository_root=str(tmp_path),
    )
    trace: list[dict] = []

    async def capture(event: dict) -> None:
        trace.append(event)

    result = await engine.review(
        context,
        RepositoryToolExecutor(tmp_path, settings),
        tool_trace=capture,
    )

    assert result.findings == []
    statuses = [event.get("status") for event in trace if event.get("event") == "tool_call"]
    assert statuses == ["ok", "duplicate_suppressed"]
    assert trace[-1]["event"] == "forced_finalization"
    assert trace[-1]["reason"] == "duplicate_tool_loop"
    assert llm.seen_tools[-1] is None


@pytest.mark.asyncio
async def test_semantically_equivalent_tool_defaults_are_duplicate_suppressed(
    tmp_path: Path,
) -> None:
    target = tmp_path / "fw.c"
    target.write_text("changed();\n", encoding="utf-8")
    implicit_defaults = ToolCall(
        id="call-1",
        name="read_file",
        arguments={"path": "./fw.c"},
        raw_arguments='{"path":"./fw.c"}',
    )
    explicit_defaults = ToolCall(
        id="call-2",
        name="read_file",
        arguments={"path": "fw.c", "start_line": 1, "end_line": 200},
        raw_arguments='{"path":"fw.c","start_line":1,"end_line":200}',
    )
    llm = FakeLlm(
        [
            _completion("", implicit_defaults),
            _completion("", explicit_defaults),
            _completion(
                json.dumps({"change_summary": "- changed() 호출을 수정합니다.", "findings": []})
            ),
        ]
    )
    settings = ReviewSettings(max_tool_rounds=8)
    engine = NativeFirmwareReviewEngine(llm, settings)  # type: ignore[arg-type]
    context = ReviewContext(
        project="soc/fw",
        change_number=17,
        patchset_number=1,
        revision_sha="5" * 40,
        diff="+changed();",
        changed_files=["fw.c"],
        changed_lines=[ChangedLine(path="fw.c", line=1, text="changed();")],
        policy_text="policy",
        repository_root=str(tmp_path),
    )
    trace: list[dict] = []

    async def capture(event: dict) -> None:
        trace.append(event)

    result = await engine.review(
        context,
        RepositoryToolExecutor(tmp_path, settings),
        tool_trace=capture,
    )

    assert result.findings == []
    statuses = [event.get("status") for event in trace if event.get("event") == "tool_call"]
    assert statuses == ["ok", "duplicate_suppressed"]


@pytest.mark.asyncio
async def test_tool_error_can_be_retried_without_duplicate_suppression(tmp_path: Path) -> None:
    target = tmp_path / "fw.c"
    target.write_text("changed();\n", encoding="utf-8")
    invalid = ToolCall(
        id="call-1",
        name="read_file",
        arguments={"path": "../outside.c"},
        raw_arguments='{"path":"../outside.c"}',
    )
    invalid_retry = ToolCall(
        id="call-2",
        name="read_file",
        arguments={"path": "../outside.c"},
        raw_arguments='{"path":"../outside.c"}',
    )
    llm = FakeLlm(
        [
            _completion("", invalid),
            _completion("", invalid_retry),
            _completion(
                json.dumps({"change_summary": "- changed() 호출을 수정합니다.", "findings": []})
            ),
        ]
    )
    settings = ReviewSettings(max_tool_rounds=8)
    engine = NativeFirmwareReviewEngine(llm, settings)  # type: ignore[arg-type]
    context = ReviewContext(
        project="soc/fw",
        change_number=15,
        patchset_number=1,
        revision_sha="3" * 40,
        diff="+changed();",
        changed_files=["fw.c"],
        changed_lines=[ChangedLine(path="fw.c", line=1, text="changed();")],
        policy_text="policy",
        repository_root=str(tmp_path),
    )
    trace: list[dict] = []

    async def capture(event: dict) -> None:
        trace.append(event)

    result = await engine.review(
        context,
        RepositoryToolExecutor(tmp_path, settings),
        tool_trace=capture,
    )

    assert result.findings == []
    statuses = [event.get("status") for event in trace if event.get("event") == "tool_call"]
    assert statuses == ["error", "error"]


@pytest.mark.asyncio
async def test_review_language_can_be_switched_to_english(tmp_path: Path) -> None:
    target = tmp_path / "fw.c"
    target.write_text("changed();\n", encoding="utf-8")
    llm = FakeLlm(
        [_completion(json.dumps({"change_summary": "- Updates changed().", "findings": []}))]
    )
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

    result = await engine.review(context, RepositoryToolExecutor(tmp_path, settings))

    assert "clear English" in llm.seen_messages[0][1]["content"]
    assert "natural Korean" not in llm.seen_messages[0][1]["content"]
    assert result.summary.startswith("Change summary\n- Updates changed().")
    assert "Review result\n- No actionable firmware correctness issues" in result.summary


@pytest.mark.asyncio
async def test_verifier_can_drop_candidate(tmp_path: Path) -> None:
    target = tmp_path / "fw.c"
    target.write_text("changed();\n", encoding="utf-8")
    candidate = json.dumps(
        {
            "change_summary": "- fw.c의 호출 경로를 변경합니다.",
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
    verified = json.dumps({"review_summary": "", "findings": []})
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
    assert "변경 요약" in result.summary
    assert "fw.c의 호출 경로를 변경합니다." in result.summary
    assert "리뷰 결과" in result.summary
    assert "추가로 조치가 필요한 펌웨어 동작상 문제는 발견되지 않았습니다." in result.summary


@pytest.mark.asyncio
async def test_findings_with_empty_verifier_summary_use_korean_fallback(tmp_path: Path) -> None:
    target = tmp_path / "fw" / "train.c"
    target.parent.mkdir(parents=True)
    target.write_text("int rc = poll_done();\nadvance();\n", encoding="utf-8")
    candidate_payload = json.loads(_review_json())
    verified_payload = json.loads(_review_json())
    verified_payload["review_summary"] = ""
    llm = FakeLlm(
        [
            _completion(json.dumps(candidate_payload)),
            _completion(json.dumps(verified_payload)),
        ]
    )
    settings = ReviewSettings()
    engine = NativeFirmwareReviewEngine(llm, settings)  # type: ignore[arg-type]
    context = ReviewContext(
        project="soc/fw",
        change_number=2,
        patchset_number=1,
        revision_sha="c" * 40,
        diff="+advance();",
        changed_files=["fw/train.c"],
        changed_lines=[ChangedLine(path="fw/train.c", line=2, text="advance();")],
        policy_text="policy",
        repository_root=str(tmp_path),
    )

    result = await engine.review(context, RepositoryToolExecutor(tmp_path, settings))

    assert len(result.findings) == 1
    assert "검증된 조치 필요 이슈 1건이 있습니다 (P1: 1)." in result.summary


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
async def test_context_overflow_after_tool_round_keeps_spent_usage(tmp_path: Path) -> None:
    target = tmp_path / "fw.c"
    target.write_text("changed();\n", encoding="utf-8")
    diff = "+changed();\n" * 800
    read = ToolCall(
        id="call-1",
        name="read_file",
        arguments={"path": "fw.c"},
        raw_arguments='{"path":"fw.c"}',
    )
    llm = FakeLlm(
        [
            _completion("", read),
            ContextLengthError("maximum context length"),
            _completion(
                json.dumps({"change_summary": "- changed() 호출을 수정합니다.", "findings": []})
            ),
            _completion(
                json.dumps({"change_summary": "- changed() 호출을 수정합니다.", "findings": []})
            ),
        ]
    )
    settings = ReviewSettings(max_diff_chunk_chars=20_000)
    engine = NativeFirmwareReviewEngine(llm, settings)  # type: ignore[arg-type]
    context = ReviewContext(
        project="soc/fw",
        change_number=16,
        patchset_number=1,
        revision_sha="4" * 40,
        diff=diff,
        changed_files=["fw.c"],
        changed_lines=[ChangedLine(path="fw.c", line=1, text="changed();")],
        policy_text="policy",
        repository_root=str(tmp_path),
    )

    result = await engine.review(context, RepositoryToolExecutor(tmp_path, settings))

    # 10/5 from the abandoned tool round plus 10/5 for each successful split chunk.
    assert result.input_tokens == 30
    assert result.output_tokens == 15


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
    missed = json.dumps({"change_summary": "- training 경로를 수정합니다.", "findings": []})
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


@pytest.mark.asyncio
async def test_candidate_chunk_budget_returns_partial_coverage_without_failing(
    tmp_path: Path,
) -> None:
    def section(path: str, marker: str) -> str:
        body = "".join(f"+{marker}_{index}_" + "x" * 36 + "\n" for index in range(60))
        return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1,60 @@\n{body}"

    for name in ("a.c", "b.c"):
        (tmp_path / name).write_text("changed();\n", encoding="utf-8")
    diff = section("a.c", "A") + section("b.c", "B")
    llm = FakeLlm(
        [_completion(json.dumps({"change_summary": "- a.c를 수정합니다.", "findings": []}))]
    )
    settings = ReviewSettings(max_diff_chunk_chars=4_000, max_candidate_chunks=1)
    engine = NativeFirmwareReviewEngine(llm, settings)  # type: ignore[arg-type]
    context = ReviewContext(
        project="soc/fw",
        change_number=20,
        patchset_number=1,
        revision_sha="6" * 40,
        diff=diff,
        changed_files=["a.c", "b.c"],
        changed_lines=[
            ChangedLine(path="a.c", line=1, text="changed();"),
            ChangedLine(path="b.c", line=1, text="changed();"),
        ],
        policy_text="policy",
        repository_root=str(tmp_path),
    )

    result = await engine.review(context, RepositoryToolExecutor(tmp_path, settings))

    budget = result.review_metadata["review_budget"]
    assert budget["candidate_chunks_reviewed"] == 1
    assert budget["candidate_chunks_total"] == 2
    assert budget["reviewable_files_fully_reviewed"] == 1
    assert budget["reviewable_files_total"] == 2
    assert budget["stop_reasons"] == ["max_candidate_chunks"]
    assert result.review_metadata["lineage_complete"] is False
    assert "리뷰 범위" in result.summary
    assert "1/2" in result.summary


@pytest.mark.asyncio
async def test_job_llm_call_budget_stops_tool_loop_as_partial_review(tmp_path: Path) -> None:
    target = tmp_path / "fw.c"
    target.write_text("changed();\n", encoding="utf-8")
    read = ToolCall(
        id="call-budget",
        name="read_file",
        arguments={"path": "fw.c"},
        raw_arguments='{"path":"fw.c"}',
    )
    llm = FakeLlm([_completion("", read)])
    settings = ReviewSettings(max_llm_calls_per_job=1)
    engine = NativeFirmwareReviewEngine(llm, settings)  # type: ignore[arg-type]
    context = ReviewContext(
        project="soc/fw",
        change_number=21,
        patchset_number=1,
        revision_sha="7" * 40,
        diff="+changed();",
        changed_files=["fw.c"],
        changed_lines=[ChangedLine(path="fw.c", line=1, text="changed();")],
        policy_text="policy",
        repository_root=str(tmp_path),
    )

    result = await engine.review(context, RepositoryToolExecutor(tmp_path, settings))

    budget = result.review_metadata["review_budget"]
    assert budget["llm_calls"] == 1
    assert budget["tool_calls"] == 1
    assert budget["candidate_chunks_reviewed"] == 0
    assert budget["stop_reasons"] == ["max_llm_calls_per_job"]
    assert result.input_tokens == 10
    assert result.output_tokens == 5
    assert result.review_metadata["lineage_complete"] is False
    assert "검토가 완료된 범위" in result.summary


@pytest.mark.asyncio
async def test_job_input_token_budget_stops_next_llm_call_after_reported_usage(
    tmp_path: Path,
) -> None:
    target = tmp_path / "fw.c"
    target.write_text("changed();\n", encoding="utf-8")
    read = ToolCall(
        id="call-token-budget",
        name="read_file",
        arguments={"path": "fw.c"},
        raw_arguments='{"path":"fw.c"}',
    )
    first = LlmCompletion(
        content="",
        tool_calls=(read,),
        input_tokens=1200,
        output_tokens=5,
        finish_reason="tool_calls",
        raw_message={},
    )
    llm = FakeLlm([first])
    settings = ReviewSettings(max_input_tokens_per_job=1000)
    engine = NativeFirmwareReviewEngine(llm, settings)  # type: ignore[arg-type]
    context = ReviewContext(
        project="soc/fw",
        change_number=24,
        patchset_number=1,
        revision_sha="a" * 40,
        diff="+changed();",
        changed_files=["fw.c"],
        changed_lines=[ChangedLine(path="fw.c", line=1, text="changed();")],
        policy_text="policy",
        repository_root=str(tmp_path),
    )

    result = await engine.review(context, RepositoryToolExecutor(tmp_path, settings))

    budget = result.review_metadata["review_budget"]
    assert budget["llm_calls"] == 1
    assert budget["input_tokens"] == 1200
    assert budget["stop_reasons"] == ["max_input_tokens_per_job"]
    assert result.input_tokens == 1200
    assert result.review_metadata["lineage_complete"] is False


@pytest.mark.asyncio
async def test_job_tool_budget_forces_final_answer_and_discloses_limit(tmp_path: Path) -> None:
    target = tmp_path / "fw.c"
    target.write_text("changed();\n", encoding="utf-8")
    read = ToolCall(
        id="call-tool-budget",
        name="read_file",
        arguments={"path": "fw.c"},
        raw_arguments='{"path":"fw.c"}',
    )
    llm = FakeLlm(
        [
            _completion("", read),
            _completion(
                json.dumps({"change_summary": "- changed() 호출을 수정합니다.", "findings": []})
            ),
        ]
    )
    settings = ReviewSettings(max_tool_calls_per_job=0)
    engine = NativeFirmwareReviewEngine(llm, settings)  # type: ignore[arg-type]
    context = ReviewContext(
        project="soc/fw",
        change_number=22,
        patchset_number=1,
        revision_sha="8" * 40,
        diff="+changed();",
        changed_files=["fw.c"],
        changed_lines=[ChangedLine(path="fw.c", line=1, text="changed();")],
        policy_text="policy",
        repository_root=str(tmp_path),
    )
    trace: list[dict] = []

    async def capture(event: dict) -> None:
        trace.append(event)

    result = await engine.review(
        context,
        RepositoryToolExecutor(tmp_path, settings),
        tool_trace=capture,
    )

    budget = result.review_metadata["review_budget"]
    assert budget["llm_calls"] == 2
    assert budget["tool_calls"] == 0
    assert budget["candidate_chunks_reviewed"] == 1
    assert budget["stop_reasons"] == ["max_tool_calls_per_job"]
    assert result.review_metadata["lineage_complete"] is False
    assert any(event.get("status") == "job_budget_suppressed" for event in trace)
    assert any(
        event.get("event") == "forced_finalization"
        and event.get("reason") == "max_tool_calls_per_job"
        for event in trace
    )
    assert "리뷰 범위" in result.summary


@pytest.mark.asyncio
async def test_verifier_budget_exhaustion_never_publishes_unverified_candidate(
    tmp_path: Path,
) -> None:
    target = tmp_path / "fw.c"
    target.write_text("changed();\n", encoding="utf-8")
    candidate = json.loads(_review_json())
    candidate["findings"][0]["location"] = {"path": "fw.c", "start_line": 1}
    llm = FakeLlm([_completion(json.dumps(candidate))])
    settings = ReviewSettings(max_llm_calls_per_job=1)
    engine = NativeFirmwareReviewEngine(llm, settings)  # type: ignore[arg-type]
    context = ReviewContext(
        project="soc/fw",
        change_number=23,
        patchset_number=1,
        revision_sha="9" * 40,
        diff="+changed();",
        changed_files=["fw.c"],
        changed_lines=[ChangedLine(path="fw.c", line=1, text="changed();")],
        policy_text="policy",
        repository_root=str(tmp_path),
    )

    result = await engine.review(context, RepositoryToolExecutor(tmp_path, settings))

    budget = result.review_metadata["review_budget"]
    assert result.findings == []
    assert budget["candidate_chunks_reviewed"] == 1
    assert budget["verification_complete"] is False
    assert budget["stop_reasons"] == ["max_llm_calls_per_job"]
    assert result.review_metadata["lineage_complete"] is False
    assert "finding 검증: 미완료" in result.summary
