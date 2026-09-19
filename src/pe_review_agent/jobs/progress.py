from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pe_review_agent.jobs.models import (
    Attempt,
    Job,
    ReviewChunkCheckpoint,
    ReviewInvocationRow,
    ReviewProgressRow,
)
from pe_review_agent.jobs.store import _require_live_lease
from pe_review_agent.retry import TransientError
from pe_review_agent.review.progress import Invocation, ReviewProgress, Usage


class PostgresProgressBackend:
    """Short lease-guarded transactions; no DB lock is held across an external request."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        job_id: uuid.UUID,
        worker_id: str,
        attempt_id: int | None,
    ) -> None:
        self.sessions = sessions
        self.job_id = job_id
        self.worker_id = worker_id
        self.attempt_id = attempt_id

    async def _guard(self, session: AsyncSession) -> None:
        job = await session.scalar(select(Job).where(Job.id == self.job_id).with_for_update())
        if job is None:
            raise KeyError(self.job_id)
        _require_live_lease(job, self.worker_id)

    async def load(self, input_key: str) -> ReviewProgress | None:
        try:
            async with self.sessions() as session:
                row = await session.get(ReviewProgressRow, (self.job_id, input_key))
                return ReviewProgress.model_validate(row.payload) if row else None
        except Exception as exc:
            raise TransientError(f"failed to load review progress: {exc}") from exc

    async def save(self, progress: ReviewProgress) -> None:
        try:
            async with self.sessions.begin() as session:
                await self._guard(session)
                existing = await session.get(ReviewProgressRow, (self.job_id, progress.input_key))
                if existing is not None:
                    old = ReviewProgress.model_validate(existing.payload)
                    order = {"candidate": 0, "verification": 1, "complete": 2}
                    if order[progress.phase] < order[old.phase]:
                        raise RuntimeError(
                            "review progress cannot move back to candidate exploration"
                        )
                    if old.phase == "complete" and old.result != progress.result:
                        raise RuntimeError("completed review result is immutable")
                    if old.phase != "candidate" and (
                        old.frozen_candidates != progress.frozen_candidates
                        or old.candidate_usage != progress.candidate_usage
                        or old.coverage != progress.coverage
                        or old.candidate_limitations != progress.candidate_limitations
                    ):
                        raise RuntimeError("verification candidate manifest is immutable")
                await session.execute(
                    pg_insert(ReviewProgressRow)
                    .values(
                        job_id=self.job_id,
                        input_key=progress.input_key,
                        payload=progress.model_dump(mode="json"),
                    )
                    .on_conflict_do_update(
                        index_elements=[ReviewProgressRow.job_id, ReviewProgressRow.input_key],
                        set_={
                            "payload": progress.model_dump(mode="json"),
                            "updated_at": func.now(),
                        },
                    )
                )
        except Exception as exc:
            raise TransientError(f"failed to persist review progress: {exc}") from exc

    async def record_invocation(self, invocation: Invocation) -> None:
        try:
            async with self.sessions.begin() as session:
                await self._guard(session)
                if self.attempt_id is None:
                    raise RuntimeError("invocation recording requires an active review attempt")
                attempt = await session.get(Attempt, self.attempt_id)
                if (
                    attempt is None
                    or attempt.job_id != self.job_id
                    or attempt.worker_id != self.worker_id
                    or attempt.finished_at is not None
                ):
                    raise RuntimeError("invocation requires this worker's live review attempt")
                row = await session.get(ReviewInvocationRow, invocation.id, with_for_update=True)
                data = invocation.model_dump()
                if row is None:
                    if invocation.status != "started":
                        raise RuntimeError("invocation must be recorded before dispatch")
                    session.add(
                        ReviewInvocationRow(
                            **data,
                            job_id=self.job_id,
                            attempt_id=self.attempt_id,
                        )
                    )
                else:
                    if (
                        row.job_id != self.job_id
                        or row.attempt_id != self.attempt_id
                        or any(
                            getattr(row, key) != data[key]
                            for key in ("input_key", "phase", "work_key", "kind")
                        )
                    ):
                        raise RuntimeError("invocation identity cannot change")
                    if row.status != "started":
                        if any(
                            getattr(row, key) != data[key]
                            for key in ("status", "input_tokens", "output_tokens", "error")
                        ):
                            raise RuntimeError("finished invocation cannot be rewritten")
                        return
                    for key in ("status", "input_tokens", "output_tokens", "error"):
                        setattr(row, key, data[key])
                    row.updated_at = func.now()
        except Exception as exc:
            raise TransientError(f"failed to persist review invocation: {exc}") from exc

    async def totals(self) -> dict[str, int]:
        try:
            async with self.sessions() as session:
                row = (
                    await session.execute(
                        select(
                            func.count().filter(ReviewInvocationRow.kind == "llm"),
                            func.count().filter(ReviewInvocationRow.kind == "tool"),
                            func.coalesce(func.sum(ReviewInvocationRow.input_tokens), 0),
                            func.coalesce(func.sum(ReviewInvocationRow.output_tokens), 0),
                            func.count().filter(ReviewInvocationRow.status == "failed"),
                            func.count().filter(ReviewInvocationRow.status == "started"),
                            func.count().filter(
                                ReviewInvocationRow.kind == "llm",
                                ReviewInvocationRow.input_tokens.is_(None),
                            ),
                        ).where(ReviewInvocationRow.job_id == self.job_id)
                    )
                ).one()
                return dict(
                    zip(
                        (
                            "llm_calls",
                            "tool_calls",
                            "input_tokens",
                            "output_tokens",
                            "failed_calls",
                            "unconfirmed_calls",
                            "llm_usage_unknown",
                        ),
                        (int(value) for value in row),
                        strict=True,
                    )
                )
        except Exception as exc:
            raise TransientError(f"failed to read invocation audit totals: {exc}") from exc

    async def legacy(self) -> tuple[bool, Usage]:
        # v1 DONE counts can include earlier failed retries and cannot be separated retrospectively.
        # Carry them conservatively once; preserve every original row for operator inspection.
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(ReviewChunkCheckpoint).where(
                        ReviewChunkCheckpoint.job_id == self.job_id,
                    )
                )
            ).all()
        usage = Usage()
        for row in rows:
            if row.status == "DONE":
                usage.add(
                    Usage(
                        llm_calls=row.llm_calls,
                        tool_calls=row.tool_calls,
                        input_tokens=row.input_tokens,
                        output_tokens=row.output_tokens,
                    )
                )
        return bool(rows), usage
