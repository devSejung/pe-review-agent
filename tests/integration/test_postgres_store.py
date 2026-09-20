from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from pe_review_agent.admin import ControlStore
from pe_review_agent.config import DatabaseSettings
from pe_review_agent.db import Database
from pe_review_agent.domain import (
    AttemptStage,
    DiffSide,
    Finding,
    FindingLineage,
    FindingLocation,
    GerritPatchsetEvent,
    JobState,
    ReviewResult,
    Severity,
)
from pe_review_agent.jobs import (
    JobStore,
    ProjectReviewStartMode,
    PublicationStatus,
    PublishGuardStatus,
)
from pe_review_agent.jobs.progress import PostgresProgressBackend
from pe_review_agent.retry import TransientError
from pe_review_agent.review.checkpoints import CandidateChunkCheckpoint
from pe_review_agent.review.progress import Invocation, ReviewProgress, Usage

DSN = os.environ.get("PE_REVIEW_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="PE_REVIEW_TEST_POSTGRES_DSN is not configured")


def _event(*, patchset: int, revision: str) -> GerritPatchsetEvent:
    return GerritPatchsetEvent(
        project="team/fw",
        change_number=101,
        patchset_number=patchset,
        revision_sha=revision,
        ref=f"refs/changes/01/101/{patchset}",
        branch="main",
    )


def _review() -> ReviewResult:
    return ReviewResult(
        summary="One actionable firmware defect was found.",
        model="Qwen3.6-27B",
        input_tokens=100,
        output_tokens=20,
        findings=[
            Finding(
                severity=Severity.P1,
                category="timeout",
                title="Timeout is treated as success",
                message="The timeout path still advances the state machine.",
                impact="The next stage can consume stale training data.",
                evidence="poll_done() returns -ETIMEDOUT but the caller discards it.",
                remediation="Propagate or explicitly handle the timeout.",
                location=FindingLocation(path="fw/train.c", start_line=42),
                confidence=0.97,
            )
        ],
    )


@pytest.fixture
async def store():
    assert DSN is not None
    database = Database(DatabaseSettings(dsn=DSN, pool_size=4, max_overflow=2))
    async with database.session() as session:
        await session.execute(
            text(
                "TRUNCATE review_service_heartbeats, review_managed_projects, "
                "review_service_state, review_publications, "
                "review_findings, "
                "review_results, "
                "review_attempts, review_jobs RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()
    try:
        yield JobStore(database.sessions), database
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_reenabling_from_now_project_skips_disabled_period_queue(store) -> None:
    jobs, database = store
    control = ControlStore(database.sessions)
    await control.upsert_project("team/fw", enabled=False)
    queued, _ = await jobs.enqueue(
        _event(patchset=1, revision="a" * 40),
        review_policy_version="firmware-v1",
    )

    assert await jobs.claim_next(worker_id="worker-disabled", lease_seconds=120) is None

    await control.set_project_enabled("team/fw", True)
    queued_after = await jobs.get(queued.id)
    assert queued_after is not None and queued_after.state is JobState.SKIPPED_SCOPE
    assert await jobs.claim_next(worker_id="worker-enabled", lease_seconds=120) is None


@pytest.mark.asyncio
async def test_reenabling_include_open_project_replays_disabled_period_queue(store) -> None:
    jobs, database = store
    control = ControlStore(database.sessions)
    await control.upsert_project(
        "team/fw",
        enabled=False,
        review_start_mode=ProjectReviewStartMode.INCLUDE_OPEN,
    )
    queued, _ = await jobs.enqueue(
        _event(patchset=1, revision="a" * 40),
        review_policy_version="firmware-v1",
    )

    assert await jobs.claim_next(worker_id="worker-disabled", lease_seconds=120) is None

    await control.set_project_enabled("team/fw", True)
    claimed = await jobs.claim_next(worker_id="worker-enabled", lease_seconds=120)
    assert claimed is not None and claimed.id == queued.id


@pytest.mark.asyncio
async def test_include_open_revives_current_patchset_previously_skipped_by_scope(store) -> None:
    jobs, database = store
    control = ControlStore(database.sessions)
    await control.upsert_project("team/fw", enabled=True)
    old_event = _event(patchset=1, revision="a" * 40).model_copy(
        update={"occurred_at": datetime.now(UTC) - timedelta(hours=1)}
    )

    skipped, created = await jobs.enqueue(old_event, review_policy_version="firmware-v1")
    assert created is True
    assert skipped.state is JobState.SKIPPED_SCOPE

    await control.set_project_review_start_mode(
        "team/fw", ProjectReviewStartMode.INCLUDE_OPEN
    )
    revived, created_again = await jobs.enqueue(old_event, review_policy_version="firmware-v1")
    assert created_again is False
    assert revived.id == skipped.id
    assert revived.state is JobState.RECEIVED

    claimed = await jobs.claim_next(worker_id="worker-backfill", lease_seconds=120)
    assert claimed is not None and claimed.id == skipped.id


@pytest.mark.asyncio
async def test_durable_review_and_publication_resume_after_crash(store) -> None:
    jobs, database = store
    event = _event(patchset=1, revision="a" * 40)

    first, created = await jobs.enqueue(event, review_policy_version="firmware-v1")
    duplicate, duplicate_created = await jobs.enqueue(event, review_policy_version="firmware-v1")
    assert created is True
    assert duplicate_created is False
    assert duplicate.id == first.id

    claimed = await jobs.claim_next(worker_id="worker-a", lease_seconds=120)
    assert claimed is not None and claimed.id == first.id
    claimed = await jobs.transition(first.id, JobState.FETCHING, worker_id="worker-a")
    claimed = await jobs.transition(first.id, JobState.REVIEWING, worker_id="worker-a")
    claimed = await jobs.transition(first.id, JobState.VALIDATING, worker_id="worker-a")
    ready = await jobs.save_review_result_and_mark_ready(first.id, _review(), worker_id="worker-a")
    assert ready.state == JobState.READY_TO_PUBLISH
    assert ready.lease_owner is None

    recovered_review = await jobs.load_review_result(first.id)
    assert recovered_review is not None
    assert recovered_review.summary == _review().summary
    assert len(recovered_review.findings) == 1
    assert recovered_review.findings[0].fingerprint
    assert recovered_review.findings[0].location.side == DiffSide.REVISION

    publish_claim = await jobs.claim_next(worker_id="worker-b", lease_seconds=120)
    assert publish_claim is not None and publish_claim.state == JobState.READY_TO_PUBLISH
    await jobs.transition(first.id, JobState.PUBLISHING, worker_id="worker-b")
    payload = {
        "message": recovered_review.summary,
        "tag": "autogenerated:pe-ai-review",
        "omit_duplicate_comments": True,
    }
    publication = await jobs.begin_publication(
        first.id,
        worker_id="worker-b",
        request_payload=payload,
        finding_fingerprints=[recovered_review.findings[0].fingerprint or ""],
    )

    # Simulate a hard process crash after durable publication intent but before a Gerrit response
    # could be recorded: the lease simply expires while state remains PUBLISHING/PENDING.
    async with database.session() as session:
        await session.execute(
            text(
                "UPDATE review_jobs SET lease_expires_at = :expired "
                "WHERE id = CAST(:job_id AS uuid)"
            ),
            {
                "expired": datetime.now(UTC) - timedelta(seconds=1),
                "job_id": str(first.id),
            },
        )
        await session.commit()

    reclaimed = await jobs.claim_next(worker_id="worker-c", lease_seconds=120)
    assert reclaimed is not None
    assert reclaimed.id == first.id
    assert reclaimed.state == JobState.PUBLISHING
    publication_again = await jobs.begin_publication(
        first.id,
        worker_id="worker-c",
        request_payload=payload,
        finding_fingerprints=[recovered_review.findings[0].fingerprint or ""],
    )
    assert publication_again.id == publication.id

    await jobs.complete_publication(publication.id, gerrit_response={"ok": True})
    done = await jobs.mark_done(first.id, worker_id="worker-c")
    assert done.state == JobState.DONE


@pytest.mark.asyncio
async def test_new_patchset_supersedes_older_durable_job(store) -> None:
    jobs, _database = store
    old, _ = await jobs.enqueue(
        _event(patchset=1, revision="a" * 40), review_policy_version="firmware-v1"
    )
    new, _ = await jobs.enqueue(
        _event(patchset=2, revision="b" * 40), review_policy_version="firmware-v1"
    )

    old_after = await jobs.get(old.id)
    new_after = await jobs.get(new.id)
    assert old_after is not None and old_after.state == JobState.SUPERSEDED
    assert new_after is not None and new_after.state == JobState.RECEIVED


@pytest.mark.asyncio
async def test_concurrent_claims_do_not_take_same_job(store) -> None:
    jobs, _database = store
    one, _ = await jobs.enqueue(
        _event(patchset=1, revision="a" * 40), review_policy_version="firmware-v1"
    )
    two_event = _event(patchset=1, revision="b" * 40).model_copy(
        update={"change_number": 102, "ref": "refs/changes/02/102/1"}
    )
    two, _ = await jobs.enqueue(two_event, review_policy_version="firmware-v1")

    first_claim, second_claim = await asyncio.gather(
        jobs.claim_next(worker_id="worker-a", lease_seconds=120),
        jobs.claim_next(worker_id="worker-b", lease_seconds=120),
    )
    assert first_claim is not None and second_claim is not None
    assert {first_claim.id, second_claim.id} == {one.id, two.id}


@pytest.mark.asyncio
async def test_service_watermark_is_durable_and_monotonic(store) -> None:
    jobs, _database = store
    key = "test-reconciliation-watermark"
    first = datetime(2026, 9, 16, 1, 0, tzinfo=UTC)
    older = first - timedelta(hours=1)
    newer = first + timedelta(hours=1)

    assert await jobs.get_service_watermark(key) is None
    await jobs.advance_service_watermark(key, first)
    await jobs.advance_service_watermark(key, older)
    assert await jobs.get_service_watermark(key) == first
    await jobs.advance_service_watermark(key, newer)
    assert await jobs.get_service_watermark(key) == newer


@pytest.mark.asyncio
async def test_publish_guard_rejects_stale_worker_and_extends_current_lease(store) -> None:
    jobs, database = store
    job, _ = await jobs.enqueue(
        _event(patchset=1, revision="a" * 40), review_policy_version="firmware-v1"
    )
    reviewer = await jobs.claim_next(worker_id="reviewer", lease_seconds=120)
    assert reviewer is not None
    await jobs.transition(job.id, JobState.FETCHING, worker_id="reviewer")
    await jobs.transition(job.id, JobState.REVIEWING, worker_id="reviewer")
    await jobs.transition(job.id, JobState.VALIDATING, worker_id="reviewer")
    await jobs.save_review_result_and_mark_ready(job.id, _review(), worker_id="reviewer")

    publisher = await jobs.claim_next(worker_id="publisher-a", lease_seconds=120)
    assert publisher is not None
    await jobs.transition(job.id, JobState.PUBLISHING, worker_id="publisher-a")
    assert (
        await jobs.refresh_publish_guard(job.id, worker_id="publisher-a", lease_seconds=120)
        == PublishGuardStatus.OK
    )

    await _expire_job_lease(database, job.id)
    replacement = await jobs.claim_next(worker_id="publisher-b", lease_seconds=120)
    assert replacement is not None and replacement.id == job.id
    assert (
        await jobs.refresh_publish_guard(job.id, worker_id="publisher-a", lease_seconds=120)
        == PublishGuardStatus.LEASE_LOST
    )
    with pytest.raises(RuntimeError, match="not leased"):
        await jobs.mark_superseded(job.id, worker_id="publisher-a")
    current = await jobs.get(job.id)
    assert current is not None and current.lease_owner == "publisher-b"
    assert (
        await jobs.refresh_publish_guard(job.id, worker_id="publisher-b", lease_seconds=120)
        == PublishGuardStatus.OK
    )


@pytest.mark.asyncio
async def test_new_patchset_preserves_inflight_publish_for_side_effect_recovery(store) -> None:
    jobs, _database = store
    old, _ = await jobs.enqueue(
        _event(patchset=1, revision="a" * 40), review_policy_version="firmware-v1"
    )
    reviewer = await jobs.claim_next(worker_id="reviewer", lease_seconds=120)
    assert reviewer is not None
    await jobs.transition(old.id, JobState.FETCHING, worker_id="reviewer")
    await jobs.transition(old.id, JobState.REVIEWING, worker_id="reviewer")
    await jobs.transition(old.id, JobState.VALIDATING, worker_id="reviewer")
    await jobs.save_review_result_and_mark_ready(old.id, _review(), worker_id="reviewer")
    publisher = await jobs.claim_next(worker_id="publisher", lease_seconds=120)
    assert publisher is not None
    await jobs.transition(old.id, JobState.PUBLISHING, worker_id="publisher")

    new, _ = await jobs.enqueue(
        _event(patchset=2, revision="b" * 40), review_policy_version="firmware-v1"
    )

    old_after = await jobs.get(old.id)
    assert old_after is not None and old_after.state == JobState.PUBLISHING
    assert old_after.superseded_by_job_id == new.id
    assert old_after.lease_owner == "publisher"


@pytest.mark.asyncio
async def test_newer_patchset_waits_for_unresolved_older_publication_recovery(store) -> None:
    jobs, _database = store
    old, _ = await jobs.enqueue(
        _event(patchset=1, revision="a" * 40), review_policy_version="firmware-v1"
    )
    reviewer = await jobs.claim_next(worker_id="reviewer", lease_seconds=120)
    assert reviewer is not None and reviewer.id == old.id
    await jobs.transition(old.id, JobState.FETCHING, worker_id="reviewer")
    await jobs.transition(old.id, JobState.REVIEWING, worker_id="reviewer")
    await jobs.transition(old.id, JobState.VALIDATING, worker_id="reviewer")
    await jobs.save_review_result_and_mark_ready(old.id, _review(), worker_id="reviewer")
    publisher = await jobs.claim_next(worker_id="publisher", lease_seconds=120)
    assert publisher is not None and publisher.id == old.id
    await jobs.transition(old.id, JobState.PUBLISHING, worker_id="publisher")
    await jobs.schedule_retry(
        old.id,
        resume_state=JobState.PUBLISHING,
        retry_at=datetime.now(UTC) + timedelta(hours=1),
        error=TransientError("ambiguous Gerrit POST"),
        worker_id="publisher",
    )

    newer, _ = await jobs.enqueue(
        _event(patchset=2, revision="b" * 40), review_policy_version="firmware-v1"
    )
    blocked = await jobs.claim_next(worker_id="new-reviewer", lease_seconds=120)

    assert blocked is None
    newer_record = await jobs.get(newer.id)
    assert newer_record is not None and newer_record.state == JobState.RECEIVED


@pytest.mark.asyncio
async def test_finding_history_uses_latest_done_patchset_and_keeps_older_seen_ids(store) -> None:
    jobs, _database = store
    first_review = _review()
    first_review.findings[0].semantic_id = "1" * 32
    first_review.findings[0].lineage = FindingLineage.NEW
    first = await _save_done_review(
        jobs,
        _event(patchset=1, revision="a" * 40),
        first_review,
        worker_prefix="ps1",
    )
    assert (await jobs.get(first.id)).state == JobState.DONE  # type: ignore[union-attr]

    # PS2 was published with no findings, meaning the PS1 finding was resolved at that point.
    second = await _save_done_review(
        jobs,
        _event(patchset=2, revision="b" * 40),
        ReviewResult(summary="No findings", findings=[]),
        worker_prefix="ps2",
    )
    assert (await jobs.get(second.id)).state == JobState.DONE  # type: ignore[union-attr]

    third, _ = await jobs.enqueue(
        _event(patchset=3, revision="c" * 40),
        review_policy_version="firmware-v1",
    )
    history = await jobs.load_finding_history(third.id)

    assert history.baseline_patchset == 2
    assert history.previous_findings == ()
    assert "1" * 32 in history.seen_semantic_ids
    assert [finding.semantic_id for finding in history.historical_findings] == ["1" * 32]


@pytest.mark.asyncio
async def test_incomplete_review_does_not_replace_last_complete_finding_baseline(store) -> None:
    jobs, _database = store
    first_review = _review()
    first_review.findings[0].semantic_id = "2" * 32
    first_review.findings[0].lineage = FindingLineage.NEW
    first = await _save_done_review(
        jobs,
        _event(patchset=1, revision="a" * 40),
        first_review,
        worker_prefix="ps1",
    )
    assert (await jobs.get(first.id)).state == JobState.DONE  # type: ignore[union-attr]

    skipped = ReviewResult(
        summary="Automated review skipped for this merge commit.",
        findings=[],
        review_metadata={"lineage_complete": False, "skipped_reason": "merge"},
    )
    second = await _save_done_review(
        jobs,
        _event(patchset=2, revision="b" * 40),
        skipped,
        worker_prefix="ps2",
    )
    assert (await jobs.get(second.id)).state == JobState.DONE  # type: ignore[union-attr]

    third, _ = await jobs.enqueue(
        _event(patchset=3, revision="c" * 40),
        review_policy_version="firmware-v1",
    )
    history = await jobs.load_finding_history(third.id)

    assert history.baseline_patchset == 1
    assert [finding.semantic_id for finding in history.previous_findings] == ["2" * 32]


@pytest.mark.asyncio
async def test_manual_requeue_starts_new_retry_epoch_without_erasing_attempt_history(store) -> None:
    jobs, _database = store
    job, _ = await jobs.enqueue(
        _event(patchset=1, revision="a" * 40), review_policy_version="firmware-v1"
    )
    claim = await jobs.claim_next(worker_id="worker", lease_seconds=120)
    assert claim is not None and claim.id == job.id
    await jobs.transition(job.id, JobState.FETCHING, worker_id="worker")
    attempt_id = await jobs.start_attempt(job.id, stage=AttemptStage.FETCH, worker_id="worker")
    await jobs.finish_attempt(
        attempt_id,
        success=False,
        retryable=False,
        error="repository access denied",
    )
    await jobs.mark_failed_permanent(
        job.id,
        error="repository access denied",
        worker_id="worker",
    )
    assert await jobs.count_attempts(job.id, stage=AttemptStage.FETCH) == 1
    assert await jobs.count_consumed_retry_attempts(job.id, stage=AttemptStage.FETCH) == 1

    requeued = await jobs.requeue_failed(job.id)

    assert requeued.state == JobState.RECEIVED
    assert requeued.retry_epoch_start_attempt == 1
    assert await jobs.count_attempts(job.id, stage=AttemptStage.FETCH) == 1
    assert await jobs.count_consumed_retry_attempts(job.id, stage=AttemptStage.FETCH) == 0


@pytest.mark.asyncio
async def test_manual_requeue_resumes_durable_review_without_model_phase(store) -> None:
    jobs, _database = store
    job, _ = await jobs.enqueue(
        _event(patchset=1, revision="b" * 40), review_policy_version="firmware-v1"
    )
    reviewer = await jobs.claim_next(worker_id="reviewer", lease_seconds=120)
    assert reviewer is not None and reviewer.id == job.id
    await jobs.transition(job.id, JobState.FETCHING, worker_id="reviewer")
    await jobs.transition(job.id, JobState.REVIEWING, worker_id="reviewer")
    await jobs.transition(job.id, JobState.VALIDATING, worker_id="reviewer")
    await jobs.save_review_result_and_mark_ready(job.id, _review(), worker_id="reviewer")
    publisher = await jobs.claim_next(worker_id="publisher", lease_seconds=120)
    assert publisher is not None and publisher.id == job.id
    await jobs.mark_failed_permanent(job.id, error="Gerrit 403", worker_id="publisher")

    requeued = await jobs.requeue_failed(job.id)

    assert requeued.state == JobState.READY_TO_PUBLISH
    persisted = await jobs.load_review_result(job.id)
    assert persisted is not None and persisted.summary == _review().summary


@pytest.mark.asyncio
async def test_manual_requeue_resets_failed_publication_for_reconciliation(store) -> None:
    jobs, _database = store
    job, _ = await jobs.enqueue(
        _event(patchset=1, revision="c" * 40), review_policy_version="firmware-v1"
    )
    reviewer = await jobs.claim_next(worker_id="reviewer", lease_seconds=120)
    assert reviewer is not None and reviewer.id == job.id
    await jobs.transition(job.id, JobState.FETCHING, worker_id="reviewer")
    await jobs.transition(job.id, JobState.REVIEWING, worker_id="reviewer")
    await jobs.transition(job.id, JobState.VALIDATING, worker_id="reviewer")
    await jobs.save_review_result_and_mark_ready(job.id, _review(), worker_id="reviewer")
    publisher = await jobs.claim_next(worker_id="publisher", lease_seconds=120)
    assert publisher is not None and publisher.id == job.id
    await jobs.transition(job.id, JobState.PUBLISHING, worker_id="publisher")
    publication = await jobs.begin_publication(
        job.id,
        worker_id="publisher",
        request_payload={"message": "durable summary", "tag": "autogenerated:pe-ai-review"},
        finding_fingerprints=[],
    )
    await jobs.mark_publication_failed(publication.id, error="Gerrit 403")
    await jobs.mark_failed_permanent(job.id, error="Gerrit 403", worker_id="publisher")

    requeued = await jobs.requeue_failed(job.id)
    reset_publication = await jobs.publication_for_job(job.id)

    assert requeued.state == JobState.PUBLISHING
    assert reset_publication is not None
    assert reset_publication.status == PublicationStatus.PENDING.value
    assert reset_publication.last_error is None


@pytest.mark.asyncio
async def test_manual_requeue_refuses_stale_failed_patchset(store) -> None:
    jobs, _database = store
    old, _ = await jobs.enqueue(
        _event(patchset=1, revision="d" * 40), review_policy_version="firmware-v1"
    )
    claim = await jobs.claim_next(worker_id="worker", lease_seconds=120)
    assert claim is not None and claim.id == old.id
    await jobs.mark_failed_permanent(old.id, error="bad config", worker_id="worker")
    await jobs.enqueue(
        _event(patchset=2, revision="e" * 40), review_policy_version="firmware-v1"
    )

    with pytest.raises(RuntimeError, match="older Patch Set"):
        await jobs.requeue_failed(old.id)


@pytest.mark.asyncio
async def test_candidate_checkpoints_are_durable_and_pruned_only_for_old_terminal_jobs(
    store,
) -> None:
    jobs, database = store
    job, _ = await jobs.enqueue(
        _event(patchset=1, revision="f" * 40), review_policy_version="firmware-v1"
    )
    claim = await jobs.claim_next(worker_id="checkpoint-worker", lease_seconds=120)
    assert claim is not None and claim.id == job.id
    await jobs.transition(job.id, JobState.FETCHING, worker_id="checkpoint-worker")
    await jobs.transition(job.id, JobState.REVIEWING, worker_id="checkpoint-worker")
    checkpoint = CandidateChunkCheckpoint(
        chunk_key="a" * 64,
        parent_chunk_key=None,
        status="DONE",
        paths=("fw/train.c",),
        change_summary="- training path changed",
        findings=(),
        input_tokens=123,
        output_tokens=17,
        llm_calls=2,
        tool_calls=1,
    )
    await jobs.save_candidate_chunk_checkpoint(
        job.id,
        worker_id="checkpoint-worker",
        checkpoint_version="candidate-v1",
        checkpoint=checkpoint,
    )

    loaded = await jobs.load_candidate_chunk_checkpoints(job.id, checkpoint_version="candidate-v1")
    assert loaded[checkpoint.chunk_key] == checkpoint

    old_timestamp = datetime.now(UTC) - timedelta(days=40)
    async with database.session() as session:
        await session.execute(
            text(
                "UPDATE review_chunk_checkpoints SET updated_at = :updated_at "
                "WHERE job_id = CAST(:job_id AS uuid)"
            ),
            {"updated_at": old_timestamp, "job_id": str(job.id)},
        )
        await session.execute(
            text(
                "UPDATE review_jobs SET state = 'FAILED_PERMANENT', lease_owner = NULL, "
                "lease_expires_at = NULL WHERE id = CAST(:job_id AS uuid)"
            ),
            {"job_id": str(job.id)},
        )
        await session.commit()

    assert await jobs.prune_review_recovery_cache(retention_days=30) == 0
    assert await jobs.load_candidate_chunk_checkpoints(job.id, checkpoint_version="candidate-v1")

    async with database.session() as session:
        await session.execute(
            text("UPDATE review_jobs SET state = 'DONE' WHERE id = CAST(:job_id AS uuid)"),
            {"job_id": str(job.id)},
        )
        await session.commit()

    assert await jobs.prune_review_recovery_cache(retention_days=30) == 1
    assert (
        await jobs.load_candidate_chunk_checkpoints(job.id, checkpoint_version="candidate-v1") == {}
    )


async def _progress_backend(jobs: JobStore):
    job, _ = await jobs.enqueue(
        _event(patchset=1, revision="a" * 40),
        review_policy_version="firmware-v1",
    )
    claim = await jobs.claim_next(worker_id="reviewer", lease_seconds=120)
    assert claim is not None and claim.id == job.id
    await jobs.transition(job.id, JobState.FETCHING, worker_id="reviewer")
    await jobs.transition(job.id, JobState.REVIEWING, worker_id="reviewer")
    attempt = await jobs.start_attempt(job.id, stage=AttemptStage.REVIEW, worker_id="reviewer")
    return job, PostgresProgressBackend(
        jobs._sessions, job_id=job.id, worker_id="reviewer", attempt_id=attempt,
    )


@pytest.mark.asyncio
async def test_progress_manifest_and_invocation_completion_are_immutable_and_idempotent(store):
    jobs, _ = store
    _, backend = await _progress_backend(jobs)
    progress = ReviewProgress(input_key="1" * 64)
    await backend.save(progress)
    progress.phase = "verification"
    progress.candidate_usage = Usage(llm_calls=3, tool_calls=2)
    progress.frozen_candidates = _review().findings
    await backend.save(progress)
    await backend.save(progress)
    loaded = await backend.load(progress.input_key)
    assert loaded == progress
    changed = progress.model_copy(update={"frozen_candidates": []}, deep=True)
    with pytest.raises(TransientError, match="manifest is immutable"):
        await backend.save(changed)
    backward = progress.model_copy(update={"phase": "candidate"}, deep=True)
    with pytest.raises(TransientError, match="cannot move back"):
        await backend.save(backward)

    invocation = Invocation(
        id=str(uuid.uuid4()), input_key=progress.input_key,
        phase="verification", work_key="2" * 64, kind="llm",
    )
    await backend.record_invocation(invocation)
    await backend.record_invocation(invocation)
    invocation.status = "completed"
    invocation.input_tokens = 1234
    invocation.output_tokens = 50
    await backend.record_invocation(invocation)
    await backend.record_invocation(invocation)
    totals = await backend.totals()
    assert totals["llm_calls"] == 1 and totals["input_tokens"] == 1234
    assert totals["unconfirmed_calls"] == 0
    invocation.input_tokens = 999
    with pytest.raises(TransientError, match="cannot be rewritten"):
        await backend.record_invocation(invocation)
    assert (await backend.totals())["input_tokens"] == 1234


@pytest.mark.asyncio
async def test_lost_lease_cannot_write_progress_or_complete_an_old_invocation(store):
    jobs, database = store
    job, backend = await _progress_backend(jobs)
    progress = ReviewProgress(input_key="1" * 64)
    await backend.save(progress)
    invocation = Invocation(
        id=str(uuid.uuid4()), input_key=progress.input_key,
        phase="candidate", work_key="2" * 64, kind="llm",
    )
    await backend.record_invocation(invocation)
    await _expire_job_lease(database, job.id)
    replacement = await jobs.claim_next(worker_id="replacement", lease_seconds=120)
    assert replacement is not None
    with pytest.raises(TransientError, match="not leased"):
        await backend.save(progress)
    invocation.status = "completed"
    with pytest.raises(TransientError, match="not leased"):
        await backend.record_invocation(invocation)
    assert (await backend.totals())["unconfirmed_calls"] == 1


@pytest.mark.asyncio
async def test_legacy_checkpoint_usage_is_preserved_without_trusting_missing_context(store):
    jobs, _ = store
    job, backend = await _progress_backend(jobs)
    for index, status in enumerate(("DONE", "RETRY", "SPLIT")):
        checkpoint = CandidateChunkCheckpoint(
            chunk_key=str(index) * 64, parent_chunk_key=None, status=status,
            paths=("fw.c",), change_summary="legacy output", findings=(),
            input_tokens=100, output_tokens=50, llm_calls=3, tool_calls=2,
        )
        await jobs.save_candidate_chunk_checkpoint(
            job.id, worker_id="reviewer", checkpoint_version="candidate-v1", checkpoint=checkpoint,
        )
    present, usage = await backend.legacy()
    assert present and usage.llm_calls == 3 and usage.tool_calls == 2
    checkpoints = await jobs.load_candidate_chunk_checkpoints(
        job.id,
        checkpoint_version="candidate-v1",
    )
    assert len(checkpoints) == 3


@pytest.mark.asyncio
async def test_progress_cleanup_keeps_invocation_audit_and_failed_job_recovery(store):
    jobs, database = store
    job, backend = await _progress_backend(jobs)
    progress = ReviewProgress(input_key="1" * 64)
    await backend.save(progress)
    await backend.record_invocation(Invocation(
        id=str(uuid.uuid4()), input_key=progress.input_key,
        phase="candidate", work_key="2" * 64, kind="llm",
    ))
    async with database.session() as session:
        await session.execute(
            text("UPDATE review_progress SET updated_at = now() - interval '40 days'")
        )
        await session.execute(text("UPDATE review_jobs SET state = 'FAILED_PERMANENT'"))
        await session.commit()
    assert await jobs.prune_review_recovery_cache(retention_days=30) == 0
    assert await backend.load(progress.input_key) is not None
    async with database.session() as session:
        await session.execute(text("UPDATE review_jobs SET state = 'DONE'"))
        await session.commit()
    assert await jobs.prune_review_recovery_cache(retention_days=30) == 1
    assert await backend.load(progress.input_key) is None
    assert (await backend.totals())["llm_calls"] == 1


async def _save_done_review(
    jobs: JobStore,
    event: GerritPatchsetEvent,
    review: ReviewResult,
    *,
    worker_prefix: str,
):
    job, _ = await jobs.enqueue(event, review_policy_version="firmware-v1")
    review_worker = f"{worker_prefix}-review"
    claim = await jobs.claim_next(worker_id=review_worker, lease_seconds=120)
    assert claim is not None and claim.id == job.id
    await jobs.transition(job.id, JobState.FETCHING, worker_id=review_worker)
    await jobs.transition(job.id, JobState.REVIEWING, worker_id=review_worker)
    await jobs.transition(job.id, JobState.VALIDATING, worker_id=review_worker)
    await jobs.save_review_result_and_mark_ready(job.id, review, worker_id=review_worker)

    publish_worker = f"{worker_prefix}-publish"
    publish_claim = await jobs.claim_next(worker_id=publish_worker, lease_seconds=120)
    assert publish_claim is not None and publish_claim.id == job.id
    await jobs.transition(job.id, JobState.PUBLISHING, worker_id=publish_worker)
    await jobs.mark_done(job.id, worker_id=publish_worker)
    return job


async def _expire_job_lease(database: Database, job_id) -> None:
    async with database.session() as session:
        await session.execute(
            text(
                "UPDATE review_jobs SET lease_expires_at = now() - interval '1 second' "
                "WHERE id = CAST(:job_id AS uuid)"
            ),
            {"job_id": str(job_id)},
        )
        await session.commit()
