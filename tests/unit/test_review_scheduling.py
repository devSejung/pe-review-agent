from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from pe_review_agent.config import ReviewSettings
from pe_review_agent.domain import ChangedLine, Finding, ReviewContext
from pe_review_agent.llm.client import LlmCompletion, ToolCall
from pe_review_agent.repos.tools import RepositoryToolExecutor
from pe_review_agent.retry import ContextLengthError, TransientError
from pe_review_agent.review.lineage import FindingHistory, reconcile_finding_lineage
from pe_review_agent.review.native import NativeFirmwareReviewEngine
from pe_review_agent.review.progress import MemoryProgressBackend, ReviewProgress, Usage
from pe_review_agent.review.scheduling import ReviewBudget, RoundRobinReview, WorkItem


def completion(content: str = '{"findings":[]}', *calls: ToolCall) -> LlmCompletion:
    return LlmCompletion(content, tuple(calls), 10, 5, "tool_calls" if calls else "stop", {})


def read_call(key: str, number: int = 1) -> ToolCall:
    args = {"path": f"{key}.c", "start_line": number}
    return ToolCall(f"{key}-{number}", "read_file", args, json.dumps(args))


class Tools:
    tool_schemas = [{"type": "function", "function": {"name": "read_file"}}]

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, name: str, arguments: dict) -> str:
        self.executed.append(arguments["path"])
        return "concrete evidence"


class DemandLlm:
    def __init__(self, demand: dict[str, int]) -> None:
        self.settings = SimpleNamespace(model="test-model")
        self.demand = demand
        self.counts: Counter = Counter()
        self.requests: list[tuple[str, bool]] = []

    async def complete(self, *, messages, tools=None):
        key = messages[0]["content"]
        self.counts[key] += 1
        self.requests.append((key, tools is not None))
        if tools is not None and self.counts[key] < self.demand.get(key, 1):
            return completion("", read_call(key, self.counts[key]))
        return completion()


async def run_demand(demand: dict[str, int], settings: ReviewSettings | None = None):
    settings = settings or ReviewSettings()
    llm, tools, backend = DemandLlm(demand), Tools(), MemoryProgressBackend()
    progress = ReviewProgress(input_key="a" * 64)
    budget = ReviewBudget(settings)
    runner = RoundRobinReview(
        llm=llm,
        tools=tools,
        settings=settings,
        budget=budget,
        backend=backend,
        progress=progress,
    )
    work = [WorkItem(key=key, payload=key) for key in demand]

    def split(_):
        raise AssertionError("unexpected context split")

    result = await runner.run(
        "candidate",
        work,
        messages=lambda item: [{"role": "user", "content": item.key}],
        parse=lambda content, _: json.loads(content),
        split=split,
        max_units=settings.max_candidate_chunks,
    )
    return llm, tools, backend, budget, result, runner


@pytest.mark.asyncio
async def test_ten_demanding_chunks_leave_verifier_ten_calls_and_finalize_every_session():
    keys = [str(index) for index in range(10)]
    llm, tools, _, budget, result, runner = await run_demand(dict.fromkeys(keys, 3))
    assert [key for key, _ in llm.requests[:10]] == keys
    assert llm.counts == dict.fromkeys(keys, 2)
    assert [key for key, enabled in llm.requests if not enabled] == keys
    assert len(result.completed) == 10 and not result.pending
    assert len(tools.executed) == 10
    assert budget.usage.llm_calls == 20
    assert budget.remaining_calls("verification") == 10
    assert "candidate_final_slot" in result.limitations
    verified = await runner.run(
        "verification",
        [WorkItem(key="verify", payload=None)],
        messages=lambda _: [{"role": "user", "content": "verify"}],
        parse=lambda text, _: json.loads(text),
        split=lambda _: [],
        max_units=1,
    )
    assert len(verified.completed) == 1 and llm.counts["verify"] == 1


@pytest.mark.asyncio
async def test_easy_chunks_release_their_reserved_final_slot_to_harder_chunks():
    demand = {str(index): 1 if index % 2 == 0 else 3 for index in range(10)}
    llm, _, _, budget, result, _ = await run_demand(demand)
    assert llm.counts == demand
    assert budget.usage.llm_calls == 20 and len(result.completed) == 10
    assert [key for key, _ in llm.requests[:10]] == list(demand)


@pytest.mark.parametrize("calls", [1, 2, 3, 5, 10, 30])
@pytest.mark.parametrize("tools", [0, 1, 5, 50])
@pytest.mark.asyncio
async def test_tiny_budgets_never_exceed_cap_or_borrow_verifier_reserve(calls, tools):
    settings = ReviewSettings(max_llm_calls_per_job=calls, max_tool_calls_per_job=tools)
    llm, executor, _, budget, result, _ = await run_demand(
        dict.fromkeys((str(index) for index in range(12)), 20),
        settings,
    )
    assert budget.usage.llm_calls <= calls - __import__("math").ceil(calls / 3)
    assert budget.usage.tool_calls <= tools - __import__("math").ceil(tools / 3)
    assert len(llm.requests) == budget.usage.llm_calls
    assert len(executor.executed) == budget.usage.tool_calls
    assert len(result.completed) + len(result.pending) == 12


@pytest.mark.parametrize("rounds", [0, 1, 8])
@pytest.mark.asyncio
async def test_tool_round_limit_counts_only_allowed_tool_rounds_then_one_final(rounds):
    llm, tools, _, _, result, _ = await run_demand(
        {"one": 100},
        ReviewSettings(max_tool_rounds=rounds, max_llm_calls_per_job=100),
    )
    assert llm.counts["one"] == rounds + 1
    assert len(tools.executed) == rounds
    assert llm.requests[-1] == ("one", False)
    assert "max_tool_rounds" in result.limitations


class ScriptLlm:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.requests = []
        self.settings = SimpleNamespace(model="same-model")

    async def complete(self, *, messages, tools=None):
        self.requests.append((messages, tools))
        item = self.outputs.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def context(root: Path, *, diff: str = "+advance();\n") -> ReviewContext:
    (root / "fw.c").write_text("advance();\n", encoding="utf-8")
    return ReviewContext(
        project="soc/fw",
        change_number=1,
        patchset_number=2,
        revision_sha="a" * 40,
        diff=diff,
        changed_files=["fw.c"],
        changed_lines=[ChangedLine(path="fw.c", line=1, text="advance();")],
        policy_text="firmware",
        repository_root=str(root),
    )


def findings(count: int) -> list[dict]:
    return [
        {
            "severity": "P1",
            "category": "timeout",
            "title": f"Ignored failure {index}",
            "message": f"Trigger {index}",
            "impact": "stale state",
            "evidence": f"Contract {index}",
            "location": {"path": "fw.c", "start_line": 1},
            "confidence": 0.96,
        }
        for index in range(count)
    ]


async def engine_run(ctx, outputs, backend, settings=None):
    settings = settings or ReviewSettings()
    llm = ScriptLlm(outputs)
    result = await NativeFirmwareReviewEngine(llm, settings).review(
        ctx,
        RepositoryToolExecutor(Path(ctx.repository_root), settings),
        backend=backend,
    )
    return result, llm


@pytest.mark.asyncio
async def test_verifier_resume_freezes_candidates_and_refunds_only_failed_batch(tmp_path):
    backend, ctx = MemoryProgressBackend(), context(tmp_path)
    candidate = completion(json.dumps({"change_summary": "changed", "findings": findings(16)}))
    with pytest.raises(TransientError):
        await engine_run(ctx, [candidate, completion(), TransientError("timeout")], backend)
    saved = next(iter(backend.progress.values()))
    assert saved.phase == "verification" and len(saved.frozen_candidates) == 16
    assert len([cp for cp in saved.checkpoints.values() if cp.phase == "verification"]) == 1
    result, llm = await engine_run(ctx, [completion()], backend)
    assert len(llm.requests) == 1
    assert "independent verifier" in llm.requests[0][0][0]["content"]
    assert result.review_metadata["review_budget"]["llm_calls"] == 3
    assert result.review_metadata["actual_usage"]["llm_calls"] == 4
    assert result.review_metadata["verifier_checkpoints"]["reused"] == 1
    # A crash after engine completion but before READY_TO_PUBLISH needs no new model requests.
    again, llm = await engine_run(ctx, [], backend)
    assert not llm.requests and again.findings == result.findings


@pytest.mark.parametrize("error", [TransientError("timeout"), asyncio.CancelledError()])
@pytest.mark.asyncio
async def test_incomplete_candidate_restarts_from_initial_prompt_without_old_charge(
    tmp_path, error
):
    backend, ctx = MemoryProgressBackend(), context(tmp_path)
    settings = ReviewSettings()
    llm = ScriptLlm([completion("", read_call("fw")), error])
    with pytest.raises(type(error)):
        await NativeFirmwareReviewEngine(llm, settings).review(
            ctx,
            RepositoryToolExecutor(tmp_path, settings),
            backend=backend,
        )
    result, llm = await engine_run(ctx, [completion()], backend)
    assert not any(item["role"] == "tool" for item in llm.requests[0][0])
    assert result.review_metadata["review_budget"]["llm_calls"] == 1
    assert result.review_metadata["review_budget"]["tool_calls"] == 0
    assert result.review_metadata["actual_usage"]["llm_calls"] == 3
    assert result.review_metadata["actual_usage"]["tool_calls"] == 1


@pytest.mark.parametrize("bad", ["not json", "{}", '{"findings":42}'])
@pytest.mark.asyncio
async def test_invalid_candidate_result_keeps_actual_usage_but_not_budget_debt(tmp_path, bad):
    backend, ctx = MemoryProgressBackend(), context(tmp_path)
    with pytest.raises(TransientError):
        await engine_run(ctx, [completion(bad)], backend)
    result, _ = await engine_run(ctx, [completion()], backend)
    assert result.review_metadata["review_budget"]["llm_calls"] == 1
    assert result.review_metadata["actual_usage"]["llm_calls"] == 2
    assert result.review_metadata["actual_usage"]["input_tokens"] == 20


@pytest.mark.asyncio
async def test_candidate_limitations_survive_crash_before_phase_freeze_and_never_resolve_prior(
    tmp_path,
):
    class CrashAtFreeze(MemoryProgressBackend):
        crash = True

        async def save(self, progress):
            if self.crash and progress.phase == "verification":
                self.crash = False
                raise asyncio.CancelledError()
            await super().save(progress)

    backend, ctx = CrashAtFreeze(), context(tmp_path)
    prior = Finding.model_validate(findings(1)[0])
    prior.ensure_semantic_id(project=ctx.project)
    ctx.previous_findings = [prior]
    ctx.previous_patchset_number = 1
    settings = ReviewSettings(max_tool_calls_per_job=0)
    with pytest.raises(asyncio.CancelledError):
        await engine_run(ctx, [completion()], backend, settings)
    result, llm = await engine_run(ctx, [completion()], backend, settings)
    assert len(llm.requests) == 1
    assert "candidate_tool_budget" in result.review_metadata["review_budget"]["stop_reasons"]
    assert result.review_metadata["lineage_complete"] is False
    history = FindingHistory(1, (prior,), frozenset([prior.semantic_id]))
    reconciled = reconcile_finding_lineage(
        project=ctx.project,
        review=result,
        history=history,
        complete=result.review_metadata["lineage_complete"],
    )
    assert reconciled.resolved_findings == ()


@pytest.mark.asyncio
async def test_normal_budget_stop_is_not_refunded_by_restart(tmp_path):
    backend, ctx = MemoryProgressBackend(), context(tmp_path)
    settings = ReviewSettings(max_llm_calls_per_job=3)
    candidate = completion(json.dumps({"findings": findings(1)}))
    first, _ = await engine_run(
        ctx, [completion("", read_call("fw")), candidate, completion()], backend, settings
    )
    second, llm = await engine_run(ctx, [], backend, settings)
    assert not llm.requests
    assert first.review_metadata["review_budget"] == second.review_metadata["review_budget"]
    assert first.review_metadata["lineage_complete"] is False


@pytest.mark.asyncio
async def test_model_or_policy_change_never_reuses_incompatible_result(tmp_path):
    backend, ctx = MemoryProgressBackend(), context(tmp_path)
    await engine_run(ctx, [completion()], backend)
    changed = ctx.model_copy(update={"policy_text": "new firmware contract"})
    _, llm = await engine_run(changed, [completion()], backend)
    assert len(llm.requests) == 1 and len(backend.progress) == 2


@pytest.mark.asyncio
async def test_legacy_v1_usage_is_audit_only_when_legacy_results_are_not_reused(tmp_path):
    class LegacyBackend(MemoryProgressBackend):
        async def legacy(self):
            return True, Usage(llm_calls=20, tool_calls=10, input_tokens=200_000)

    backend, ctx = LegacyBackend(), context(tmp_path)
    result, llm = await engine_run(ctx, [completion()], backend)

    assert len(llm.requests) == 1
    assert result.review_metadata["review_budget"]["llm_calls"] == 1
    assert result.review_metadata["review_budget"]["tool_calls"] == 0
    assert result.review_metadata["lineage_complete"] is True
    assert result.review_metadata["legacy_v1_checkpoint_present"] is True
    assert result.review_metadata["legacy_v1_usage"]["llm_calls"] == 20


@pytest.mark.asyncio
async def test_candidate_dedup_happens_before_selection_cap_and_drops_are_disclosed(tmp_path):
    backend, ctx = MemoryProgressBackend(), context(tmp_path)
    candidates = findings(2)
    data = [candidates[0]] * 64 + [candidates[1]]
    result, _ = await engine_run(
        ctx, [completion(json.dumps({"findings": data})), completion()], backend
    )
    assert result.review_metadata["candidate_selection"]["distinct"] == 2
    assert result.review_metadata["candidate_selection"]["selected"] == 2
    limited, _ = await engine_run(
        ctx,
        [completion(json.dumps({"findings": candidates})), completion()],
        MemoryProgressBackend(),
        ReviewSettings(max_candidate_findings_total=1),
    )
    assert (
        "max_candidate_findings_total" in limited.review_metadata["review_budget"]["stop_reasons"]
    )
    assert limited.review_metadata["lineage_complete"] is False


@pytest.mark.asyncio
async def test_max_findings_zero_never_turns_verified_issue_into_clean_verdict(tmp_path):
    backend, ctx = MemoryProgressBackend(), context(tmp_path)
    issue = findings(1)[0]
    result, _ = await engine_run(
        ctx,
        [
            completion(json.dumps({"findings": [issue]})),
            completion(json.dumps({"review_summary": "verified issue", "findings": [issue]})),
        ],
        backend,
        ReviewSettings(max_findings=0),
    )

    assert result.findings == []
    assert result.review_metadata["validated_finding_count"] == 1
    assert result.review_metadata["lineage_complete"] is False
    assert "max_findings" in result.review_metadata["review_budget"]["stop_reasons"]
    assert "verified issue" in result.summary
    assert "문제는 발견되지 않았습니다" not in result.summary


@pytest.mark.asyncio
async def test_context_rejected_verifier_batch_split_is_reused_after_retry(tmp_path):
    backend, ctx = MemoryProgressBackend(), context(tmp_path)
    candidate = completion(json.dumps({"findings": findings(4)}))
    with pytest.raises(TransientError):
        await engine_run(
            ctx,
            [candidate, ContextLengthError("context"), completion(), TransientError("timeout")],
            backend,
        )
    result, llm = await engine_run(ctx, [completion()], backend)
    assert len(llm.requests) == 1
    assert result.review_metadata["verifier_checkpoints"]["reused"] == 2
    assert result.review_metadata["actual_usage"]["llm_calls"] == 5
    assert result.review_metadata["review_budget"]["llm_calls"] == 3


@pytest.mark.asyncio
async def test_completed_prior_findings_are_not_lost_to_inline_comment_limit(tmp_path):
    backend, ctx = MemoryProgressBackend(), context(tmp_path)
    prior = [Finding.model_validate(item) for item in findings(9)]
    for item in prior:
        item.ensure_semantic_id(project=ctx.project)
    ctx.previous_findings = prior
    candidate = completion()
    verify1 = completion(
        json.dumps({"findings": [item.model_dump(mode="json") for item in prior[:8]]})
    )
    verify2 = completion(json.dumps({"findings": [prior[8].model_dump(mode="json")]}))
    result, _ = await engine_run(ctx, [candidate, verify1, verify2], backend)
    assert len(result.findings) == 8
    assert result.review_metadata["lineage_complete"] is False
    history = FindingHistory(1, tuple(prior), frozenset(item.semantic_id for item in prior))
    assert (
        reconcile_finding_lineage(
            project=ctx.project, review=result, history=history,
            complete=result.review_metadata["lineage_complete"],
        ).resolved_findings
        == ()
    )
