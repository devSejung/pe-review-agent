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
    ChangedLine,
    Finding,
    FindingLocation,
    GerritPatchsetEvent,
    JobState,
    ReviewContext,
    ReviewResult,
    Severity,
)
from pe_review_agent.jobs import JobStore, PublicationStatus
from pe_review_agent.repos.manager import RepositoryWorkspace
from pe_review_agent.retry import TransientError
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
    async def workspace(self, **_kwargs):
        yield RepositoryWorkspace(
            project="team/fw",
            revision_sha="a" * 40,
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
            diff=workspace.diff,
            changed_files=workspace.changed_files,
            changed_lines=workspace.changed_lines,
            policy_text=policy_text,
            repository_root=str(workspace.root),
        )


class _FakeEngine:
    def __init__(self) -> None:
        self.calls = 0

    async def review(self, _context, _tools) -> ReviewResult:
        self.calls += 1
        return ReviewResult(
            summary="Timeout result must be handled before advancing.",
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


class _FakeGerrit:
    def __init__(self) -> None:
        self.publish_calls = 0
        self.already_published = False

    async def ensure_current_revision(self, *_args, **_kwargs):
        return SimpleNamespace(
            ref="refs/changes/01/101/1",
            subject="Test timeout path",
            branch="main",
        )

    async def has_published_review(self, **_kwargs) -> bool:
        return self.already_published

    async def publish_review_input(self, **_kwargs):
        self.publish_calls += 1
        # Simulate a lost/timeout response after Gerrit may have accepted the POST.
        raise TransientError("response lost", retry_after_seconds=0)


def _settings(tmp_path: Path) -> Settings:
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
                "fetch_attempts": 3,
                "review_attempts": 3,
                "publish_attempts": 3,
                "base_seconds": 0.01,
                "max_seconds": 0.1,
                "jitter_ratio": 0,
            },
        }
    )


@pytest.mark.asyncio
async def test_publish_response_loss_recovers_without_rerunning_review(tmp_path: Path) -> None:
    assert DSN is not None
    database = Database(_settings(tmp_path).database)
    async with database.session() as session:
        await session.execute(
            text(
                "TRUNCATE review_publications, review_findings, review_results, "
                "review_attempts, review_jobs RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()

    store = JobStore(database.sessions)
    engine = _FakeEngine()
    gerrit = _FakeGerrit()
    settings = _settings(tmp_path)
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
    await database.close()
