from __future__ import annotations

import json
import math
import posixpath
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pe_review_agent.config import ReviewSettings
from pe_review_agent.llm.client import LlmClient, ToolCall, assistant_message_for_tool_loop
from pe_review_agent.repos.tools import RepositoryToolExecutor
from pe_review_agent.retry import ContextLengthError, TransientError
from pe_review_agent.review.progress import (
    Invocation,
    Phase,
    ProgressBackend,
    ReviewProgress,
    Usage,
    WorkCheckpoint,
)

Trace = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class ReviewBudget:
    settings: ReviewSettings
    usage: Usage = field(default_factory=Usage)

    def remaining_calls(self, phase: Phase) -> int:
        reserve = (
            math.ceil(self.settings.max_llm_calls_per_job * self.settings.verifier_budget_fraction)
            if phase == "candidate"
            else 0
        )
        return max(0, self.settings.max_llm_calls_per_job - reserve - self.usage.llm_calls)

    def remaining_tools(self, phase: Phase) -> int:
        reserve = (
            math.ceil(self.settings.max_tool_calls_per_job * self.settings.verifier_budget_fraction)
            if phase == "candidate"
            else 0
        )
        return max(0, self.settings.max_tool_calls_per_job - reserve - self.usage.tool_calls)


@dataclass(slots=True)
class WorkItem:
    key: str
    payload: Any
    paths: tuple[str, ...] = ()
    parent_key: str | None = None


@dataclass(slots=True)
class _Session:
    work: WorkItem
    transcript: list[dict[str, Any]]
    initial_slots: int = 2
    usage: Usage = field(default_factory=Usage)
    limitations: list[str] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    pending_tools: deque[ToolCall] = field(default_factory=deque)
    rounds: int = 0
    calls: int = 0
    duplicate_only: bool = True
    final_reason: str | None = None


@dataclass(slots=True)
class PhaseResult:
    completed: list[WorkItem] = field(default_factory=list)
    pending: list[WorkItem] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    reused: int = 0
    saved: int = 0


class RoundRobinReview:
    """One LLM request or repository read per turn, with a final slot for every live session."""

    def __init__(
        self,
        *,
        llm: LlmClient,
        tools: RepositoryToolExecutor,
        settings: ReviewSettings,
        budget: ReviewBudget,
        backend: ProgressBackend,
        progress: ReviewProgress,
        trace: Trace | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.settings = settings
        self.budget = budget
        self.backend = backend
        self.progress = progress
        self.trace = trace

    async def run(
        self,
        phase: Phase,
        work: list[WorkItem],
        *,
        messages: Callable[[WorkItem], list[dict[str, Any]]],
        parse: Callable[[str, WorkItem], dict[str, Any]],
        split: Callable[[WorkItem], list[WorkItem]],
        max_units: int,
    ) -> PhaseResult:
        outcome = PhaseResult()
        waiting: deque[WorkItem] = deque()

        async def expand(item: WorkItem) -> None:
            checkpoint = self.progress.checkpoints.get(item.key)
            if checkpoint is None:
                waiting.append(item)
                return
            if checkpoint.phase != phase:
                raise ValueError("checkpoint phase does not match work identity")
            outcome.reused += 1
            await self._trace(
                event="checkpoint_reused",
                phase=phase,
                work_key=item.key,
                status=checkpoint.status.lower(),
            )
            if checkpoint.status == "SPLIT":
                for child in split(item):
                    await expand(child)
            elif checkpoint.status == "DONE":
                outcome.completed.append(item)
                outcome.limitations.extend(checkpoint.limitations)
            else:
                outcome.pending.append(item)
                outcome.limitations.extend(checkpoint.limitations)

        for item in work:
            await expand(item)

        active: deque[_Session] = deque()
        while waiting or active:
            # Completed checkpoints have already been charged by the engine. Do not reserve them
            # again. Every new exploration session needs both its first and final request slots.
            while waiting and len(outcome.completed) + len(active) < max_units:
                protected = sum(
                    session.initial_slots if session.calls == 0 else 1 for session in active
                )
                spare = self.budget.remaining_calls(phase) - protected
                minimum = (
                    2 if self.settings.max_tool_rounds and self.budget.remaining_tools(phase) else 1
                )
                if spare < minimum:
                    # A final-only one-call review is still useful, but must not steal a live
                    # session's final slot or pretend that external evidence was inspected.
                    if active or spare < 1:
                        break
                item = waiting.popleft()
                active.append(
                    _Session(
                        work=item, transcript=messages(item), initial_slots=min(minimum, spare)
                    )
                )
                if spare < minimum:
                    break
            if not active:
                break

            session = active.popleft()
            if session.pending_tools:
                await self._tool_step(phase, session)
                active.append(session)
                continue

            protected = sum(other.initial_slots if other.calls == 0 else 1 for other in active)
            available = self.budget.remaining_calls(phase) - protected
            if available < 1:
                # Context-rejected calls can consume reservations before their split children are
                # admitted. Persist a normal stop so restart is not a way to refund this limit.
                await self._checkpoint(
                    phase,
                    session,
                    "STOPPED",
                    limitations=[f"{phase}_call_budget"],
                )
                outcome.pending.append(session.work)
                outcome.limitations.append(f"{phase}_call_budget")
                outcome.saved += 1
                continue

            final_reason = session.final_reason
            if final_reason is None and session.rounds >= self.settings.max_tool_rounds:
                final_reason = "max_tool_rounds"
            if final_reason is None and available == 1:
                final_reason = f"{phase}_final_slot"
            if final_reason is None and self.budget.remaining_tools(phase) == 0:
                final_reason = f"{phase}_tool_budget"
            final = final_reason is not None
            transcript = session.transcript
            if final:
                session.limitations.append(final_reason)
                transcript = [
                    *transcript,
                    {
                        "role": "user",
                        "content": (
                            "This is the final response for this session. Tools are unavailable. "
                            "Using only evidence already in this conversation, return the "
                            "requested final JSON now. Do not invent missing code or contracts. "
                            "Return only evidence-supported findings; insufficient evidence is not "
                            "proof that an earlier issue was fixed. Do not request any more tools."
                        ),
                    },
                ]
                await self._trace(
                    event="forced_finalization",
                    phase=phase,
                    work_key=session.work.key,
                    round=session.calls + 1,
                    reason=final_reason,
                )
            try:
                completion = await self._llm_step(phase, session, transcript, final=final)
                if completion.finish_reason == "length":
                    raise TransientError("review response was truncated at the model output limit")
                if completion.tool_calls:
                    if final:
                        raise TransientError(
                            "review model emitted tools in the final no-tools turn"
                        )
                    session.transcript.append(assistant_message_for_tool_loop(completion))
                    session.pending_tools.extend(completion.tool_calls)
                    session.rounds += 1
                    session.duplicate_only = True
                    active.append(session)
                    continue
                # Parsing is inside the same failure boundary as inference. Failed JSON never
                # creates a DONE checkpoint, although its actual request remains in the audit.
                result = parse(completion.content, session.work)
            except ContextLengthError:
                children = split(session.work)
                await self._checkpoint(phase, session, "SPLIT")
                outcome.saved += 1
                # Round robin preserves opportunities for other files instead of recursively
                # consuming all calls on the first oversized chunk.
                waiting.extend(children)
                continue

            await self._checkpoint(phase, session, "DONE", result=result)
            outcome.completed.append(session.work)
            outcome.limitations.extend(session.limitations)
            outcome.saved += 1

        outcome.pending.extend(waiting)
        if waiting:
            reason = (
                "max_candidate_chunks"
                if phase == "candidate" and len(outcome.completed) >= max_units
                else f"{phase}_call_budget"
            )
            outcome.limitations.append(reason)
            await self._trace(event="budget_exhausted", phase=phase, reason=reason)
        outcome.limitations = list(dict.fromkeys(outcome.limitations))
        return outcome

    async def _llm_step(
        self,
        phase: Phase,
        session: _Session,
        transcript: list[dict[str, Any]],
        *,
        final: bool,
    ) -> Any:
        invocation = Invocation(
            id=str(uuid.uuid4()),
            input_key=self.progress.input_key,
            phase=phase,
            work_key=session.work.key,
            kind="llm",
        )
        # Commit the intent before dispatch. A kill during HTTP leaves an auditable unknown call;
        # only completed work, not this abandoned session, is charged on the next review attempt.
        await self.backend.record_invocation(invocation)
        self.budget.usage.llm_calls += 1
        session.usage.llm_calls += 1
        session.calls += 1
        try:
            completion = await self.llm.complete(
                messages=transcript,
                tools=None if final else self.tools.tool_schemas,
            )
        except Exception as exc:
            invocation.status = "failed"
            invocation.error = f"{type(exc).__name__}: {str(exc)[:1000]}"
            invocation.input_tokens = getattr(exc, "input_tokens", None)
            invocation.output_tokens = getattr(exc, "output_tokens", None)
            await self.backend.record_invocation(invocation)
            self._tokens(session, invocation.input_tokens, invocation.output_tokens)
            raise
        invocation.status = "completed"
        invocation.input_tokens = completion.input_tokens
        invocation.output_tokens = completion.output_tokens
        await self.backend.record_invocation(invocation)
        self._tokens(session, completion.input_tokens, completion.output_tokens)
        return completion

    def _tokens(
        self, session: _Session, input_tokens: int | None, output_tokens: int | None
    ) -> None:
        for usage in (session.usage, self.budget.usage):
            usage.input_tokens += input_tokens or 0
            usage.output_tokens += output_tokens or 0

    async def _tool_step(self, phase: Phase, session: _Session) -> None:
        call = session.pending_tools.popleft()
        key = _tool_call_key(call.name, call.arguments)
        if key in session.seen:
            status = "duplicate_suppressed"
            result = json.dumps({"error": "duplicate read-only tool call; use the earlier result"})
        elif self.budget.remaining_tools(phase) == 0:
            status = "job_budget_suppressed"
            result = json.dumps(
                {"error": "repository tool budget exhausted; finalize from evidence"}
            )
            session.limitations.append(f"{phase}_tool_budget")
            session.final_reason = f"{phase}_tool_budget"
            session.duplicate_only = False
        else:
            invocation = Invocation(
                id=str(uuid.uuid4()),
                input_key=self.progress.input_key,
                phase=phase,
                work_key=session.work.key,
                kind="tool",
            )
            await self.backend.record_invocation(invocation)
            self.budget.usage.tool_calls += 1
            session.usage.tool_calls += 1
            session.duplicate_only = False
            try:
                result = await self.tools.execute(call.name, call.arguments)
            except (ValueError, OSError) as exc:
                result = json.dumps({"error": str(exc)})
            status = _tool_result_status(result)
            invocation.status = "failed" if status == "error" else "completed"
            invocation.error = result[:1000] if status == "error" else None
            await self.backend.record_invocation(invocation)
            if status != "error":
                session.seen.add(key)
        session.transcript.append({"role": "tool", "tool_call_id": call.id, "content": result})
        await self._trace(
            event="tool_call",
            phase=phase,
            work_key=session.work.key,
            round=session.calls,
            tool=call.name,
            arguments=call.arguments,
            status=status,
            result_bytes=len(result.encode("utf-8", errors="replace")),
            result_preview=_bounded_preview(result),
        )
        if not session.pending_tools and session.duplicate_only:
            session.final_reason = "duplicate_tool_loop"

    async def _checkpoint(
        self,
        phase: Phase,
        session: _Session,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        limitations: list[str] | None = None,
    ) -> None:
        checkpoint = WorkCheckpoint(
            phase=phase,
            key=session.work.key,
            parent_key=session.work.parent_key,
            status=status,
            paths=list(session.work.paths),
            result=result or {},
            # A context rejection has no reusable model result. Keep its deterministic split but
            # refund its discarded execution on crash/retry, like other incomplete sessions.
            usage=session.usage if status != "SPLIT" else Usage(),
            limitations=list(dict.fromkeys([*session.limitations, *(limitations or [])])),
        )
        self.progress.checkpoints[session.work.key] = checkpoint
        await self.backend.save(self.progress)
        await self._trace(
            event="checkpoint_saved",
            phase=phase,
            work_key=session.work.key,
            status=status.lower(),
        )

    async def _trace(self, **event: Any) -> None:
        if self.trace is not None:
            await self.trace({**event, "ts": datetime.now(UTC).isoformat()})


def _tool_call_key(name: str, arguments: dict[str, Any]) -> str:
    return json.dumps(
        {"name": name, "arguments": _canonical_tool_arguments(name, arguments)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _canonical_tool_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "read_file":
        start = _canonical_int(arguments.get("start_line", 1), default=1, low=1)
        if not isinstance(start, int):
            # Let the executor return its normal validation error; canonicalization must not turn
            # a malformed public tool argument into an unclassified scheduler crash.
            return arguments
        end_value = arguments.get("end_line")
        end = (
            _canonical_int(end_value, default=start + 199, low=1)
            if end_value is not None
            else start + 199
        )
        return {
            "path": _canonical_path(arguments.get("path")),
            "start_line": start,
            "end_line": end,
        }
    if name == "search_text":
        path = arguments.get("path")
        return {
            "query": arguments.get("query"),
            "path": _canonical_path(path) if path else None,
            "max_results": _canonical_int(
                arguments.get("max_results", 40), default=40, low=1, high=100
            ),
        }
    if name == "list_files":
        path = arguments.get("path")
        return {"path": _canonical_path(path) if path else None}
    return arguments


def _canonical_path(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    normalized = posixpath.normpath(value.replace("\\", "/").lstrip("/"))
    return normalized if normalized != "." else ""


def _canonical_int(value: Any, *, default: int, low: int, high: int | None = None) -> Any:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return value if value is not None else default
    parsed = max(low, parsed)
    return min(parsed, high) if high is not None else parsed


def _tool_result_status(result: str) -> str:
    stripped = result.strip()
    if stripped in {"<no matches>", "<empty>", "<no tracked files>"}:
        return "empty"
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and payload.get("error"):
            return "error"
    return "ok"


def _bounded_preview(value: str, limit: int = 1200) -> str:
    normalized = value.replace("\x00", "")
    return normalized if len(normalized) <= limit else normalized[:limit] + "\n<truncated>"
