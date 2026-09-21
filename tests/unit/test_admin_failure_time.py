from datetime import UTC, datetime

from pe_review_agent.admin.store import _current_failure_at
from pe_review_agent.jobs.models import Attempt, Job, Publication


def test_current_failure_time_uses_newest_matching_failure_source() -> None:
    job = Job(
        project="team/fw",
        change_number=1,
        patchset_number=1,
        revision_sha="a" * 40,
        review_policy_version="firmware-v1",
        last_error="timeout",
    )
    job.updated_at = datetime(2026, 9, 21, 5, 30, tzinfo=UTC)
    publication = Publication(
        job_id=job.id,
        publication_fingerprint="f" * 64,
        status="AMBIGUOUS",
        request_payload={},
        finding_fingerprints=[],
        last_error="timeout",
    )
    publication.updated_at = datetime(2026, 9, 21, 5, 20, tzinfo=UTC)
    older = Attempt(
        job_id=job.id,
        attempt_number=1,
        stage="PUBLISH",
        worker_id="worker",
        error_message="timeout",
    )
    older.finished_at = datetime(2026, 9, 21, 5, 21, tzinfo=UTC)
    newer = Attempt(
        job_id=job.id,
        attempt_number=2,
        stage="RECONCILE",
        worker_id="worker",
        error_message="timeout",
    )
    newer.finished_at = datetime(2026, 9, 21, 5, 29, tzinfo=UTC)

    assert _current_failure_at(job, [older, newer], publication) == newer.finished_at


def test_current_failure_time_falls_back_to_job_update_for_budget_exhaustion() -> None:
    job = Job(
        project="team/fw",
        change_number=1,
        patchset_number=1,
        revision_sha="a" * 40,
        review_policy_version="firmware-v1",
        last_error="REVIEW retry budget exhausted after 4 failed/crashed attempts",
    )
    job.updated_at = datetime(2026, 9, 21, 5, 30, tzinfo=UTC)

    assert _current_failure_at(job, [], None) == job.updated_at
