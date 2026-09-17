from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter

from sqlalchemy import text

from pe_review_agent.admin import ControlStore
from pe_review_agent.config import Settings
from pe_review_agent.db import Database
from pe_review_agent.domain import AttemptStage, GerritPatchsetEvent, JobState, ReviewResult
from pe_review_agent.gerrit import (
    GerritEventStream,
    GerritRestClient,
    SupersededRevisionError,
    build_review_input,
)
from pe_review_agent.jobs import (
    JobRecord,
    JobStore,
    ProjectReviewStartMode,
    PublicationStatus,
    PublishGuardStatus,
)
from pe_review_agent.llm import LlmClient
from pe_review_agent.observability import METRICS, log_event
from pe_review_agent.repos import RepositoryManager, RepositoryToolExecutor
from pe_review_agent.retry import PermanentError, TransientError, exponential_backoff
from pe_review_agent.review import NativeFirmwareReviewEngine
from pe_review_agent.review.lineage import (
    findings_for_inline_publication,
    reconcile_finding_lineage,
)
from pe_review_agent.review.policy import load_policy

logger = logging.getLogger(__name__)
_RECONCILIATION_WATERMARK_KEY = "gerrit-open-changes"
_RECONCILIATION_FULL_SWEEP_KEY = "gerrit-open-changes-full-sweep"


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
        interval = max(5.0, min(30.0, self.lease_seconds / 3))
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
        control: ControlStore | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.gerrit = gerrit
        self.repos = repos
        self.engine = engine
        self.control = control

    async def run_forever(self) -> None:
        async with self.repos.worker_runtime():
            tasks = [
                asyncio.create_task(self._slot(index), name=f"review-worker-{index}")
                for index in range(self.settings.service.worker_concurrency)
            ]
            metrics_task = asyncio.create_task(self._metrics_loop(), name="review-worker-metrics")
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                metrics_task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.gather(metrics_task, return_exceptions=True)

    async def _metrics_loop(self) -> None:
        while True:
            try:
                METRICS.queue_depth.set(await self.store.pending_depth())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("failed to update review queue depth metric")
            await asyncio.sleep(5)

    async def _slot(self, index: int) -> None:
        worker_id = f"{uuid.uuid4()}:{index}"
        while True:
            if self.control is not None:
                enabled = await self.control.service_enabled(default=self.settings.service.enabled)
                projects = await self.control.enabled_projects(
                    fallback=tuple(self.settings.gerrit.projects)
                )
                self.gerrit.replace_projects(projects)
            else:
                enabled = self.settings.service.enabled
                projects = tuple(self.settings.gerrit.projects)
            if not enabled or not projects:
                await asyncio.sleep(self.settings.service.poll_interval_seconds)
                continue
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
        if job.state in {
            JobState.DONE,
            JobState.SUPERSEDED,
            JobState.SKIPPED_SCOPE,
            JobState.FAILED_PERMANENT,
        }:
            return
        await self._permanent_failure(
            job,
            worker_id=worker_id,
            error=PermanentError(f"unsupported claimed state {job.state.value}"),
        )

    async def _process_review(self, job: JobRecord, *, worker_id: str, lease: LeaseGuard) -> None:
        review_started = perf_counter()
        if job.state == JobState.RECEIVED:
            job = await self.store.transition(job.id, JobState.FETCHING, worker_id=worker_id)

        event = GerritPatchsetEvent.model_validate(job.event_payload)
        if not await self._can_start_attempt(job, AttemptStage.FETCH, worker_id):
            return
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
                exclude_patterns=self.settings.review.generated_path_patterns,
            ) as workspace:
                lease.ensure()
                await self._finish_attempt(fetch_attempt, success=True)
                if job.state == JobState.FETCHING:
                    job = await self.store.transition(
                        job.id, JobState.REVIEWING, worker_id=worker_id
                    )

                failure_stage = AttemptStage.REVIEW
                policy_text = await load_policy(
                    base_revision_sha=workspace.base_revision_sha,
                    settings=self.settings.review,
                    read_revision_text=lambda path, limit: self.repos.read_text_at_revision(
                        workspace.root,
                        workspace.base_revision_sha,
                        path,
                        max_bytes=limit,
                    ),
                )
                context = self.repos.to_review_context(
                    workspace,
                    change_number=job.change_number,
                    patchset_number=job.patchset_number,
                    subject=change.subject,
                    branch=change.branch,
                    policy_text=policy_text,
                )
                history = await self.store.load_finding_history(job.id)
                context.previous_findings = list(history.previous_findings)
                context.historical_findings = list(history.historical_findings)
                context.previous_patchset_number = history.baseline_patchset
                tools = RepositoryToolExecutor(workspace.root, self.settings.review)
                if not await self._can_start_attempt(job, AttemptStage.REVIEW, worker_id):
                    return
                review_attempt = await self._start_attempt(job, AttemptStage.REVIEW, worker_id)
                try:
                    review = await self.engine.review(context, tools)
                    if review.review_metadata.get("lineage_complete", True):
                        review = reconcile_finding_lineage(
                            project=job.project,
                            review=review,
                            history=history,
                        ).review
                    lease.ensure()
                    # The engine performs model verification and static location validation. The
                    # explicit VALIDATING state makes recovery semantics visible and leaves room
                    # for additional deterministic validators without coupling them to publishing.
                    if job.state == JobState.REVIEWING:
                        job = await self.store.transition(
                            job.id, JobState.VALIDATING, worker_id=worker_id
                        )
                    lease.ensure()
                    await self.store.save_review_result_and_mark_ready(
                        job.id, review, worker_id=worker_id
                    )
                    # Only mark the model attempt successful after the generated result is durable.
                    # A crash before READY_TO_PUBLISH therefore leaves an unfinished attempt that
                    # consumes the configured review retry budget on reclaim.
                    await self._finish_attempt(review_attempt, success=True)
                except Exception as exc:
                    await self._finish_attempt_if_open(
                        review_attempt,
                        success=False,
                        retryable=isinstance(exc, TransientError),
                        error=exc,
                    )
                    raise
                METRICS.review_latency_seconds.observe(perf_counter() - review_started)
                self._record_review_metrics(job, review)
        except SupersededRevisionError:
            await self._finish_attempt_if_open(fetch_attempt, success=True)
            await self.store.mark_superseded(job.id, worker_id=worker_id)
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
        review = await self.store.load_review_result(job.id)
        if review is None:
            await self._permanent_failure(
                job,
                worker_id=worker_id,
                error=PermanentError("publishable job is missing durable review result"),
            )
            return

        publication = await self.store.publication_for_job(job.id)
        if not await self.store.is_latest_known_patchset(job.id):
            await self._resolve_superseded_publish(
                job,
                review=review,
                publication=publication,
                worker_id=worker_id,
            )
            return

        if job.state == JobState.READY_TO_PUBLISH:
            job = await self.store.transition(job.id, JobState.PUBLISHING, worker_id=worker_id)

        if publication is None:
            inline_findings = findings_for_inline_publication(review)
            inline_review = review.model_copy(
                update={"findings": inline_findings},
                deep=True,
            )
            payload = build_review_input(
                inline_review,
                tag=self._effective_review_tag(job),
                notify=self.settings.gerrit.notify,
                max_comment_bytes=self.settings.gerrit.max_comment_bytes,
            )
            publication = await self.store.begin_publication(
                job.id,
                worker_id=worker_id,
                request_payload=payload,
                finding_fingerprints=[finding.fingerprint or "" for finding in inline_findings],
            )
        if publication.status == PublicationStatus.POSTED.value:
            await self.store.complete_publication_and_mark_done(
                publication.id,
                job_id=job.id,
                worker_id=worker_id,
                gerrit_response=publication.gerrit_response,
            )
            METRICS.success_total.labels(project=job.project).inc()
            return
        if publication.status == PublicationStatus.FAILED.value:
            await self._permanent_failure(
                job,
                worker_id=worker_id,
                error=PermanentError(
                    publication.last_error or "durable Gerrit publication is permanently failed"
                ),
            )
            return

        reconcile_attempt: _Attempt | None = None
        publish_attempt: _Attempt | None = None
        post_started = False
        reconciliation_observed_absent = False
        try:
            if not await self._can_start_attempt(job, AttemptStage.RECONCILE, worker_id):
                return
            reconcile_attempt = await self._start_attempt(job, AttemptStage.RECONCILE, worker_id)
            lease.ensure()
            already_posted = await self.gerrit.has_published_review(
                project=job.project,
                change_number=job.change_number,
                patchset_number=job.patchset_number,
                summary=self._publication_message(review, publication.request_payload),
                tag=self._publication_tag(job, publication.request_payload),
            )
            if already_posted:
                await self._finish_attempt(reconcile_attempt, success=True)
                await self.store.complete_publication_and_mark_done(
                    publication.id,
                    job_id=job.id,
                    worker_id=worker_id,
                    gerrit_response={"recovered": True},
                )
                METRICS.success_total.labels(project=job.project).inc()
                return

            # A successful message lookup proves an earlier ambiguous POST did not leave this
            # durable ReviewInput behind. Only after that observation is it safe to treat a closed
            # or superseded Change as terminal rather than preserving publication uncertainty.
            reconciliation_observed_absent = True
            await self.gerrit.ensure_current_revision(
                job.project, job.change_number, job.revision_sha
            )

            await self._finish_attempt(reconcile_attempt, success=True)
            reconcile_attempt = None
            if not await self._can_start_attempt(job, AttemptStage.PUBLISH, worker_id):
                return
            publish_attempt = await self._start_attempt(job, AttemptStage.PUBLISH, worker_id)
            lease.ensure()
            # Commit the uncertainty marker before starting the external side effect. A crash after
            # this point can therefore never leave a PENDING row that might already have reached
            # Gerrit. Recovery may safely treat PENDING as "POST not started" and AMBIGUOUS as
            # "POST may have committed".
            await self.store.mark_publication_ambiguous(
                publication.id,
                error="Gerrit POST started; outcome not yet durably confirmed",
            )
            post_started = True
            publish_started = perf_counter()
            try:
                response = await self.gerrit.publish_review_input(
                    project=job.project,
                    change_number=job.change_number,
                    revision_sha=job.revision_sha,
                    payload=publication.request_payload,
                    pre_post_guard=lambda: self._publish_guard(job.id, worker_id=worker_id),
                )
            finally:
                METRICS.gerrit_publish_latency_seconds.observe(perf_counter() - publish_started)
            await self._finish_attempt(publish_attempt, success=True)
            await self.store.complete_publication_and_mark_done(
                publication.id,
                job_id=job.id,
                worker_id=worker_id,
                gerrit_response=response,
            )
            METRICS.success_total.labels(project=job.project).inc()
        except SupersededRevisionError:
            if reconcile_attempt is not None:
                await self._finish_attempt_if_open(reconcile_attempt, success=True)
            if publish_attempt is not None:
                await self._finish_attempt_if_open(publish_attempt, success=True)
            await self.store.mark_superseded(job.id, worker_id=worker_id)
            METRICS.superseded_total.labels(project=job.project).inc()
        except TransientError as exc:
            if reconcile_attempt is not None:
                await self._finish_attempt_if_open(
                    reconcile_attempt, success=False, retryable=True, error=exc
                )
                await self._schedule_retry(job.id, AttemptStage.RECONCILE, worker_id, exc)
                return
            if publish_attempt is not None:
                await self._finish_attempt_if_open(
                    publish_attempt, success=False, retryable=True, error=exc
                )
            if post_started:
                await self.store.mark_publication_ambiguous(publication.id, error=str(exc))
                # Always allow one recovery pass after an ambiguous POST, even when this was the
                # final outbound publish attempt. The next claim first checks Gerrit and only then
                # decides whether another POST is allowed by the budget.
                await self._schedule_retry(
                    job.id,
                    AttemptStage.PUBLISH,
                    worker_id,
                    exc,
                    allow_recovery_pass=True,
                )
            else:
                await self._schedule_retry(job.id, AttemptStage.PUBLISH, worker_id, exc)
        except PermanentError as exc:
            if (
                reconcile_attempt is not None
                and publication.status == PublicationStatus.AMBIGUOUS.value
                and not reconciliation_observed_absent
            ):
                await self._finish_attempt_if_open(
                    reconcile_attempt, success=False, retryable=True, error=exc
                )
                # A permanent read/auth error still does not prove whether an earlier Gerrit POST
                # committed. Preserve the publication intent and keep newer Patch Sets blocked until
                # reconciliation can actually observe the external side effect (or an operator fixes
                # access/configuration).
                await self._schedule_retry(job.id, AttemptStage.RECONCILE, worker_id, exc)
                return
            await self.store.mark_publication_failed(publication.id, error=str(exc))
            if reconcile_attempt is not None:
                await self._finish_attempt_if_open(
                    reconcile_attempt, success=False, retryable=False, error=exc
                )
            if publish_attempt is not None:
                await self._finish_attempt_if_open(
                    publish_attempt, success=False, retryable=False, error=exc
                )
            await self._permanent_failure(job, worker_id=worker_id, error=exc)

    async def _resolve_superseded_publish(
        self,
        job: JobRecord,
        *,
        review: ReviewResult,
        publication,
        worker_id: str,
    ) -> None:
        """Resolve an older Patch Set that may already have produced an external side effect."""
        if publication is None:
            await self.store.mark_superseded(job.id, worker_id=worker_id)
            METRICS.superseded_total.labels(project=job.project).inc()
            return
        if publication.status == PublicationStatus.POSTED.value:
            await self.store.complete_publication_and_mark_done(
                publication.id,
                job_id=job.id,
                worker_id=worker_id,
                gerrit_response=publication.gerrit_response,
            )
            METRICS.success_total.labels(project=job.project).inc()
            return
        if publication.status == PublicationStatus.FAILED.value:
            await self.store.mark_superseded(job.id, worker_id=worker_id)
            METRICS.superseded_total.labels(project=job.project).inc()
            return

        if not await self._can_start_attempt(job, AttemptStage.RECONCILE, worker_id):
            return
        attempt = await self._start_attempt(job, AttemptStage.RECONCILE, worker_id)
        try:
            already_posted = await self.gerrit.has_published_review(
                project=job.project,
                change_number=job.change_number,
                patchset_number=job.patchset_number,
                summary=self._publication_message(review, publication.request_payload),
                tag=self._publication_tag(job, publication.request_payload),
            )
            await self._finish_attempt(attempt, success=True)
        except TransientError as exc:
            await self._finish_attempt_if_open(attempt, success=False, retryable=True, error=exc)
            await self._schedule_retry(job.id, AttemptStage.RECONCILE, worker_id, exc)
            return
        except PermanentError as exc:
            if publication.status == PublicationStatus.AMBIGUOUS.value:
                await self._finish_attempt_if_open(
                    attempt, success=False, retryable=True, error=exc
                )
                await self._schedule_retry(job.id, AttemptStage.RECONCILE, worker_id, exc)
                return
            await self._finish_attempt_if_open(
                attempt, success=False, retryable=False, error=exc
            )
            await self.store.mark_superseded(job.id, worker_id=worker_id)
            METRICS.superseded_total.labels(project=job.project).inc()
            return
        if already_posted:
            await self.store.complete_publication_and_mark_done(
                publication.id,
                job_id=job.id,
                worker_id=worker_id,
                gerrit_response={"recovered_after_supersession": True},
            )
            METRICS.success_total.labels(project=job.project).inc()
        else:
            await self.store.mark_superseded(job.id, worker_id=worker_id)
            METRICS.superseded_total.labels(project=job.project).inc()

    async def _publish_guard(self, job_id: uuid.UUID, *, worker_id: str) -> bool:
        status = await self.store.refresh_publish_guard(
            job_id,
            worker_id=worker_id,
            lease_seconds=self.settings.service.claim_lease_seconds,
        )
        if status is PublishGuardStatus.LEASE_LOST:
            raise LeaseLostError(f"job {job_id} lost publish lease before Gerrit POST")
        return status is PublishGuardStatus.OK

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
        error: TransientError | PermanentError,
        *,
        allow_recovery_pass: bool = False,
    ) -> None:
        job = await self.store.get(job_id)
        if job is None:
            return
        stage_attempts = await self.store.count_consumed_retry_attempts(job_id, stage=stage)
        budget = self._retry_budget(stage)
        # RECONCILE is special: a previously ambiguous Gerrit POST may already have committed.
        # Transient GET failures can never prove that side effect absent, so exhausting an
        # ordinary retry budget here must not terminalize the job or release a newer Patch Set.
        # Attempts stay durable/auditable and backoff is capped by retry.max_seconds until Gerrit
        # can answer (or an operator deliberately intervenes).
        if (
            stage is not AttemptStage.RECONCILE
            and stage_attempts >= budget
            and not allow_recovery_pass
        ):
            await self.store.mark_failed_permanent(
                job_id,
                worker_id=worker_id,
                error=PermanentError(
                    f"{stage.value} retry budget exhausted after {stage_attempts} attempts: {error}"
                ),
            )
            METRICS.failed_total.labels(project=job.project, stage=stage.value).inc()
            return

        retry_after = error.retry_after_seconds if isinstance(error, TransientError) else None
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
        # Publication-intent helpers may deliberately leave the durable review at
        # READY_TO_PUBLISH after an ambiguous transport result. A publish-stage retry must resume
        # the publish state machine, never route back through the LLM/review path.
        resume = (
            JobState.PUBLISHING
            if stage == AttemptStage.PUBLISH and current.state == JobState.READY_TO_PUBLISH
            else current.state
        )
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

    async def _can_start_attempt(
        self,
        job: JobRecord,
        stage: AttemptStage,
        worker_id: str,
    ) -> bool:
        consumed = await self.store.count_consumed_retry_attempts(job.id, stage=stage)
        if stage is AttemptStage.RECONCILE:
            return True
        budget = self._retry_budget(stage)
        if consumed < budget:
            return True
        await self.store.mark_failed_permanent(
            job.id,
            worker_id=worker_id,
            error=PermanentError(
                f"{stage.value} retry budget exhausted after {consumed} failed/crashed attempts"
            ),
        )
        METRICS.failed_total.labels(project=job.project, stage=stage.value).inc()
        return False

    def _retry_budget(self, stage: AttemptStage) -> int:
        return {
            AttemptStage.FETCH: self.settings.retry.fetch_attempts,
            AttemptStage.REVIEW: self.settings.retry.review_attempts,
            AttemptStage.VALIDATE: self.settings.retry.review_attempts,
            AttemptStage.PUBLISH: self.settings.retry.publish_attempts,
            AttemptStage.RECONCILE: self.settings.retry.fetch_attempts,
        }[stage]

    def _effective_review_tag(self, job: JobRecord) -> str:
        return f"{self.settings.gerrit.review_tag}~{job.review_policy_version}"

    def _publication_tag(self, job: JobRecord, payload: dict[str, object]) -> str:
        value = payload.get("tag")
        return value if isinstance(value, str) and value else self._effective_review_tag(job)

    @staticmethod
    def _publication_message(review: ReviewResult, payload: dict[str, object]) -> str:
        value = payload.get("message")
        return value if isinstance(value, str) else review.summary

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


async def run_receiver(
    settings: Settings,
    store: JobStore,
    control: ControlStore | None = None,
) -> None:
    stream = GerritEventStream(settings.gerrit, filter_projects=control is None)
    async for event in stream:
        if control is not None:
            if not await control.service_enabled(default=settings.service.enabled):
                continue
            if not await control.project_enabled(
                event.project,
                fallback=tuple(settings.gerrit.projects),
            ):
                continue
        elif not settings.service.enabled:
            continue
        job, created = await store.enqueue(
            event, review_policy_version=settings.review.policy_version
        )
        if created:
            if job.state is JobState.SKIPPED_SCOPE:
                log_event(
                    logger,
                    "ignored Gerrit Patch Set outside project review scope",
                    job_id=str(job.id),
                    project=event.project,
                    change=event.change_number,
                    patchset=event.patchset_number,
                    revision=event.revision_sha,
                )
                continue
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


async def run_reconciler(
    settings: Settings,
    store: JobStore,
    gerrit: GerritRestClient,
    control: ControlStore | None = None,
) -> None:
    interval = settings.service.reconcile_interval_seconds
    overlap = timedelta(seconds=max(60, interval))
    full_sweep_interval = timedelta(
        seconds=settings.service.reconcile_full_sweep_interval_seconds
    )
    while True:
        project_cutoffs: dict[str, datetime | None] | None = None
        project_scopes = ()
        if control is not None:
            if not await control.service_enabled(default=settings.service.enabled):
                await asyncio.sleep(interval)
                continue
            project_scopes = await control.enabled_project_scopes()
            projects = tuple(scope.project for scope in project_scopes)
            if not projects:
                await asyncio.sleep(interval)
                continue
            gerrit.replace_projects(projects)
            project_cutoffs = {
                scope.project: (
                    scope.review_start_at
                    if scope.review_start_mode is ProjectReviewStartMode.FROM_NOW
                    else None
                )
                for scope in project_scopes
            }
        elif not settings.service.enabled:
            await asyncio.sleep(interval)
            continue
        pass_started = datetime.now(UTC)
        try:
            watermark = await store.get_service_watermark(_RECONCILIATION_WATERMARK_KEY)
            last_full_sweep = await store.get_service_watermark(_RECONCILIATION_FULL_SWEEP_KEY)
            full_sweep_due = (
                last_full_sweep is None or pass_started - last_full_sweep >= full_sweep_interval
            )
            if control is not None and not full_sweep_due:
                full_sweep_due = any(
                    scope.review_start_mode is ProjectReviewStartMode.INCLUDE_OPEN
                    and (last_full_sweep is None or scope.updated_at > last_full_sweep)
                    for scope in project_scopes
                )
            # First boot scans every current open change in the allowlist. Later passes resume from
            # the durable successful watermark with overlap. Periodic full scans ensure an open
            # Change omitted by a temporarily stale Gerrit secondary index is never aged out
            # forever.
            since = (
                None
                if full_sweep_due
                else watermark - overlap
                if watermark is not None
                else None
            )
            events = await gerrit.reconciliation_events(
                since=since,
                project_since=project_cutoffs,
            )
            for event in events:
                job, created = await store.enqueue(
                    event, review_policy_version=settings.review.policy_version
                )
                if created:
                    if job.state is JobState.SKIPPED_SCOPE:
                        continue
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
            await store.advance_service_watermark(_RECONCILIATION_WATERMARK_KEY, pass_started)
            if full_sweep_due:
                await store.advance_service_watermark(_RECONCILIATION_FULL_SWEEP_KEY, pass_started)
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
) -> AsyncIterator[
    tuple[Database, JobStore, ControlStore, Settings, GerritRestClient, LlmClient, ReviewWorker]
]:
    database = Database(settings.database)
    store = JobStore(database.sessions)
    control = ControlStore(database.sessions)
    await control.ensure_bootstrap(settings)
    effective = await control.effective_settings(settings)
    gerrit = GerritRestClient(effective.gerrit)
    llm = LlmClient(effective.llm)
    repos = RepositoryManager(effective.repos, effective.gerrit)
    engine = NativeFirmwareReviewEngine(llm, effective.review)
    worker = ReviewWorker(effective, store, gerrit, repos, engine, control=control)
    try:
        yield database, store, control, effective, gerrit, llm, worker
    finally:
        await llm.aclose()
        await gerrit.aclose()
        await database.close()
