from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from pe_review_agent.config import Settings
from pe_review_agent.db import Database
from pe_review_agent.domain import AttemptStage, GerritPatchsetEvent, JobState, ReviewResult
from pe_review_agent.gerrit import (
    GerritEventStream,
    GerritRestClient,
    SupersededRevisionError,
    build_review_input,
)
from pe_review_agent.jobs import JobRecord, JobStore, PublicationStatus
from pe_review_agent.llm import LlmClient
from pe_review_agent.observability import METRICS, log_event
from pe_review_agent.repos import RepositoryManager, RepositoryToolExecutor
from pe_review_agent.retry import PermanentError, TransientError, exponential_backoff
from pe_review_agent.review import NativeFirmwareReviewEngine
from pe_review_agent.review.policy import load_policy

logger = logging.getLogger(__name__)


class LeaseLostError(RuntimeError):
    pass


@dataclass(slots=True)
class _Attempt:
    id: int
    stage: AttemptStage


class LeaseGuard:
    def __init__(self, store: JobStore, job: JobRecord, worker_id: str, lease_seconds: int) -> None:
        self.store = store
        self.job = job
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.lost = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> LeaseGuard:
        self._task = asyncio.create_task(self._heartbeat_loop())
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    def ensure(self) -> None:
        if self.lost.is_set():
            raise LeaseLostError(f"job {self.job.id} lost worker lease")

    async def _heartbeat_loop(self) -> None:
        interval = max(5.0, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                alive = await self.store.heartbeat(
                    self.job.id,
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                )
            except Exception:
                logger.exception("job lease heartbeat failed", extra={"job_id": str(self.job.id)})
                alive = False
            if not alive:
                self.lost.set()
                return


class ReviewWorker:
    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        gerrit: GerritRestClient,
        repos: RepositoryManager,
        engine: NativeFirmwareReviewEngine,
    ) -> None:
        self.settings = settings
        self.store = store
        self.gerrit = gerrit
        self.repos = repos
        self.engine = engine

    async def run_forever(self) -> None:
        tasks = [
            asyncio.create_task(self._slot(index), name=f"review-worker-{index}")
            for index in range(self.settings.service.worker_concurrency)
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _slot(self, index: int) -> None:
        worker_id = f"{uuid.uuid4()}:{index}"
        while True:
            job = await self.store.claim_next(
                worker_id=worker_id,
                lease_seconds=self.settings.service.claim_lease_seconds,
            )
            if job is None:
                await asyncio.sleep(self.settings.service.poll_interval_seconds)
                continue
            try:
                async with LeaseGuard(
                    self.store,
                    job,
                    worker_id,
                    self.settings.service.claim_lease_seconds,
                ) as lease:
                    await self._process_claimed(job, worker_id=worker_id, lease=lease)
            except LeaseLostError as exc:
                logger.warning("review job lease lost; another worker may reclaim it: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "unexpected review job failure",
                    extra={"job_id": str(job.id), "project": job.project},
                )

    async def _process_claimed(self, job: JobRecord, *, worker_id: str, lease: LeaseGuard) -> None:
        log_event(
            logger,
            "processing review job",
            job_id=str(job.id),
            project=job.project,
            change=job.change_number,
            patchset=job.patchset_number,
            revision=job.revision_sha,
            state=job.state.value,
        )
        if job.state in {
            JobState.RECEIVED,
            JobState.FETCHING,
            JobState.REVIEWING,
            JobState.VALIDATING,
        }:
            await self._process_review(job, worker_id=worker_id, lease=lease)
            return
        if job.state in {JobState.READY_TO_PUBLISH, JobState.PUBLISHING}:
            await self._process_publish(job, worker_id=worker_id, lease=lease)
            return
        if job.state in {JobState.DONE, JobState.SUPERSEDED, JobState.FAILED_PERMANENT}:
            return
        await self._permanent_failure(
            job,
            worker_id=worker_id,
            error=PermanentError(f"unsupported claimed state {job.state.value}"),
        )

    async def _process_review(self, job: JobRecord, *, worker_id: str, lease: LeaseGuard) -> None:
        if job.state == JobState.RECEIVED:
            job = await self.store.transition(job.id, JobState.FETCHING, worker_id=worker_id)

        event = GerritPatchsetEvent.model_validate(job.event_payload)
        fetch_attempt = await self._start_attempt(job, AttemptStage.FETCH, worker_id)
        failure_stage = AttemptStage.FETCH
        try:
            change = await self.gerrit.ensure_current_revision(
                job.project, job.change_number, job.revision_sha
            )
            async with self.repos.workspace(
                project=job.project,
                change_number=job.change_number,
                revision_sha=job.revision_sha,
                ref=event.ref or change.ref,
            ) as workspace:
                lease.ensure()
                await self._finish_attempt(fetch_attempt, success=True)
                if job.state == JobState.FETCHING:
                    job = await self.store.transition(
                        job.id, JobState.REVIEWING, worker_id=worker_id
                    )

                failure_stage = AttemptStage.REVIEW
                policy_text = load_policy(workspace.root, self.settings.review)
                context = self.repos.to_review_context(
                    workspace,
                    change_number=job.change_number,
                    patchset_number=job.patchset_number,
                    subject=change.subject,
                    branch=change.branch,
                    policy_text=policy_text,
                )
                tools = RepositoryToolExecutor(workspace.root, self.settings.review)
                review_attempt = await self._start_attempt(job, AttemptStage.REVIEW, worker_id)
                try:
                    review = await self.engine.review(context, tools)
                    lease.ensure()
                    await self._finish_attempt(review_attempt, success=True)
                except Exception as exc:
                    await self._finish_attempt(
                        review_attempt,
                        success=False,
                        retryable=isinstance(exc, TransientError),
                        error=exc,
                    )
                    raise

                # The engine performs model verification and static location validation. The
                # explicit VALIDATING state makes recovery semantics visible and leaves room for
                # additional deterministic validators without coupling them to Gerrit publishing.
                if job.state == JobState.REVIEWING:
                    job = await self.store.transition(
                        job.id, JobState.VALIDATING, worker_id=worker_id
                    )
                lease.ensure()
                await self.store.save_review_result_and_mark_ready(
                    job.id, review, worker_id=worker_id
                )
                self._record_review_metrics(job, review)
        except SupersededRevisionError:
            await self._finish_attempt_if_open(fetch_attempt, success=True)
            await self.store.mark_superseded(job.id)
            METRICS.superseded_total.labels(project=job.project).inc()
        except TransientError as exc:
            await self._finish_attempt_if_open(
                fetch_attempt, success=False, retryable=True, error=exc
            )
            await self._schedule_retry(job.id, failure_stage, worker_id, exc)
        except PermanentError as exc:
            await self._finish_attempt_if_open(
                fetch_attempt, success=False, retryable=False, error=exc
            )
            await self._permanent_failure(job, worker_id=worker_id, error=exc)

    async def _process_publish(self, job: JobRecord, *, worker_id: str, lease: LeaseGuard) -> None:
        if not await self.store.is_latest_known_patchset(job.id):
            await self.store.mark_superseded(job.id)
            METRICS.superseded_total.labels(project=job.project).inc()
            return

        review = await self.store.load_review_result(job.id)
        if review is None:
            await self._permanent_failure(
                job,
                worker_id=worker_id,
                error=PermanentError("publishable job is missing durable review result"),
            )
            return

        if job.state == JobState.READY_TO_PUBLISH:
            job = await self.store.transition(job.id, JobState.PUBLISHING, worker_id=worker_id)

        payload = build_review_input(
            review,
            tag=self.settings.gerrit.review_tag,
            notify=self.settings.gerrit.notify,
        )
        publication = await self.store.begin_publication(
            job.id,
            worker_id=worker_id,
            request_payload=payload,
            finding_fingerprints=[finding.fingerprint or "" for finding in review.findings],
        )
        if publication.status == PublicationStatus.POSTED.value:
            await self.store.mark_done(job.id, worker_id=worker_id)
            METRICS.success_total.labels(project=job.project).inc()
            return

        attempt = await self._start_attempt(job, AttemptStage.PUBLISH, worker_id)
        try:
            lease.ensure()
            await self.gerrit.ensure_current_revision(
                job.project, job.change_number, job.revision_sha
            )

            # PENDING is also treated as ambiguous on recovery: the process might have crashed
            # after Gerrit committed the POST but before the DB status update.
            already_posted = await self.gerrit.has_published_review(
                project=job.project,
                change_number=job.change_number,
                patchset_number=job.patchset_number,
                summary=review.summary,
            )
            if already_posted:
                await self.store.complete_publication(
                    publication.id, gerrit_response={"recovered": True}
                )
                await self._finish_attempt(attempt, success=True)
                lease.ensure()
                await self.store.mark_done(job.id, worker_id=worker_id)
                METRICS.success_total.labels(project=job.project).inc()
                return

            lease.ensure()
            response = await self.gerrit.publish_review_input(
                project=job.project,
                change_number=job.change_number,
                revision_sha=job.revision_sha,
                payload=publication.request_payload,
            )
            await self.store.complete_publication(publication.id, gerrit_response=response)
            await self._finish_attempt(attempt, success=True)
            lease.ensure()
            await self.store.mark_done(job.id, worker_id=worker_id)
            METRICS.success_total.labels(project=job.project).inc()
        except SupersededRevisionError:
            await self._finish_attempt_if_open(attempt, success=True)
            await self.store.mark_superseded(job.id)
            METRICS.superseded_total.labels(project=job.project).inc()
        except TransientError as exc:
            await self.store.mark_publication_ambiguous(publication.id, error=str(exc))
            await self._finish_attempt_if_open(attempt, success=False, retryable=True, error=exc)
            await self._schedule_retry(job.id, AttemptStage.PUBLISH, worker_id, exc)
        except PermanentError as exc:
            await self.store.mark_publication_failed(publication.id, error=str(exc))
            await self._finish_attempt_if_open(attempt, success=False, retryable=False, error=exc)
            await self._permanent_failure(job, worker_id=worker_id, error=exc)

    async def _start_attempt(self, job: JobRecord, stage: AttemptStage, worker_id: str) -> _Attempt:
        return _Attempt(
            id=await self.store.start_attempt(job.id, stage=stage, worker_id=worker_id),
            stage=stage,
        )

    async def _finish_attempt(
        self,
        attempt: _Attempt,
        *,
        success: bool,
        retryable: bool | None = None,
        error: BaseException | str | None = None,
    ) -> None:
        await self.store.finish_attempt(
            attempt.id, success=success, retryable=retryable, error=error
        )

    async def _finish_attempt_if_open(
        self,
        attempt: _Attempt,
        *,
        success: bool,
        retryable: bool | None = None,
        error: BaseException | str | None = None,
    ) -> None:
        try:
            await self._finish_attempt(attempt, success=success, retryable=retryable, error=error)
        except RuntimeError as exc:
            if "already finished" not in str(exc):
                raise

    async def _schedule_retry(
        self,
        job_id: uuid.UUID,
        stage: AttemptStage,
        worker_id: str,
        error: TransientError,
    ) -> None:
        job = await self.store.get(job_id)
        if job is None:
            return
        stage_attempts = await self.store.count_attempts(job_id, stage=stage)
        budget = {
            AttemptStage.FETCH: self.settings.retry.fetch_attempts,
            AttemptStage.REVIEW: self.settings.retry.review_attempts,
            AttemptStage.VALIDATE: self.settings.retry.review_attempts,
            AttemptStage.PUBLISH: self.settings.retry.publish_attempts,
            AttemptStage.RECONCILE: self.settings.retry.fetch_attempts,
        }[stage]
        if stage_attempts >= budget:
            await self.store.mark_failed_permanent(
                job_id,
                worker_id=worker_id,
                error=PermanentError(
                    f"{stage.value} retry budget exhausted after {stage_attempts} attempts: {error}"
                ),
            )
            METRICS.failed_total.labels(project=job.project, stage=stage.value).inc()
            return

        retry_after = error.retry_after_seconds
        if retry_after is None:
            retry_after = exponential_backoff(
                stage_attempts,
                base_seconds=self.settings.retry.base_seconds,
                max_seconds=self.settings.retry.max_seconds,
                jitter_ratio=self.settings.retry.jitter_ratio,
            )
        current = await self.store.get(job_id)
        if current is None:
            return
        resume = current.state
        if resume not in {
            JobState.FETCHING,
            JobState.REVIEWING,
            JobState.VALIDATING,
            JobState.READY_TO_PUBLISH,
            JobState.PUBLISHING,
        }:
            raise RuntimeError(f"cannot retry job {job_id} from {resume.value}")
        await self.store.schedule_retry(
            job_id,
            resume_state=resume,
            retry_at=datetime.now(UTC) + timedelta(seconds=retry_after),
            error=error,
            worker_id=worker_id,
        )
        METRICS.stage_retries_total.labels(stage=stage.value, reason=type(error).__name__).inc()

    async def _permanent_failure(
        self, job: JobRecord, *, worker_id: str, error: PermanentError
    ) -> None:
        current = await self.store.get(job.id)
        if current is None or current.state in {
            JobState.DONE,
            JobState.SUPERSEDED,
            JobState.FAILED_PERMANENT,
        }:
            return
        await self.store.mark_failed_permanent(job.id, error=error, worker_id=worker_id)
        METRICS.failed_total.labels(project=job.project, stage=current.state.value).inc()

    @staticmethod
    def _record_review_metrics(job: JobRecord, review: ReviewResult) -> None:
        model = review.model or "unknown"
        if review.input_tokens:
            METRICS.llm_input_tokens_total.labels(model=model).inc(review.input_tokens)
        if review.output_tokens:
            METRICS.llm_output_tokens_total.labels(model=model).inc(review.output_tokens)
        for finding in review.findings:
            METRICS.findings_total.labels(
                severity=finding.severity.value, project=job.project
            ).inc()


async def run_receiver(settings: Settings, store: JobStore) -> None:
    stream = GerritEventStream(settings.gerrit)
    async for event in stream:
        job, created = await store.enqueue(
            event, review_policy_version=settings.review.policy_version
        )
        if created:
            METRICS.jobs_total.labels(project=event.project).inc()
            log_event(
                logger,
                "enqueued Gerrit Patch Set",
                job_id=str(job.id),
                project=event.project,
                change=event.change_number,
                patchset=event.patchset_number,
                revision=event.revision_sha,
            )
        else:
            METRICS.duplicate_events_total.labels(project=event.project).inc()


async def run_reconciler(settings: Settings, store: JobStore, gerrit: GerritRestClient) -> None:
    interval = settings.service.reconcile_interval_seconds
    lookback = max(900, interval * 3)
    while True:
        since = datetime.now(UTC) - timedelta(seconds=lookback)
        try:
            events = await gerrit.reconciliation_events(since=since)
            for event in events:
                job, created = await store.enqueue(
                    event, review_policy_version=settings.review.policy_version
                )
                if created:
                    METRICS.jobs_total.labels(project=event.project).inc()
                    log_event(
                        logger,
                        "reconciler recovered missing Patch Set job",
                        job_id=str(job.id),
                        project=event.project,
                        change=event.change_number,
                        patchset=event.patchset_number,
                        revision=event.revision_sha,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Gerrit reconciliation pass failed")
        await asyncio.sleep(interval)


async def database_ready(database: Database) -> tuple[bool, str]:
    try:
        async with database.session() as session:
            await session.execute(text("SELECT 1"))
        return True, "database reachable"
    except Exception as exc:
        return False, f"database unavailable: {type(exc).__name__}"


@asynccontextmanager
async def service_components(
    settings: Settings,
) -> AsyncIterator[tuple[Database, JobStore, GerritRestClient, LlmClient, ReviewWorker]]:
    database = Database(settings.database)
    store = JobStore(database.sessions)
    gerrit = GerritRestClient(settings.gerrit)
    llm = LlmClient(settings.llm)
    repos = RepositoryManager(settings.repos, settings.gerrit)
    engine = NativeFirmwareReviewEngine(llm, settings.review)
    worker = ReviewWorker(settings, store, gerrit, repos, engine)
    try:
        yield database, store, gerrit, llm, worker
    finally:
        await llm.aclose()
        await gerrit.aclose()
        await database.close()
