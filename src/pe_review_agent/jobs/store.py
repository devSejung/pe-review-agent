from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from pe_review_agent.domain import (
    TERMINAL_JOB_STATES,
    AttemptStage,
    DiffSide,
    Finding,
    FindingLineage,
    FindingLocation,
    GerritPatchsetEvent,
    JobIdentity,
    JobState,
    ReviewResult,
    Severity,
)
from pe_review_agent.jobs.models import (
    Attempt,
    Job,
    Publication,
    PublicationStatus,
    ReviewFinding,
    ReviewResultRow,
    ServiceState,
)
from pe_review_agent.jobs.state_machine import require_transition, valid_retry_target
from pe_review_agent.review.lineage import FindingHistory


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: uuid.UUID
    project: str
    change_number: int
    patchset_number: int
    revision_sha: str
    review_policy_version: str
    state: JobState
    retry_state: JobState | None
    attempt_count: int
    next_attempt_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    event_payload: dict[str, Any]
    superseded_by_job_id: uuid.UUID | None = None
    retry_epoch_start_attempt: int = 0


class PublishGuardStatus(StrEnum):
    OK = "OK"
    SUPERSEDED = "SUPERSEDED"
    LEASE_LOST = "LEASE_LOST"


def _enqueue_insert(event: GerritPatchsetEvent, policy_version: str):
    return (
        pg_insert(Job)
        .values(
            project=event.project,
            change_number=event.change_number,
            patchset_number=event.patchset_number,
            revision_sha=event.revision_sha,
            review_policy_version=policy_version,
            state=JobState.RECEIVED.value,
            event_payload=event.model_dump(mode="json"),
        )
        .on_conflict_do_nothing(
            index_elements=[
                Job.project,
                Job.change_number,
                Job.revision_sha,
                Job.review_policy_version,
            ]
        )
        .returning(Job.id)
    )


def _claim_select(now: datetime):
    terminal = [state.value for state in TERMINAL_JOB_STATES]
    older = aliased(Job)
    unresolved_older_publication = exists(
        select(1)
        .select_from(older)
        .where(
            older.project == Job.project,
            older.change_number == Job.change_number,
            older.patchset_number < Job.patchset_number,
            or_(
                older.state == JobState.PUBLISHING.value,
                and_(
                    older.state == JobState.RETRY_WAIT.value,
                    older.retry_state == JobState.PUBLISHING.value,
                ),
            ),
        )
    )
    return (
        select(Job)
        .where(
            Job.state.not_in(terminal),
            or_(Job.lease_expires_at.is_(None), Job.lease_expires_at <= now),
            or_(
                Job.state != JobState.RETRY_WAIT.value,
                Job.next_attempt_at <= now,
            ),
            ~unresolved_older_publication,
        )
        .order_by(Job.next_attempt_at.asc(), Job.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )


def _change_lock_select(project: str, change_number: int):
    key = f"{project}:{change_number}"
    return select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0)))


def _count_attempts_select(job_id: uuid.UUID, stage: AttemptStage):
    return (
        select(func.count())
        .select_from(Attempt)
        .where(
            Attempt.job_id == job_id,
            Attempt.stage == stage.value,
        )
    )


def _count_consumed_retry_attempts_select(job_id: uuid.UUID, stage: AttemptStage):
    """Count failed or crash-abandoned attempts, excluding completed successful work."""
    return (
        select(func.count())
        .select_from(Attempt)
        .join(Job, Job.id == Attempt.job_id)
        .where(
            Attempt.job_id == job_id,
            Attempt.stage == stage.value,
            Attempt.attempt_number > Job.retry_epoch_start_attempt,
            or_(Attempt.success.is_(False), Attempt.finished_at.is_(None)),
        )
    )


def publication_fingerprint(job: JobRecord, payload: dict[str, Any]) -> str:
    body = {
        "identity": {
            "project": job.project,
            "change_number": job.change_number,
            "revision_sha": job.revision_sha,
            "review_policy_version": job.review_policy_version,
        },
        "payload": payload,
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


class JobStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def enqueue(
        self, event: GerritPatchsetEvent, *, review_policy_version: str
    ) -> tuple[JobRecord, bool]:
        identity = JobIdentity(
            project=event.project,
            change_number=event.change_number,
            revision_sha=event.revision_sha,
            review_policy_version=review_policy_version,
        )
        async with self._sessions.begin() as session:
            await session.execute(_change_lock_select(event.project, event.change_number))
            inserted_id = await session.scalar(_enqueue_insert(event, review_policy_version))
            created = inserted_id is not None
            if inserted_id is None:
                job = await session.scalar(
                    select(Job).where(
                        Job.project == identity.project,
                        Job.change_number == identity.change_number,
                        Job.revision_sha == identity.revision_sha,
                        Job.review_policy_version == identity.review_policy_version,
                    )
                )
                if job is None:
                    raise RuntimeError("idempotent enqueue conflict row disappeared")
            else:
                job = await session.get(Job, inserted_id)
                if job is None:
                    raise RuntimeError("newly enqueued job disappeared")

            await self._apply_patchset_ordering(session, job)
            await session.flush()
            return _record(job), created

    async def _apply_patchset_ordering(self, session: AsyncSession, job: Job) -> None:
        terminal = [state.value for state in TERMINAL_JOB_STATES]
        newer_id = await session.scalar(
            select(Job.id)
            .where(
                Job.project == job.project,
                Job.change_number == job.change_number,
                Job.patchset_number > job.patchset_number,
            )
            .order_by(Job.patchset_number.desc(), Job.created_at.desc())
            .limit(1)
        )
        if newer_id is not None and job.state not in terminal:
            if _needs_publish_recovery(job):
                # A ReviewInput may already have reached Gerrit while the local response/DB commit
                # is still outstanding. Preserve this job as reclaimable so a later worker can ask
                # Gerrit whether the side effect happened before deciding SUPERSEDED vs DONE.
                job.superseded_by_job_id = newer_id
                return
            job.state = JobState.SUPERSEDED.value
            job.superseded_by_job_id = newer_id
            job.lease_owner = None
            job.lease_expires_at = None
            return

        await session.execute(
            update(Job)
            .where(
                Job.project == job.project,
                Job.change_number == job.change_number,
                Job.patchset_number < job.patchset_number,
                Job.state.not_in(terminal),
                or_(
                    Job.state == JobState.PUBLISHING.value,
                    and_(
                        Job.state == JobState.RETRY_WAIT.value,
                        Job.retry_state == JobState.PUBLISHING.value,
                    ),
                ),
            )
            .values(
                superseded_by_job_id=job.id,
                updated_at=func.now(),
            )
        )

        await session.execute(
            update(Job)
            .where(
                Job.project == job.project,
                Job.change_number == job.change_number,
                Job.patchset_number < job.patchset_number,
                Job.state.not_in(terminal),
                ~or_(
                    Job.state == JobState.PUBLISHING.value,
                    and_(
                        Job.state == JobState.RETRY_WAIT.value,
                        Job.retry_state == JobState.PUBLISHING.value,
                    ),
                ),
            )
            .values(
                state=JobState.SUPERSEDED.value,
                superseded_by_job_id=job.id,
                lease_owner=None,
                lease_expires_at=None,
                retry_state=None,
                updated_at=func.now(),
            )
        )

    async def get(self, job_id: uuid.UUID) -> JobRecord | None:
        async with self._sessions() as session:
            job = await session.get(Job, job_id)
            return _record(job) if job is not None else None

    async def get_service_watermark(self, key: str) -> datetime | None:
        async with self._sessions() as session:
            return await session.scalar(
                select(ServiceState.timestamp_value).where(ServiceState.key == key)
            )

    async def advance_service_watermark(self, key: str, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("service watermark must be timezone-aware")
        statement = pg_insert(ServiceState).values(
            key=key,
            timestamp_value=value,
            json_value={},
        )
        statement = statement.on_conflict_do_update(
            index_elements=[ServiceState.key],
            set_={
                "timestamp_value": statement.excluded.timestamp_value,
                "updated_at": func.now(),
            },
            where=or_(
                ServiceState.timestamp_value.is_(None),
                ServiceState.timestamp_value < statement.excluded.timestamp_value,
            ),
        )
        async with self._sessions.begin() as session:
            await session.execute(statement)

    async def claim_next(self, *, worker_id: str, lease_seconds: int) -> JobRecord | None:
        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=lease_seconds)
        async with self._sessions.begin() as session:
            job = await session.scalar(_claim_select(now))
            if job is None:
                return None
            if job.state == JobState.RETRY_WAIT.value:
                if not job.retry_state:
                    job.state = JobState.FAILED_PERMANENT.value
                    job.last_error = "RETRY_WAIT job has no retry_state"
                    job.last_error_class = "InvalidPersistedState"
                    return None
                job.state = job.retry_state
                job.retry_state = None
            job.lease_owner = worker_id
            job.claimed_at = now
            job.lease_expires_at = lease_until
            await session.flush()
            return _record(job)

    async def heartbeat(self, job_id: uuid.UUID, *, worker_id: str, lease_seconds: int) -> bool:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.lease_owner == worker_id,
                    Job.lease_expires_at > now,
                    Job.state.not_in([state.value for state in TERMINAL_JOB_STATES]),
                )
                .values(
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    updated_at=func.now(),
                )
            )
            return result.rowcount == 1

    async def refresh_publish_guard(
        self,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> PublishGuardStatus:
        """Atomically prove ownership/latest-PS status and extend the publish lease.

        This is called immediately before the Gerrit POST. Extending the lease here prevents an
        expired/stale worker and a replacement worker from both publishing the same durable payload.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            identity = await session.execute(
                select(Job.project, Job.change_number).where(Job.id == job_id)
            )
            row = identity.one_or_none()
            if row is None:
                return PublishGuardStatus.LEASE_LOST
            await session.execute(_change_lock_select(row.project, row.change_number))
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if job is None:
                return PublishGuardStatus.LEASE_LOST
            if (
                job.state != JobState.PUBLISHING.value
                or job.lease_owner != worker_id
                or job.lease_expires_at is None
                or job.lease_expires_at <= now
            ):
                return PublishGuardStatus.LEASE_LOST
            newer = await session.scalar(
                select(Job.id)
                .where(
                    Job.project == job.project,
                    Job.change_number == job.change_number,
                    Job.patchset_number > job.patchset_number,
                )
                .limit(1)
            )
            if newer is not None:
                return PublishGuardStatus.SUPERSEDED
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.updated_at = func.now()
            await session.flush()
            return PublishGuardStatus.OK

    async def transition(
        self,
        job_id: uuid.UUID,
        target: JobState,
        *,
        worker_id: str,
        release_lease: bool = False,
    ) -> JobRecord:
        async with self._sessions.begin() as session:
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if job is None:
                raise KeyError(job_id)
            _require_live_lease(job, worker_id)
            current = JobState(job.state)
            require_transition(current, target)
            job.state = target.value
            job.retry_state = None
            if release_lease or target in TERMINAL_JOB_STATES:
                job.lease_owner = None
                job.lease_expires_at = None
            await session.flush()
            return _record(job)

    async def schedule_retry(
        self,
        job_id: uuid.UUID,
        *,
        resume_state: JobState,
        retry_at: datetime,
        error: BaseException | str,
        worker_id: str,
    ) -> JobRecord:
        if not valid_retry_target(resume_state):
            raise ValueError(f"invalid retry target: {resume_state.value}")
        async with self._sessions.begin() as session:
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if job is None:
                raise KeyError(job_id)
            _require_live_lease(job, worker_id)
            require_transition(JobState(job.state), JobState.RETRY_WAIT)
            job.state = JobState.RETRY_WAIT.value
            job.retry_state = resume_state.value
            job.next_attempt_at = retry_at
            job.last_error_class = (
                error.__class__.__name__ if isinstance(error, BaseException) else None
            )
            job.last_error = str(error)
            job.lease_owner = None
            job.lease_expires_at = None
            await session.flush()
            return _record(job)

    async def mark_failed_permanent(
        self,
        job_id: uuid.UUID,
        *,
        error: BaseException | str,
        worker_id: str,
    ) -> JobRecord:
        async with self._sessions.begin() as session:
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if job is None:
                raise KeyError(job_id)
            _require_live_lease(job, worker_id)
            require_transition(JobState(job.state), JobState.FAILED_PERMANENT)
            job.state = JobState.FAILED_PERMANENT.value
            job.retry_state = None
            job.last_error_class = (
                error.__class__.__name__ if isinstance(error, BaseException) else None
            )
            job.last_error = str(error)
            job.lease_owner = None
            job.lease_expires_at = None
            await session.flush()
            return _record(job)

    async def mark_superseded(
        self,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        superseded_by_job_id: uuid.UUID | None = None,
    ) -> JobRecord:
        async with self._sessions.begin() as session:
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if job is None:
                raise KeyError(job_id)
            if JobState(job.state) == JobState.SUPERSEDED:
                return _record(job)
            _require_live_lease(job, worker_id)
            require_transition(JobState(job.state), JobState.SUPERSEDED)
            job.state = JobState.SUPERSEDED.value
            job.superseded_by_job_id = superseded_by_job_id
            job.retry_state = None
            job.lease_owner = None
            job.lease_expires_at = None
            await session.flush()
            return _record(job)

    async def mark_done(self, job_id: uuid.UUID, *, worker_id: str) -> JobRecord:
        return await self.transition(job_id, JobState.DONE, worker_id=worker_id, release_lease=True)

    async def requeue_failed(self, job_id: uuid.UUID) -> JobRecord:
        """Administratively retry a failed job without erasing its attempt audit history.

        Retry accounting starts a new epoch at the current durable attempt number. If a review or
        publication intent already exists, resume from that durable phase instead of invoking the
        model again. A stale Patch Set is never reopened.
        """

        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            identity = await session.execute(
                select(Job.project, Job.change_number).where(Job.id == job_id)
            )
            row = identity.one_or_none()
            if row is None:
                raise KeyError(job_id)
            await session.execute(_change_lock_select(row.project, row.change_number))
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if job is None:
                raise KeyError(job_id)

            state = JobState(job.state)
            if state is not JobState.FAILED_PERMANENT:
                raise RuntimeError(
                    f"job {job_id} is {state.value}; only FAILED_PERMANENT jobs can be requeued"
                )
            newer = await session.scalar(
                select(Job.id)
                .where(
                    Job.project == job.project,
                    Job.change_number == job.change_number,
                    Job.patchset_number > job.patchset_number,
                )
                .limit(1)
            )
            if newer is not None:
                raise RuntimeError(
                    f"job {job_id} is an older Patch Set and cannot be manually requeued"
                )

            publication = await session.scalar(
                select(Publication).where(Publication.job_id == job_id).with_for_update()
            )
            review_exists = (
                await session.scalar(
                    select(ReviewResultRow.job_id).where(ReviewResultRow.job_id == job_id)
                )
                is not None
            )

            if publication is not None and publication.status == PublicationStatus.POSTED.value:
                job.state = JobState.DONE.value
            elif publication is not None:
                if publication.status == PublicationStatus.FAILED.value:
                    publication.status = PublicationStatus.PENDING.value
                    publication.last_error = None
                    publication.gerrit_response = None
                    publication.posted_at = None
                    publication.updated_at = func.now()
                job.state = JobState.PUBLISHING.value
            elif review_exists:
                job.state = JobState.READY_TO_PUBLISH.value
            else:
                job.state = JobState.RECEIVED.value

            job.retry_epoch_start_attempt = job.attempt_count
            job.retry_state = None
            job.next_attempt_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            job.claimed_at = None
            job.last_error_class = None
            job.last_error = None
            job.updated_at = func.now()
            await session.flush()
            return _record(job)

    async def start_attempt(self, job_id: uuid.UUID, *, stage: AttemptStage, worker_id: str) -> int:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            attempt_number = await session.scalar(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.lease_owner == worker_id,
                    Job.lease_expires_at > now,
                )
                .values(attempt_count=Job.attempt_count + 1, updated_at=func.now())
                .returning(Job.attempt_count)
            )
            if attempt_number is None:
                raise RuntimeError(f"job {job_id} is missing or not leased by {worker_id}")
            attempt = Attempt(
                job_id=job_id,
                attempt_number=attempt_number,
                stage=stage.value,
                worker_id=worker_id,
            )
            session.add(attempt)
            await session.flush()
            return attempt.id

    async def count_attempts(self, job_id: uuid.UUID, *, stage: AttemptStage) -> int:
        """Return the durable number of started attempts for one retry-budget stage."""
        async with self._sessions() as session:
            count = await session.scalar(_count_attempts_select(job_id, stage))
            return int(count or 0)

    async def count_consumed_retry_attempts(self, job_id: uuid.UUID, *, stage: AttemptStage) -> int:
        """Return failures plus unfinished attempts left behind by crashes/lease loss."""
        async with self._sessions() as session:
            count = await session.scalar(_count_consumed_retry_attempts_select(job_id, stage))
            return int(count or 0)

    async def finish_attempt(
        self,
        attempt_id: int,
        *,
        success: bool,
        retryable: bool | None = None,
        error: BaseException | str | None = None,
    ) -> None:
        error_class = error.__class__.__name__ if isinstance(error, BaseException) else None
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(Attempt)
                .where(Attempt.id == attempt_id, Attempt.finished_at.is_(None))
                .values(
                    finished_at=func.now(),
                    success=success,
                    retryable=retryable,
                    error_class=error_class,
                    error_message=str(error) if error is not None else None,
                )
            )
            if result.rowcount != 1:
                raise RuntimeError(f"attempt {attempt_id} is missing or already finished")

    async def save_review_result_and_mark_ready(
        self,
        job_id: uuid.UUID,
        review: ReviewResult,
        *,
        worker_id: str,
    ) -> JobRecord:
        async with self._sessions.begin() as session:
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if job is None:
                raise KeyError(job_id)
            _require_live_lease(job, worker_id)

            current = JobState(job.state)
            if current == JobState.READY_TO_PUBLISH:
                existing = await session.get(ReviewResultRow, job_id)
                if existing is None:
                    raise RuntimeError("READY_TO_PUBLISH job is missing its durable review result")
                return _record(job)
            require_transition(current, JobState.READY_TO_PUBLISH)

            existing = await session.get(ReviewResultRow, job_id)
            if existing is None:
                result_row = ReviewResultRow(
                    job_id=job_id,
                    summary=review.summary,
                    model=review.model,
                    input_tokens=review.input_tokens,
                    output_tokens=review.output_tokens,
                    review_metadata=review.review_metadata,
                )
                session.add(result_row)
                seen: set[str] = set()
                for ordinal, finding in enumerate(review.findings):
                    finding.ensure_fingerprint(project=job.project)
                    finding.ensure_semantic_id(project=job.project)
                    assert finding.fingerprint is not None
                    assert finding.semantic_id is not None
                    if finding.fingerprint in seen:
                        continue
                    seen.add(finding.fingerprint)
                    session.add(
                        ReviewFinding(
                            job_id=job_id,
                            ordinal=ordinal,
                            fingerprint=finding.fingerprint,
                            semantic_id=finding.semantic_id,
                            lineage_state=(finding.lineage or FindingLineage.NEW).value,
                            severity=finding.severity.value,
                            category=finding.category,
                            title=finding.title,
                            message=finding.message,
                            impact=finding.impact,
                            evidence=finding.evidence,
                            remediation=finding.remediation,
                            path=finding.location.path,
                            side=finding.location.side.value,
                            start_line=finding.location.start_line,
                            start_character=finding.location.start_character,
                            end_line=finding.location.end_line,
                            end_character=finding.location.end_character,
                            confidence=finding.confidence,
                        )
                    )

            job.state = JobState.READY_TO_PUBLISH.value
            job.retry_state = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error = None
            job.last_error_class = None
            await session.flush()
            return _record(job)

    async def load_review_result(self, job_id: uuid.UUID) -> ReviewResult | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(ReviewResultRow).where(ReviewResultRow.job_id == job_id)
            )
            if row is None:
                return None
            findings = await session.scalars(
                select(ReviewFinding)
                .where(ReviewFinding.job_id == job_id)
                .order_by(ReviewFinding.ordinal.asc())
            )
            return ReviewResult(
                summary=row.summary,
                model=row.model,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                review_metadata=row.review_metadata,
                findings=[
                    Finding(
                        severity=Severity(finding.severity),
                        category=finding.category,
                        title=finding.title,
                        message=finding.message,
                        impact=finding.impact,
                        evidence=finding.evidence,
                        remediation=finding.remediation,
                        location=FindingLocation(
                            path=finding.path,
                            side=DiffSide(finding.side),
                            start_line=finding.start_line,
                            start_character=finding.start_character,
                            end_line=finding.end_line,
                            end_character=finding.end_character,
                        ),
                        confidence=finding.confidence,
                        fingerprint=finding.fingerprint,
                        semantic_id=finding.semantic_id,
                        lineage=FindingLineage(finding.lineage_state),
                    )
                    for finding in findings
                ],
            )

    async def load_finding_history(self, job_id: uuid.UUID) -> FindingHistory:
        """Load the last posted Patch Set plus all semantic IDs seen in posted history."""
        async with self._sessions() as session:
            job = await session.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            baseline_job = await session.scalar(
                select(Job)
                .join(ReviewResultRow, ReviewResultRow.job_id == Job.id)
                .where(
                    Job.project == job.project,
                    Job.change_number == job.change_number,
                    Job.review_policy_version == job.review_policy_version,
                    Job.patchset_number < job.patchset_number,
                    Job.state == JobState.DONE.value,
                    ReviewResultRow.review_metadata["lineage_complete"]
                    .as_boolean()
                    .is_not(False),
                )
                .order_by(Job.patchset_number.desc(), Job.created_at.desc())
                .limit(1)
            )
            previous: list[Finding] = []
            if baseline_job is not None:
                rows = await session.scalars(
                    select(ReviewFinding)
                    .where(ReviewFinding.job_id == baseline_job.id)
                    .order_by(ReviewFinding.ordinal.asc())
                )
                previous = [_finding_from_row(row) for row in rows]

            previous_ids = {
                finding.semantic_id for finding in previous if finding.semantic_id is not None
            }
            historical_rows = (
                await session.execute(
                    select(ReviewFinding, Job.patchset_number)
                    .join(Job, Job.id == ReviewFinding.job_id)
                    .where(
                        Job.project == job.project,
                        Job.change_number == job.change_number,
                        Job.review_policy_version == job.review_policy_version,
                        Job.patchset_number < job.patchset_number,
                        Job.state == JobState.DONE.value,
                    )
                    .order_by(Job.patchset_number.desc(), ReviewFinding.ordinal.asc())
                    .limit(200)
                )
            ).all()
            historical: list[Finding] = []
            historical_ids: set[str] = set()
            for finding_row, _patchset_number in historical_rows:
                semantic_id = finding_row.semantic_id
                if semantic_id in previous_ids or semantic_id in historical_ids:
                    continue
                historical_ids.add(semantic_id)
                historical.append(_finding_from_row(finding_row))
                if len(historical) >= 20:
                    break

            seen = await session.scalars(
                select(ReviewFinding.semantic_id)
                .join(Job, Job.id == ReviewFinding.job_id)
                .where(
                    Job.project == job.project,
                    Job.change_number == job.change_number,
                    Job.review_policy_version == job.review_policy_version,
                    Job.patchset_number < job.patchset_number,
                    Job.state == JobState.DONE.value,
                )
                .distinct()
            )
            return FindingHistory(
                baseline_patchset=baseline_job.patchset_number if baseline_job else None,
                previous_findings=tuple(previous),
                seen_semantic_ids=frozenset(seen),
                historical_findings=tuple(historical),
            )

    async def begin_publication(
        self,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        request_payload: dict[str, Any],
        finding_fingerprints: list[str],
    ) -> Publication:
        async with self._sessions.begin() as session:
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if job is None:
                raise KeyError(job_id)
            _require_live_lease(job, worker_id)
            if JobState(job.state) != JobState.PUBLISHING:
                raise RuntimeError(f"job {job_id} cannot begin publication from state {job.state}")
            record = _record(job)
            fingerprint = publication_fingerprint(record, request_payload)
            stmt = (
                pg_insert(Publication)
                .values(
                    job_id=job_id,
                    publication_fingerprint=fingerprint,
                    status=PublicationStatus.PENDING.value,
                    finding_fingerprints=sorted(set(finding_fingerprints)),
                    request_payload=request_payload,
                )
                .on_conflict_do_nothing(index_elements=[Publication.job_id])
                .returning(Publication.id)
            )
            publication_id = await session.scalar(stmt)
            if publication_id is None:
                publication = await session.scalar(
                    select(Publication).where(Publication.job_id == job_id)
                )
                if publication is None:
                    raise RuntimeError("publication conflict row disappeared")
                if publication.publication_fingerprint != fingerprint:
                    raise RuntimeError(
                        "publication payload changed after durable publication began"
                    )
                return publication
            publication = await session.get(Publication, publication_id)
            if publication is None:
                raise RuntimeError("new publication row disappeared")
            return publication

    async def complete_publication(
        self, publication_id: int, *, gerrit_response: dict[str, Any] | None = None
    ) -> None:
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(Publication)
                .where(Publication.id == publication_id)
                .values(
                    status=PublicationStatus.POSTED.value,
                    gerrit_response=gerrit_response,
                    posted_at=func.now(),
                    last_error=None,
                    updated_at=func.now(),
                )
            )
            if result.rowcount != 1:
                raise KeyError(publication_id)

    async def complete_publication_and_mark_done(
        self,
        publication_id: int,
        *,
        job_id: uuid.UUID,
        worker_id: str,
        gerrit_response: dict[str, Any] | None = None,
    ) -> JobRecord:
        """Atomically record Gerrit's side effect and the job's published terminal state.

        A newer Patch Set may arrive after Gerrit accepted the ReviewInput but before this local
        transaction. In that case the job can carry a supersession marker (or even have briefly been
        marked SUPERSEDED by older code); the externally observed publication still wins and must be
        recorded as DONE so future Patch Sets use it as their published baseline.
        """
        async with self._sessions.begin() as session:
            identity = await session.execute(
                select(Job.project, Job.change_number).where(Job.id == job_id)
            )
            row = identity.one_or_none()
            if row is None:
                raise KeyError(job_id)
            await session.execute(_change_lock_select(row.project, row.change_number))
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            publication = await session.scalar(
                select(Publication).where(Publication.id == publication_id).with_for_update()
            )
            if publication is None:
                raise KeyError(publication_id)
            if publication.job_id != job_id:
                raise RuntimeError("publication belongs to a different review job")
            if job is None:
                raise KeyError(job_id)

            state = JobState(job.state)
            if state == JobState.DONE and publication.status == PublicationStatus.POSTED.value:
                return _record(job)
            if state not in {JobState.PUBLISHING, JobState.SUPERSEDED}:
                raise RuntimeError(
                    f"cannot finalize Gerrit publication for job {job_id} from {state.value}"
                )
            if state == JobState.PUBLISHING and job.lease_owner not in {None, worker_id}:
                raise RuntimeError(
                    f"job {job_id} publish lease is owned by {job.lease_owner}, not {worker_id}"
                )

            publication.status = PublicationStatus.POSTED.value
            publication.gerrit_response = gerrit_response
            publication.posted_at = func.now()
            publication.last_error = None
            publication.updated_at = func.now()

            job.state = JobState.DONE.value
            job.retry_state = None
            job.last_error_class = None
            job.last_error = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = func.now()
            await session.flush()
            return _record(job)

    async def mark_publication_ambiguous(self, publication_id: int, *, error: str) -> None:
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(Publication)
                .where(Publication.id == publication_id)
                .values(
                    status=PublicationStatus.AMBIGUOUS.value,
                    last_error=error,
                    updated_at=func.now(),
                )
            )
            if result.rowcount != 1:
                raise KeyError(publication_id)

    async def mark_publication_failed(self, publication_id: int, *, error: str) -> None:
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(Publication)
                .where(Publication.id == publication_id)
                .values(
                    status=PublicationStatus.FAILED.value,
                    last_error=error,
                    updated_at=func.now(),
                )
            )
            if result.rowcount != 1:
                raise KeyError(publication_id)

    async def publication_for_job(self, job_id: uuid.UUID) -> Publication | None:
        async with self._sessions() as session:
            return await session.scalar(select(Publication).where(Publication.job_id == job_id))

    async def is_latest_known_patchset(self, job_id: uuid.UUID) -> bool:
        """Cheap DB guard for publication; Gerrit must still be checked before the REST post."""
        async with self._sessions() as session:
            job = await session.get(Job, job_id)
            if job is None or JobState(job.state) == JobState.SUPERSEDED:
                return False
            newer = await session.scalar(
                select(Job.id)
                .where(
                    Job.project == job.project,
                    Job.change_number == job.change_number,
                    Job.patchset_number > job.patchset_number,
                )
                .limit(1)
            )
            return newer is None

    async def pending_depth(self) -> int:
        """Count queued/retry-wait/reclaimable jobs for the operational queue gauge."""
        now = datetime.now(UTC)
        terminal = [state.value for state in TERMINAL_JOB_STATES]
        async with self._sessions() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(Job)
                .where(
                    Job.state.not_in(terminal),
                    or_(
                        Job.state == JobState.RETRY_WAIT.value,
                        Job.lease_owner.is_(None),
                        Job.lease_expires_at.is_(None),
                        Job.lease_expires_at <= now,
                    ),
                )
            )
            return int(count or 0)


def _record(job: Job) -> JobRecord:
    return JobRecord(
        id=job.id,
        project=job.project,
        change_number=job.change_number,
        patchset_number=job.patchset_number,
        revision_sha=job.revision_sha,
        review_policy_version=job.review_policy_version,
        state=JobState(job.state),
        retry_state=JobState(job.retry_state) if job.retry_state else None,
        attempt_count=job.attempt_count,
        next_attempt_at=job.next_attempt_at,
        lease_owner=job.lease_owner,
        lease_expires_at=job.lease_expires_at,
        superseded_by_job_id=job.superseded_by_job_id,
        event_payload=job.event_payload,
        retry_epoch_start_attempt=job.retry_epoch_start_attempt,
    )


def _finding_from_row(finding: ReviewFinding) -> Finding:
    return Finding(
        severity=Severity(finding.severity),
        category=finding.category,
        title=finding.title,
        message=finding.message,
        impact=finding.impact,
        evidence=finding.evidence,
        remediation=finding.remediation,
        location=FindingLocation(
            path=finding.path,
            side=DiffSide(finding.side),
            start_line=finding.start_line,
            start_character=finding.start_character,
            end_line=finding.end_line,
            end_character=finding.end_character,
        ),
        confidence=finding.confidence,
        fingerprint=finding.fingerprint,
        semantic_id=finding.semantic_id,
        lineage=FindingLineage(finding.lineage_state),
    )


def _needs_publish_recovery(job: Job) -> bool:
    return job.state == JobState.PUBLISHING.value or (
        job.state == JobState.RETRY_WAIT.value and job.retry_state == JobState.PUBLISHING.value
    )


def _require_live_lease(job: Job, worker_id: str) -> None:
    now = datetime.now(UTC)
    if job.lease_owner != worker_id:
        raise RuntimeError(f"job {job.id} is not leased by {worker_id}")
    if job.lease_expires_at is None or job.lease_expires_at <= now:
        raise RuntimeError(f"job {job.id} lease for {worker_id} has expired")
