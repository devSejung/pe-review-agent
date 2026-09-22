from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pe_review_agent.domain import JobState
from pe_review_agent.jobs.models import Job, Publication, ReviewVote
from pe_review_agent.jobs.store import JobRecord, _require_live_lease
from pe_review_agent.review.project_policy import VoteDecision

TERMINAL_VOTE_STATES = frozenset({"APPLIED", "FAILED", "SKIPPED"})


class VoteStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def ensure(
        self,
        job: JobRecord,
        *,
        worker_id: str,
        decision: VoteDecision,
        payload: dict[str, Any],
    ) -> ReviewVote:
        async with self.sessions.begin() as session:
            await self._guard(session, job.id, worker_id)
            row = await session.get(ReviewVote, job.id)
            if row is None:
                row = ReviewVote(
                    job_id=job.id,
                    value=decision.value,
                    reason=decision.reason,
                    finding_count=decision.finding_count,
                    status="SKIPPED" if decision.value is None else "PENDING",
                    request_payload=payload,
                    events=[],
                )
                session.add(row)
                await session.flush()
            return row

    async def get(self, job_id: uuid.UUID) -> ReviewVote | None:
        async with self.sessions() as session:
            return await session.get(ReviewVote, job_id)

    async def dispatch(
        self,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        account_id: int,
    ) -> ReviewVote:
        async with self.sessions.begin() as session:
            await self._guard(session, job_id, worker_id)
            row = await session.get(ReviewVote, job_id, with_for_update=True)
            if row is None or row.status in TERMINAL_VOTE_STATES:
                raise RuntimeError("vote is missing or already terminal")
            if row.account_id is not None and row.account_id != account_id:
                raise RuntimeError("vote account identity cannot change")
            row.account_id = account_id
            row.attempts += 1
            row.status = "AMBIGUOUS"
            row.events = [
                *row.events,
                {
                    "event": "post_started",
                    "attempt": row.attempts,
                    "account_id": account_id,
                    "ts": datetime.now(UTC).isoformat(),
                },
            ]
            row.updated_at = datetime.now(UTC)
            await session.flush()
            return row

    async def retry(
        self,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        error: str,
    ) -> ReviewVote:
        async with self.sessions.begin() as session:
            await self._guard(session, job_id, worker_id)
            row = await session.get(ReviewVote, job_id, with_for_update=True)
            if row is None:
                raise KeyError(job_id)
            row.retry_count += 1
            row.last_error = error[:2000]
            row.updated_at = datetime.now(UTC)
            row.events = [
                *row.events,
                {
                    "event": "retry",
                    "count": row.retry_count,
                    "error": error[:1000],
                    "ts": datetime.now(UTC).isoformat(),
                },
            ]
            await session.flush()
            return row

    async def finish(
        self,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        status: str,
        response: dict[str, Any] | None = None,
        error: str | None = None,
        account_id: int | None = None,
    ) -> None:
        if status not in TERMINAL_VOTE_STATES:
            raise ValueError("vote completion requires a terminal status")
        async with self.sessions.begin() as session:
            job = await self._guard(session, job_id, worker_id)
            publication = await session.scalar(
                select(Publication).where(Publication.job_id == job_id)
            )
            if publication is None or publication.status != "POSTED":
                raise RuntimeError("review comments must be durable before completing a vote")
            row = await session.get(ReviewVote, job_id, with_for_update=True)
            if row is None:
                raise KeyError(job_id)
            if row.status not in TERMINAL_VOTE_STATES:
                if account_id is not None:
                    if row.account_id is not None and row.account_id != account_id:
                        raise RuntimeError("vote account identity cannot change")
                    row.account_id = account_id
                row.events = [
                    *row.events,
                    {
                        "event": status.lower(),
                        "detail": error,
                        "ts": datetime.now(UTC).isoformat(),
                    },
                ]
                row.status = status
                row.response = response
                row.last_error = error
                row.updated_at = datetime.now(UTC)
            job.state = JobState.DONE.value
            job.retry_state = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error = None
            job.last_error_class = None
            job.updated_at = datetime.now(UTC)

    @staticmethod
    async def _guard(session: AsyncSession, job_id: uuid.UUID, worker_id: str) -> Job:
        job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None:
            raise KeyError(job_id)
        _require_live_lease(job, worker_id)
        if job.state != JobState.PUBLISHING.value:
            raise RuntimeError("vote requires a live PUBLISHING job")
        return job
