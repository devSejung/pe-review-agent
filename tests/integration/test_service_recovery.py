from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from pe_review_agent.config import Settings
from pe_review_agent.db import Database
from pe_review_agent.domain import (
    AttemptStage,
    ChangedLine,
    Finding,
    FindingLocation,
    GerritPatchsetEvent,
    JobState,
    ReviewContext,
    ReviewResult,
    Severity,
)
from pe_review_agent.gerrit import SupersededRevisionError
from pe_review_agent.jobs import JobStore, PublicationStatus
from pe_review_agent.repos.manager import RepositoryWorkspace
from pe_review_agent.retry import PermanentError, TransientError
from pe_review_agent.service import ReviewWorker

DSN = os.environ.get("PE_REVIEW_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="PE_REVIEW_TEST_POSTGRES_DSN is not configured")


class _Lease:
    def ensure(self) -> None:
        return None


class _FakeRepos:
    def __init__(self, root: Path) -> None:
        self.root = root

    @asynccontextmanager
    async def workspace(self, **kwargs):
        yield RepositoryWorkspace(
            project="team/fw",
            revision_sha=kwargs["revision_sha"],
            base_revision_sha="b" * 40,
            root=self.root,
            mirror=self.root / "mirror.git",
            diff="+advance();\n",
            changed_files=["fw/train.c"],
            changed_lines=[ChangedLine(path="fw/train.c", line=2, text="advance();")],
        )

    def to_review_context(
        self,
        workspace: RepositoryWorkspace,
        *,
        change_number: int,
        patchset_number: int,
        subject: str | None,
        branch: str | None,
        commit_message: str | None,
        policy_text: str,
    ) -> ReviewContext:
        return ReviewContext(
            project=workspace.project,
            change_number=change_number,
            patchset_number=patchset_number,
            revision_sha=workspace.revision_sha,
            base_revision_sha=workspace.base_revision_sha,
            subject=subject,
            branch=branch,
            commit_message=commit_message,
            diff=workspace.diff,
            changed_files=workspace.changed_files,
            changed_lines=workspace.changed_lines,
            policy_text=policy_text,
            repository_root=str(workspace.root),
        )

    async def read_text_at_revision(
        self,
        _root: Path,
        _revision_sha: str,
        _relative_path: str,
        *,
        max_bytes: int,
    ) -> str | None:
        assert max_bytes > 0
        return None


class _FakeEngine:
    def __init__(self) -> None:
        self.calls = 0
        self.summary = "Timeout result must be handled before advancing."

    async def review(self, _context, _tools, *, tool_trace=None) -> ReviewResult:
        self.calls += 1
        return ReviewResult(
            summary=self.summary,
            model="Qwen3.6-27B",
            findings=[
                Finding(
                    severity=Severity.P1,
                    category="timeout",
                    title="Timeout is ignored",
                    message="Execution advances after a timeout.",
                    impact="Stale training state can be consumed.",
                    evidence="poll_done() can return -ETIMEDOUT.",
                    remediation="Handle the error before advance().",
                    location=FindingLocation(path="fw/train.c", start_line=2),
                    confidence=0.97,
                )
            ],
        )


class _HistoryAwareFakeEngine:
    def __init__(self) -> None:
        self.contexts: list[ReviewContext] = []

    async def review(self, context: ReviewContext, _tools, *, tool_trace=None) -> ReviewResult:
        self.contexts.append(context)
        semantic_id = (
            context.previous_findings[0].semantic_id if context.previous_findings else None
        )
        return ReviewResult(
            summary="Timeout defect remains actionable.",
            model="Qwen3.6-27B",
            findings=[
                Finding(
                    severity=Severity.P1,
                    category="timeout",
                    title="Timeout is ignored",
                    message="Execution advances after a timeout.",
                    impact="Stale training state can be consumed.",
                    evidence="poll_done() can return -ETIMEDOUT.",
                    remediation="Handle the error before advance().",
                    location=FindingLocation(path="fw/train.c", start_line=2),
                    confidence=0.97,
                    semantic_id=semantic_id,
                )
            ],
        )


class _FakeGerrit:
    def __init__(self) -> None:
        self.publish_calls = 0
        self.already_published = False
        self.publish_error: Exception | None = TransientError(
            "response lost", retry_after_seconds=0
        )
        self.seen_recovery_tags: list[str | None] = []
        self.seen_recovery_summaries: list[str] = []
        self.seen_payloads: list[dict] = []
        self.recovery_error: Exception | None = None
        self.ensure_error: Exception | None = None
        self.before_pre_post_guard = None
        self.after_post_side_effect = None

    async def ensure_current_revision(self, *_args, **_kwargs):
        if self.ensure_error is not None:
            raise self.ensure_error
        return SimpleNamespace(
            ref="refs/changes/01/101/1",
            subject="Test timeout path",
            branch="main",
            commit_message="Test timeout path\n\nExercise retry recovery.",
        )

    async def has_published_review(self, **_kwargs) -> bool:
        self.seen_recovery_tags.append(_kwargs.get("tag"))
        self.seen_recovery_summaries.append(_kwargs["summary"])
        if self.recovery_error is not None:
            raise self.recovery_error
        return self.already_published

    async def publish_review_input(self, **_kwargs):
        callback = self.before_pre_post_guard
        if callback is not None:
            await callback()
        guard = _kwargs.get("pre_post_guard")
        if guard is not None and not await guard():
            raise SupersededRevisionError(
                project=_kwargs["project"],
                change_number=_kwargs["change_number"],
                expected=_kwargs["revision_sha"],
                actual="newer-patchset-known-by-worker",
            )
        self.publish_calls += 1
        self.seen_payloads.append(dict(_kwargs["payload"]))
        callback = self.after_post_side_effect
        if callback is not None:
            await callback()
        if self.publish_error is not None:
            raise self.publish_error
        return {"labels": {}}


def _settings(
    tmp_path: Path,
    *,
    fetch_attempts: int = 3,
    review_attempts: int = 3,
    publish_attempts: int = 3,
) -> Settings:
    assert DSN is not None
    return Settings.model_validate(
        {
            "gerrit": {
                "ssh_host": "gerrit",
                "ssh_user": "bot",
                "ssh_key_path": str(tmp_path / "key"),
                "rest_url": "https://gerrit",
                "projects": ["team/fw"],
            },
            "llm": {"base_url": "https://llm/v1"},
            "database": {"dsn": DSN},
            "repos": {
                "cache_root": str(tmp_path / "repos"),
                "work_root": str(tmp_path / "work"),
            },
            "retry": {
                "fetch_attempts": fetch_attempts,
                "review_attempts": review_attempts,
                "publish_attempts": publish_attempts,
                "base_seconds": 0.01,
                "max_seconds": 0.1,
                "jitter_ratio": 0,
            },
        }
    )


@pytest.mark.parametrize("publish_attempts", [1, 3])
@pytest.mark.asyncio
async def test_publish_response_loss_recovers_without_rerunning_review(
    tmp_path: Path, publish_attempts: int
) -> None:
    assert DSN is not None
    settings = _settings(tmp_path, publish_attempts=publish_attempts)
    database = Database(settings.database)
    async with database.session() as session:
        await session.execute(
            text(
                "TRUNCATE review_managed_projects, review_publications, review_findings, "
                "review_results, "
                "review_attempts, review_jobs RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()

    store = JobStore(database.sessions)
    engine = _FakeEngine()
    gerrit = _FakeGerrit()
    source = tmp_path / "fw" / "train.c"
    source.parent.mkdir(parents=True)
    source.write_text("int rc = poll_done();\nadvance();\n", encoding="utf-8")
    worker = ReviewWorker(
        settings,
        store,
        gerrit,  # type: ignore[arg-type]
        _FakeRepos(tmp_path),  # type: ignore[arg-type]
        engine,  # type: ignore[arg-type]
    )
    event = GerritPatchsetEvent(
        project="team/fw",
        change_number=101,
        patchset_number=1,
        revision_sha="a" * 40,
        ref="refs/changes/01/101/1",
        branch="main",
    )
    job, _ = await store.enqueue(event, review_policy_version=settings.review.policy_version)

    review_claim = await store.claim_next(worker_id="reviewer", lease_seconds=120)
    assert review_claim is not None
    await worker._process_review(review_claim, worker_id="reviewer", lease=_Lease())  # noqa: SLF001
    ready = await store.get(job.id)
    assert ready is not None and ready.state == JobState.READY_TO_PUBLISH
    assert engine.calls == 1

    publish_claim = await store.claim_next(worker_id="publisher-1", lease_seconds=120)
    assert publish_claim is not None
    await worker._process_publish(  # noqa: SLF001
        publish_claim, worker_id="publisher-1", lease=_Lease()
    )
    waiting = await store.get(job.id)
    assert waiting is not None and waiting.state == JobState.RETRY_WAIT
    publication = await store.publication_for_job(job.id)
    assert publication is not None
    assert publication.status == PublicationStatus.AMBIGUOUS.value
    assert publication.request_payload["tag"] == "autogenerated:pe-ai-review~firmware-v1"
    assert gerrit.publish_calls == 1
    assert engine.calls == 1

    # The first HTTP response was lost, but Gerrit did commit the review. On recovery the worker
    # discovers the existing tagged Patch Set message and completes the durable job without POSTing
    # or invoking the model again.
    gerrit.already_published = True
    publish_claim_2 = await store.claim_next(worker_id="publisher-2", lease_seconds=120)
    assert publish_claim_2 is not None and publish_claim_2.state == JobState.PUBLISHING
    await worker._process_publish(  # noqa: SLF001
        publish_claim_2, worker_id="publisher-2", lease=_Lease()
    )

    done = await store.get(job.id)
    assert done is not None and done.state == JobState.DONE
    assert gerrit.publish_calls == 1
    assert engine.calls == 1
    assert gerrit.seen_recovery_tags[-1] == "autogenerated:pe-ai-review~firmware-v1"
    await database.close()


@pytest.mark.asyncio
async def test_ambiguous_recovery_matches_exact_durable_truncated_message(tmp_path: Path) -> None:
    assert DSN is not None
    settings = _settings(tmp_path)
    settings.gerrit.max_comment_bytes = 1024
    database = Database(settings.database)
    await _truncate(database)
    store = JobStore(database.sessions)
    engine = _FakeEngine()
    engine.summary = "요약" * 2000
    gerrit = _FakeGerrit()
    _write_source(tmp_path)
    worker = ReviewWorker(
        settings,
        store,
        gerrit,  # type: ignore[arg-type]
        _FakeRepos(tmp_path),  # type: ignore[arg-type]
        engine,  # type: ignore[arg-type]
    )
    job = await _enqueue_default(store, settings)
    await _run_review_once(worker, store)

    first_publish = await store.claim_next(worker_id="publisher-1", lease_seconds=120)
    assert first_publish is not None
    await worker._process_publish(  # noqa: SLF001
        first_publish, worker_id="publisher-1", lease=_Lease()
    )
    publication = await store.publication_for_job(job.id)
    assert publication is not None and publication.status == PublicationStatus.AMBIGUOUS.value
    durable_message = publication.request_payload["message"]
    assert isinstance(durable_message, str)
    assert durable_message != engine.summary
    assert len(durable_message.encode("utf-8")) <= settings.gerrit.max_comment_bytes

    gerrit.already_published = True
    second_claim = await store.claim_next(worker_id="publisher-2", lease_seconds=120)
    assert second_claim is not None
    await worker._process_publish(  # noqa: SLF001
        second_claim, worker_id="publisher-2", lease=_Lease()
    )

    done = await store.get(job.id)
    assert done is not None and done.state == JobState.DONE
    assert gerrit.publish_calls == 1
    assert gerrit.seen_recovery_summaries[-1] == durable_message
    await database.close()


@pytest.mark.asyncio
async def test_ambiguous_recovery_closed_change_terminates_after_proving_post_absent(
    tmp_path: Path,
) -> None:
    assert DSN is not None
    settings = _settings(tmp_path, fetch_attempts=1, publish_attempts=2)
    database = Database(settings.database)
    await _truncate(database)
    store = JobStore(database.sessions)
    engine = _FakeEngine()
    gerrit = _FakeGerrit()
    _write_source(tmp_path)
    worker = ReviewWorker(
        settings,
        store,
        gerrit,  # type: ignore[arg-type]
        _FakeRepos(tmp_path),  # type: ignore[arg-type]
        engine,  # type: ignore[arg-type]
    )
    job = await _enqueue_default(store, settings)
    await _run_review_once(worker, store)

    first_publish = await store.claim_next(worker_id="publisher-1", lease_seconds=120)
    assert first_publish is not None
    await worker._process_publish(  # noqa: SLF001
        first_publish, worker_id="publisher-1", lease=_Lease()
    )
    publication = await store.publication_for_job(job.id)
    assert publication is not None and publication.status == PublicationStatus.AMBIGUOUS.value

    # Gerrit can still list messages for a closed Change. Once that lookup proves the ambiguous
    # payload absent, a closed status is a permanent publish failure rather than unresolved
    # uncertainty that should block the queue forever.
    gerrit.already_published = False
    gerrit.ensure_error = PermanentError("Gerrit change team/fw~101 is not open (status=MERGED)")
    second_claim = await store.claim_next(worker_id="publisher-2", lease_seconds=120)
    assert second_claim is not None
    await worker._process_publish(  # noqa: SLF001
        second_claim, worker_id="publisher-2", lease=_Lease()
    )

    failed = await store.get(job.id)
    publication = await store.publication_for_job(job.id)
    assert failed is not None and failed.state == JobState.FAILED_PERMANENT
    assert publication is not None and publication.status == PublicationStatus.FAILED.value
    assert len(gerrit.seen_recovery_summaries) >= 2
    assert gerrit.publish_calls == 1
    await database.close()


@pytest.mark.asyncio
async def test_final_publish_budget_fails_only_after_recovery_proves_absent(
    tmp_path: Path,
) -> None:
    assert DSN is not None
    settings = _settings(tmp_path, publish_attempts=1)
    database = Database(settings.database)
    await _truncate(database)
    store = JobStore(database.sessions)
    engine = _FakeEngine()
    gerrit = _FakeGerrit()
    _write_source(tmp_path)
    worker = ReviewWorker(
        settings,
        store,
        gerrit,  # type: ignore[arg-type]
        _FakeRepos(tmp_path),  # type: ignore[arg-type]
        engine,  # type: ignore[arg-type]
    )
    job = await _enqueue_default(store, settings)
    await _run_review_once(worker, store)

    first_publish = await store.claim_next(worker_id="publisher-1", lease_seconds=120)
    assert first_publish is not None
    await worker._process_publish(  # noqa: SLF001
        first_publish, worker_id="publisher-1", lease=_Lease()
    )
    assert gerrit.publish_calls == 1

    # Gerrit did not persist the ambiguous request. Recovery is still allowed, but a second POST is
    # forbidden because the only configured publish attempt has been consumed.
    second_claim = await store.claim_next(worker_id="publisher-2", lease_seconds=120)
    assert second_claim is not None
    await worker._process_publish(  # noqa: SLF001
        second_claim, worker_id="publisher-2", lease=_Lease()
    )
    failed = await store.get(job.id)
    assert failed is not None and failed.state == JobState.FAILED_PERMANENT
    assert gerrit.publish_calls == 1
    assert engine.calls == 1
    await database.close()


@pytest.mark.asyncio
async def test_ambiguous_post_reconciliation_survives_transient_get_budget_exhaustion(
    tmp_path: Path,
) -> None:
    assert DSN is not None
    settings = _settings(tmp_path, fetch_attempts=1, publish_attempts=2)
    database = Database(settings.database)
    await _truncate(database)
    store = JobStore(database.sessions)
    engine = _FakeEngine()
    gerrit = _FakeGerrit()
    _write_source(tmp_path)
    worker = ReviewWorker(
        settings,
        store,
        gerrit,  # type: ignore[arg-type]
        _FakeRepos(tmp_path),  # type: ignore[arg-type]
        engine,  # type: ignore[arg-type]
    )
    job = await _enqueue_default(store, settings)
    await _run_review_once(worker, store)

    first_publish = await store.claim_next(worker_id="publisher-1", lease_seconds=120)
    assert first_publish is not None
    await worker._process_publish(  # noqa: SLF001
        first_publish, worker_id="publisher-1", lease=_Lease()
    )
    publication = await store.publication_for_job(job.id)
    assert publication is not None and publication.status == PublicationStatus.AMBIGUOUS.value

    gerrit.recovery_error = TransientError("Gerrit GET unavailable", retry_after_seconds=0)
    for index in range(2):
        worker_id = f"reconciler-{index}"
        claim = await store.claim_next(worker_id=worker_id, lease_seconds=120)
        assert claim is not None and claim.state == JobState.PUBLISHING
        await worker._process_publish(claim, worker_id=worker_id, lease=_Lease())  # noqa: SLF001
        waiting = await store.get(job.id)
        assert waiting is not None and waiting.state == JobState.RETRY_WAIT
        assert waiting.retry_state == JobState.PUBLISHING

    assert await store.count_consumed_retry_attempts(job.id, stage=AttemptStage.RECONCILE) >= 2
    await database.close()


@pytest.mark.asyncio
async def test_ambiguous_post_reconciliation_preserves_uncertainty_on_permanent_get_error(
    tmp_path: Path,
) -> None:
    assert DSN is not None
    settings = _settings(tmp_path, fetch_attempts=1, publish_attempts=2)
    database = Database(settings.database)
    await _truncate(database)
    store = JobStore(database.sessions)
    engine = _FakeEngine()
    gerrit = _FakeGerrit()
    _write_source(tmp_path)
    worker = ReviewWorker(
        settings,
        store,
        gerrit,  # type: ignore[arg-type]
        _FakeRepos(tmp_path),  # type: ignore[arg-type]
        engine,  # type: ignore[arg-type]
    )
    job = await _enqueue_default(store, settings)
    await _run_review_once(worker, store)

    first_publish = await store.claim_next(worker_id="publisher-1", lease_seconds=120)
    assert first_publish is not None
    await worker._process_publish(  # noqa: SLF001
        first_publish, worker_id="publisher-1", lease=_Lease()
    )
    publication = await store.publication_for_job(job.id)
    assert publication is not None and publication.status == PublicationStatus.AMBIGUOUS.value

    gerrit.recovery_error = PermanentError("Gerrit GET forbidden")
    claim = await store.claim_next(worker_id="reconciler", lease_seconds=120)
    assert claim is not None and claim.state == JobState.PUBLISHING
    await worker._process_publish(claim, worker_id="reconciler", lease=_Lease())  # noqa: SLF001

    waiting = await store.get(job.id)
    publication = await store.publication_for_job(job.id)
    assert waiting is not None and waiting.state == JobState.RETRY_WAIT
    assert waiting.retry_state == JobState.PUBLISHING
    assert publication is not None and publication.status == PublicationStatus.AMBIGUOUS.value
    await database.close()


@pytest.mark.asyncio
async def test_publishing_recovery_reuses_durable_payload_after_config_change(
    tmp_path: Path,
) -> None:
    assert DSN is not None
    settings = _settings(tmp_path, publish_attempts=3)
    database = Database(settings.database)
    await _truncate(database)
    store = JobStore(database.sessions)
    engine = _FakeEngine()
    gerrit = _FakeGerrit()
    _write_source(tmp_path)
    worker = ReviewWorker(
        settings,
        store,
        gerrit,  # type: ignore[arg-type]
        _FakeRepos(tmp_path),  # type: ignore[arg-type]
        engine,  # type: ignore[arg-type]
    )
    job = await _enqueue_default(store, settings)
    await _run_review_once(worker, store)

    first_publish = await store.claim_next(worker_id="publisher-1", lease_seconds=120)
    assert first_publish is not None
    await worker._process_publish(  # noqa: SLF001
        first_publish, worker_id="publisher-1", lease=_Lease()
    )
    publication = await store.publication_for_job(job.id)
    assert publication is not None
    original_payload = dict(publication.request_payload)

    # Simulate a deployment changing current config between the ambiguous POST and recovery.
    settings.gerrit.review_tag = "autogenerated:new-config"
    settings.gerrit.notify = "NONE"
    gerrit.publish_error = None
    second_claim = await store.claim_next(worker_id="publisher-2", lease_seconds=120)
    assert second_claim is not None
    await worker._process_publish(  # noqa: SLF001
        second_claim, worker_id="publisher-2", lease=_Lease()
    )

    done = await store.get(job.id)
    assert done is not None and done.state == JobState.DONE
    assert gerrit.publish_calls == 2
    assert gerrit.seen_payloads[-1] == original_payload
    assert gerrit.seen_recovery_tags[-1] == original_payload["tag"]
    await database.close()


@pytest.mark.asyncio
async def test_crash_abandoned_fetch_attempt_consumes_retry_budget(tmp_path: Path) -> None:
    assert DSN is not None
    settings = _settings(tmp_path, fetch_attempts=1)
    database = Database(settings.database)
    await _truncate(database)
    store = JobStore(database.sessions)
    engine = _FakeEngine()
    gerrit = _FakeGerrit()
    _write_source(tmp_path)
    worker = ReviewWorker(
        settings,
        store,
        gerrit,  # type: ignore[arg-type]
        _FakeRepos(tmp_path),  # type: ignore[arg-type]
        engine,  # type: ignore[arg-type]
    )
    job = await _enqueue_default(store, settings)
    first_claim = await store.claim_next(worker_id="crashed-worker", lease_seconds=120)
    assert first_claim is not None
    first_claim = await store.transition(job.id, JobState.FETCHING, worker_id="crashed-worker")
    await store.start_attempt(
        job.id,
        stage=AttemptStage.FETCH,
        worker_id="crashed-worker",
    )
    await _expire_lease(database, job.id)

    reclaimed = await store.claim_next(worker_id="replacement", lease_seconds=120)
    assert reclaimed is not None and reclaimed.state == JobState.FETCHING
    await worker._process_review(  # noqa: SLF001
        reclaimed, worker_id="replacement", lease=_Lease()
    )
    failed = await store.get(job.id)
    assert failed is not None and failed.state == JobState.FAILED_PERMANENT
    assert engine.calls == 0
    await database.close()


@pytest.mark.asyncio
async def test_new_patchset_known_at_final_pre_post_guard_prevents_post(tmp_path: Path) -> None:
    assert DSN is not None
    settings = _settings(tmp_path)
    database = Database(settings.database)
    await _truncate(database)
    store = JobStore(database.sessions)
    engine = _FakeEngine()
    gerrit = _FakeGerrit()
    gerrit.publish_error = None
    _write_source(tmp_path)
    worker = ReviewWorker(
        settings,
        store,
        gerrit,  # type: ignore[arg-type]
        _FakeRepos(tmp_path),  # type: ignore[arg-type]
        engine,  # type: ignore[arg-type]
    )
    old_job = await _enqueue_default(store, settings)
    await _run_review_once(worker, store)

    async def inject_new_patchset() -> None:
        await store.enqueue(
            GerritPatchsetEvent(
                project="team/fw",
                change_number=101,
                patchset_number=2,
                revision_sha="c" * 40,
                ref="refs/changes/01/101/2",
                branch="main",
            ),
            review_policy_version=settings.review.policy_version,
        )

    gerrit.before_pre_post_guard = inject_new_patchset
    publish_claim = await store.claim_next(worker_id="publisher", lease_seconds=120)
    assert publish_claim is not None
    await worker._process_publish(  # noqa: SLF001
        publish_claim, worker_id="publisher", lease=_Lease()
    )
    old_after = await store.get(old_job.id)
    assert old_after is not None and old_after.state == JobState.SUPERSEDED
    assert gerrit.publish_calls == 0
    await database.close()


@pytest.mark.asyncio
async def test_persisting_finding_is_tracked_but_not_reposted_inline(tmp_path: Path) -> None:
    assert DSN is not None
    settings = _settings(tmp_path)
    database = Database(settings.database)
    await _truncate(database)
    store = JobStore(database.sessions)
    engine = _HistoryAwareFakeEngine()
    gerrit = _FakeGerrit()
    gerrit.publish_error = None
    _write_source(tmp_path)
    worker = ReviewWorker(
        settings,
        store,
        gerrit,  # type: ignore[arg-type]
        _FakeRepos(tmp_path),  # type: ignore[arg-type]
        engine,  # type: ignore[arg-type]
    )

    ps1 = await _enqueue_default(store, settings)
    await _run_review_once(worker, store)
    ps1_publish = await store.claim_next(worker_id="ps1-publisher", lease_seconds=120)
    assert ps1_publish is not None
    await worker._process_publish(  # noqa: SLF001
        ps1_publish, worker_id="ps1-publisher", lease=_Lease()
    )
    ps1_done = await store.get(ps1.id)
    assert ps1_done is not None and ps1_done.state == JobState.DONE
    ps1_result = await store.load_review_result(ps1.id)
    assert ps1_result is not None
    semantic_id = ps1_result.findings[0].semantic_id
    assert semantic_id is not None
    assert "comments" in gerrit.seen_payloads[-1]

    ps2_event = GerritPatchsetEvent(
        project="team/fw",
        change_number=101,
        patchset_number=2,
        revision_sha="c" * 40,
        ref="refs/changes/01/101/2",
        branch="main",
    )
    ps2, _ = await store.enqueue(ps2_event, review_policy_version=settings.review.policy_version)
    await _run_review_once(worker, store)
    ps2_result = await store.load_review_result(ps2.id)
    assert ps2_result is not None
    assert engine.contexts[-1].previous_patchset_number == 1
    assert engine.contexts[-1].previous_findings[0].semantic_id == semantic_id
    assert ps2_result.findings[0].semantic_id == semantic_id
    assert ps2_result.findings[0].lineage.value == "PERSISTING"
    assert "1 still present" in ps2_result.summary

    ps2_publish = await store.claim_next(worker_id="ps2-publisher", lease_seconds=120)
    assert ps2_publish is not None
    await worker._process_publish(  # noqa: SLF001
        ps2_publish, worker_id="ps2-publisher", lease=_Lease()
    )
    ps2_done = await store.get(ps2.id)
    assert ps2_done is not None and ps2_done.state == JobState.DONE
    assert "comments" not in gerrit.seen_payloads[-1]
    assert "1 still present" in gerrit.seen_payloads[-1]["message"]
    assert len(gerrit.seen_payloads) == 2
    await database.close()


@pytest.mark.asyncio
async def test_new_patchset_after_external_post_keeps_published_job_as_done(
    tmp_path: Path,
) -> None:
    """A PS arriving after Gerrit accepted the review must not erase the published baseline."""
    assert DSN is not None
    settings = _settings(tmp_path)
    database = Database(settings.database)
    await _truncate(database)
    store = JobStore(database.sessions)
    engine = _FakeEngine()
    gerrit = _FakeGerrit()
    gerrit.publish_error = None
    _write_source(tmp_path)
    worker = ReviewWorker(
        settings,
        store,
        gerrit,  # type: ignore[arg-type]
        _FakeRepos(tmp_path),  # type: ignore[arg-type]
        engine,  # type: ignore[arg-type]
    )

    ps1 = await _enqueue_default(store, settings)
    await _run_review_once(worker, store)

    ps2_holder: dict[str, object] = {}

    async def enqueue_ps2_after_gerrit_side_effect() -> None:
        ps2, _ = await store.enqueue(
            GerritPatchsetEvent(
                project="team/fw",
                change_number=101,
                patchset_number=2,
                revision_sha="c" * 40,
                ref="refs/changes/01/101/2",
                branch="main",
            ),
            review_policy_version=settings.review.policy_version,
        )
        ps2_holder["job"] = ps2

    gerrit.after_post_side_effect = enqueue_ps2_after_gerrit_side_effect
    claim = await store.claim_next(worker_id="publisher", lease_seconds=120)
    assert claim is not None and claim.id == ps1.id
    await worker._process_publish(claim, worker_id="publisher", lease=_Lease())  # noqa: SLF001

    ps1_after = await store.get(ps1.id)
    assert ps1_after is not None and ps1_after.state == JobState.DONE
    ps2 = ps2_holder["job"]
    assert hasattr(ps2, "id")
    ps2_after = await store.get(ps2.id)  # type: ignore[union-attr]
    assert ps2_after is not None and ps2_after.state == JobState.RECEIVED
    history = await store.load_finding_history(ps2.id)  # type: ignore[union-attr]
    assert history.baseline_patchset == 1
    assert len(history.previous_findings) == 1
    await database.close()


@pytest.mark.asyncio
async def test_durable_failed_publication_is_not_reposted_after_restart(tmp_path: Path) -> None:
    assert DSN is not None
    settings = _settings(tmp_path)
    database = Database(settings.database)
    await _truncate(database)
    store = JobStore(database.sessions)
    engine = _FakeEngine()
    gerrit = _FakeGerrit()
    gerrit.publish_error = None
    _write_source(tmp_path)
    worker = ReviewWorker(
        settings,
        store,
        gerrit,  # type: ignore[arg-type]
        _FakeRepos(tmp_path),  # type: ignore[arg-type]
        engine,  # type: ignore[arg-type]
    )

    job = await _enqueue_default(store, settings)
    await _run_review_once(worker, store)
    claim = await store.claim_next(worker_id="publisher", lease_seconds=120)
    assert claim is not None
    claim = await store.transition(job.id, JobState.PUBLISHING, worker_id="publisher")
    review = await store.load_review_result(job.id)
    assert review is not None
    publication = await store.begin_publication(
        job.id,
        worker_id="publisher",
        request_payload={
            "message": review.summary,
            "tag": "autogenerated:pe-ai-review~firmware-v1",
        },
        finding_fingerprints=[],
    )
    await store.mark_publication_failed(publication.id, error="HTTP 403 forbidden")

    # Simulate the crash gap before the job itself was marked FAILED_PERMANENT.
    await _expire_lease(database, job.id)
    restarted = await store.claim_next(worker_id="replacement", lease_seconds=120)
    assert restarted is not None and restarted.state == JobState.PUBLISHING
    await worker._process_publish(  # noqa: SLF001
        restarted, worker_id="replacement", lease=_Lease()
    )

    failed = await store.get(job.id)
    assert failed is not None and failed.state == JobState.FAILED_PERMANENT
    assert gerrit.publish_calls == 0
    await database.close()


async def _truncate(database: Database) -> None:
    async with database.session() as session:
        await session.execute(
            text(
                "TRUNCATE review_managed_projects, review_publications, review_findings, "
                "review_results, "
                "review_attempts, review_jobs RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()


def _write_source(tmp_path: Path) -> None:
    source = tmp_path / "fw" / "train.c"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("int rc = poll_done();\nadvance();\n", encoding="utf-8")


async def _enqueue_default(store: JobStore, settings: Settings):
    job, _ = await store.enqueue(
        GerritPatchsetEvent(
            project="team/fw",
            change_number=101,
            patchset_number=1,
            revision_sha="a" * 40,
            ref="refs/changes/01/101/1",
            branch="main",
        ),
        review_policy_version=settings.review.policy_version,
    )
    return job


async def _run_review_once(worker: ReviewWorker, store: JobStore) -> None:
    claim = await store.claim_next(worker_id="reviewer", lease_seconds=120)
    assert claim is not None
    await worker._process_review(claim, worker_id="reviewer", lease=_Lease())  # noqa: SLF001


async def _expire_lease(database: Database, job_id) -> None:
    async with database.session() as session:
        await session.execute(
            text(
                "UPDATE review_jobs SET lease_expires_at = now() - interval '1 second' "
                "WHERE id = CAST(:job_id AS uuid)"
            ),
            {"job_id": str(job_id)},
        )
        await session.commit()
