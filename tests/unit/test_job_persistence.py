from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql

from pe_review_agent.domain import AttemptStage, GerritPatchsetEvent, JobState
from pe_review_agent.jobs.models import Job
from pe_review_agent.jobs.state_machine import (
    InvalidJobTransition,
    require_transition,
    valid_retry_target,
)
from pe_review_agent.jobs.store import (
    JobRecord,
    _change_lock_select,
    _claim_select,
    _count_attempts_select,
    _enqueue_insert,
    _require_live_lease,
    publication_fingerprint,
)


def test_job_state_machine_allows_pipeline_and_retry() -> None:
    require_transition(JobState.RECEIVED, JobState.FETCHING)
    require_transition(JobState.FETCHING, JobState.REVIEWING)
    require_transition(JobState.REVIEWING, JobState.VALIDATING)
    require_transition(JobState.VALIDATING, JobState.READY_TO_PUBLISH)
    require_transition(JobState.READY_TO_PUBLISH, JobState.PUBLISHING)
    require_transition(JobState.PUBLISHING, JobState.DONE)
    require_transition(JobState.REVIEWING, JobState.RETRY_WAIT)
    require_transition(JobState.RETRY_WAIT, JobState.REVIEWING)
    assert valid_retry_target(JobState.PUBLISHING)


def test_terminal_state_cannot_be_reopened() -> None:
    with pytest.raises(InvalidJobTransition):
        require_transition(JobState.DONE, JobState.REVIEWING)
    with pytest.raises(InvalidJobTransition):
        require_transition(JobState.SUPERSEDED, JobState.PUBLISHING)


def test_enqueue_is_database_idempotent_on_durable_identity() -> None:
    event = GerritPatchsetEvent(
        project="soc/fw",
        change_number=42,
        patchset_number=3,
        revision_sha="0123456789abcdef",
    )
    sql = str(
        _enqueue_insert(event, "firmware-v1").compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}
        )
    )
    assert (
        "ON CONFLICT (project, change_number, revision_sha, review_policy_version) DO NOTHING"
        in sql
    )


def test_enqueue_serializes_patchsets_per_change() -> None:
    sql = str(
        _change_lock_select("soc/fw", 42).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "pg_advisory_xact_lock" in sql
    assert "hashtextextended('soc/fw:42', 0)" in sql


def test_claim_uses_skip_locked_and_due_lease_predicates() -> None:
    sql = str(_claim_select(datetime.now(UTC)).compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "lease_expires_at IS NULL" in sql
    assert "next_attempt_at" in sql


def test_attempt_count_is_scoped_to_job_and_stage() -> None:
    job_id = uuid.uuid4()
    sql = str(
        _count_attempts_select(job_id, AttemptStage.REVIEW).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "count(*)" in sql
    assert f"review_attempts.job_id = '{job_id}'" in sql
    assert "review_attempts.stage = 'REVIEW'" in sql


def test_live_lease_rejects_expired_or_wrong_worker() -> None:
    job = Job(
        id=uuid.uuid4(),
        project="soc/fw",
        change_number=42,
        patchset_number=3,
        revision_sha="0123456789abcdef",
        review_policy_version="firmware-v1",
        event_payload={},
        lease_owner="worker-a",
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    with pytest.raises(RuntimeError, match="expired"):
        _require_live_lease(job, "worker-a")

    job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
    with pytest.raises(RuntimeError, match="not leased"):
        _require_live_lease(job, "worker-b")


def test_publication_fingerprint_is_deterministic_and_identity_scoped() -> None:
    now = datetime.now(UTC)
    job = JobRecord(
        id=uuid.uuid4(),
        project="soc/fw",
        change_number=42,
        patchset_number=3,
        revision_sha="0123456789abcdef",
        review_policy_version="firmware-v1",
        state=JobState.READY_TO_PUBLISH,
        retry_state=None,
        attempt_count=1,
        next_attempt_at=now,
        lease_owner=None,
        lease_expires_at=None,
        event_payload={},
    )
    payload_a = {"message": "summary", "comments": {"a.c": [{"line": 7, "message": "bug"}]}}
    payload_b = {
        "comments": {"a.c": [{"message": "bug", "line": 7}]},
        "message": "summary",
    }

    assert publication_fingerprint(job, payload_a) == publication_fingerprint(job, payload_b)

    other = JobRecord(
        id=uuid.uuid4(),
        project=job.project,
        change_number=job.change_number,
        patchset_number=4,
        revision_sha="fedcba9876543210",
        review_policy_version=job.review_policy_version,
        state=job.state,
        retry_state=None,
        attempt_count=job.attempt_count,
        next_attempt_at=now,
        lease_owner=None,
        lease_expires_at=None,
        event_payload={},
    )
    assert publication_fingerprint(job, payload_a) != publication_fingerprint(other, payload_a)
