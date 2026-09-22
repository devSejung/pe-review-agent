from __future__ import annotations

import asyncio
import copy
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from pe_review_agent.admin import ControlStore
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
from pe_review_agent.gerrit import ChangeNotOpenError, SupersededRevisionError
from pe_review_agent.gerrit.votes import VoteObservation
from pe_review_agent.jobs import JobStore
from pe_review_agent.jobs.models import Job, ReviewVote
from pe_review_agent.jobs.project_policy import bind_project_review_policy
from pe_review_agent.repos.manager import RepositoryWorkspace
from pe_review_agent.retry import PermanentError, TransientError
from pe_review_agent.service import ReviewWorker
from pe_review_agent.voting import VotePublisher

DSN = os.environ.get("PE_REVIEW_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="PE_REVIEW_TEST_POSTGRES_DSN is not configured")
SHA = "a" * 40


class Repositories:
    def __init__(self, root):
        self.root = root
        (root / "fw.c").write_text("int rc = poll();\nadvance();\n", encoding="utf-8")

    @asynccontextmanager
    async def workspace(self, **kwargs):
        yield RepositoryWorkspace(
            project=kwargs["project"],
            revision_sha=kwargs["revision_sha"],
            base_revision_sha="b" * 40,
            root=self.root,
            mirror=self.root / "mirror.git",
            diff="+advance();\n",
            changed_files=["fw.c"],
            changed_lines=[ChangedLine(path="fw.c", line=2, text="advance();")],
        )

    def to_review_context(self, workspace, **kwargs):
        return ReviewContext(
            project=workspace.project,
            revision_sha=workspace.revision_sha,
            base_revision_sha=workspace.base_revision_sha,
            diff=workspace.diff,
            changed_files=workspace.changed_files,
            changed_lines=workspace.changed_lines,
            repository_root=str(self.root),
            **kwargs,
        )

    async def read_text_at_revision(self, *args, **kwargs):
        return None


class Engine:
    def __init__(self):
        self.language = "ko-KR"
        self.state = {
            "calls": [],
            "count": 0,
            "partial": False,
            "error": None,
            "skip": False,
            "hide_findings": False,
            "after_review": None,
        }

    def with_output_language(self, language):
        clone = copy.copy(self)
        clone.language = language
        return clone

    async def review(self, context, tools, **kwargs):
        self.state["calls"].append((context.project, self.language))
        if self.state["error"]:
            error, self.state["error"] = self.state["error"], None
            raise error
        findings = []
        if self.state["count"] and not self.state["hide_findings"]:
            findings = [
                Finding(
                    severity=Severity.P1,
                    category="timeout",
                    title="Ignored result",
                    message="poll error ignored",
                    impact="Stale state",
                    evidence="return unused",
                    location=FindingLocation(path="fw.c", start_line=2),
                    confidence=0.99,
                )
            ]
        metadata = {
            "validated_finding_count": self.state["count"],
            "lineage_complete": not self.state["partial"],
            "output_language": self.language,
            "review_budget": {"stop_reasons": ["max_tool_rounds"] if self.state["partial"] else []},
        }
        if self.state["skip"]:
            metadata["skipped_reason"] = "merge commit"
        if self.state["after_review"]:
            await self.state["after_review"]()
        return ReviewResult(summary="결과 / result", findings=findings, review_metadata=metadata)


class Gerrit:
    def __init__(self):
        self.current = SHA
        self.account_id = 7
        self.comment_posts = 0
        self.vote_posts = []
        self.marker = None
        self.value = 0
        self.permission = True
        self.lose_ack = False
        self.vote_error = None
        self.read_error = None
        self.before_vote = None
        self.ignore_label = False
        self.status = "NEW"

    async def ensure_current_revision(
        self, project, change_number, revision_sha, *, allow_merged=False, **kwargs
    ):
        if revision_sha != self.current:
            raise SupersededRevisionError(
                project=project,
                change_number=change_number,
                expected=revision_sha,
                actual=self.current,
            )
        if self.status != "NEW" and not (allow_merged and self.status == "MERGED"):
            raise ChangeNotOpenError(
                project=project,
                change_number=change_number,
                status=self.status,
            )
        return SimpleNamespace(
            ref="refs/changes/01/101/1",
            subject="training",
            branch="main",
            commit_message="training",
            status=self.status,
        )

    async def has_published_review(self, **kwargs):
        return self.comment_posts > 0

    async def authenticated_account_id(self):
        return self.account_id

    async def code_review_vote_observation(self, **kwargs):
        await self.ensure_current_revision(
            kwargs["project"],
            kwargs["change_number"],
            kwargs["revision_sha"],
            allow_merged=True,
        )
        if self.read_error:
            raise self.read_error
        marker = (kwargs["tag"], kwargs["marker"], kwargs["account_id"])
        return VoteObservation(
            self.value,
            self.permission and self.status == "NEW",
            self.marker == marker,
            True,
            self.status,
        )

    async def publish_review_input(self, **kwargs):
        payload = kwargs["payload"]
        is_vote = "labels" in payload
        if is_vote and self.before_vote:
            await self.before_vote()
        await self.ensure_current_revision(
            kwargs["project"],
            kwargs["change_number"],
            kwargs["revision_sha"],
            allow_merged=kwargs.get("allow_merged", False),
        )
        guard = kwargs.get("pre_post_guard")
        if guard and not await guard():
            raise SupersededRevisionError(
                project=kwargs["project"],
                change_number=kwargs["change_number"],
                expected=kwargs["revision_sha"],
                actual="newer",
            )
        if not is_vote:
            self.comment_posts += 1
            return {"labels": {}}
        self.vote_posts.append(copy.deepcopy(payload))
        if self.vote_error:
            raise self.vote_error
        self.marker = (payload["tag"], payload["message"], self.account_id)
        if self.ignore_label:
            return {"labels": {}}
        self.value = payload["labels"]["Code-Review"]
        if self.lose_ack:
            self.lose_ack = False
            raise TransientError("vote COMMIT acknowledgement lost", retry_after_seconds=0)
        return {"labels": {"Code-Review": self.value}}


@pytest.fixture
async def env(tmp_path):
    settings = Settings.model_validate(
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
            "repos": {"cache_root": str(tmp_path / "repos"), "work_root": str(tmp_path / "work")},
            "retry": {
                "base_seconds": 0.01,
                "max_seconds": 0.1,
                "jitter_ratio": 0,
                "publish_attempts": 2,
            },
        }
    )
    db = Database(settings.database)
    async with db.sessions.begin() as session:
        await session.execute(
            text(
                "TRUNCATE review_service_state, review_managed_projects, "
                "review_jobs RESTART IDENTITY CASCADE"
            )
        )
    store, control = JobStore(db.sessions), ControlStore(db.sessions)
    await control.ensure_bootstrap(settings)
    await control.set_project_review_policy(
        "team/fw", review_language="INHERIT", auto_code_review=True, expected_generation=0
    )
    engine, gerrit = Engine(), Gerrit()
    worker = ReviewWorker(settings, store, gerrit, Repositories(tmp_path), engine, control=control)
    job, _ = await store.enqueue(event(), review_policy_version="firmware-v1")
    try:
        yield SimpleNamespace(
            settings=settings,
            db=db,
            store=store,
            control=control,
            engine=engine,
            gerrit=gerrit,
            worker=worker,
            job=job,
        )
    finally:
        await db.close()


def event(**updates):
    return GerritPatchsetEvent(
        project="team/fw",
        change_number=101,
        patchset_number=updates.get("patchset", 1),
        revision_sha=updates.get("revision", SHA),
        ref=f"refs/changes/01/101/{updates.get('patchset', 1)}",
        branch="main",
    )


async def run_review(env):
    job = await env.store.claim_next(worker_id="reviewer", lease_seconds=120)
    assert job is not None
    await env.worker._process_review(
        job, worker_id="reviewer", lease=SimpleNamespace(ensure=lambda: None)
    )


async def run_publish(env):
    job = await env.store.claim_next(worker_id="publisher", lease_seconds=120)
    assert job is not None
    await env.worker._process_publish(
        job, worker_id="publisher", lease=SimpleNamespace(ensure=lambda: None)
    )


async def due(env):
    async with env.db.sessions.begin() as session:
        await session.execute(
            text(
                "UPDATE review_jobs SET next_attempt_at = now(), "
                "lease_expires_at = now() - interval '1 second'"
            )
        )


async def audit_vote(env):
    async with env.db.sessions() as session:
        return await session.get(ReviewVote, env.job.id)


@pytest.mark.parametrize(
    "count,partial,hidden,target",
    [
        (0, False, False, 1),
        (0, True, False, 1),
        (1, False, False, 0),
        (1, True, True, 0),
    ],
)
async def test_current_ps_vote_uses_actual_findings_including_partial_and_capped_results(
    env,
    count,
    partial,
    hidden,
    target,
):
    env.engine.state.update(count=count, partial=partial, hide_findings=hidden)
    env.gerrit.value = 1  # copied previous +1 must not prevent an explicit current-PS 0
    await run_review(env)
    await run_publish(env)
    vote = await audit_vote(env)
    assert vote.status == "APPLIED" and vote.value == target
    assert vote.finding_count == count
    assert env.gerrit.comment_posts == 1
    assert len(env.gerrit.vote_posts) == 1
    assert env.gerrit.vote_posts[0]["labels"] == {"Code-Review": target}
    assert (await env.store.get(env.job.id)).state == JobState.DONE
    assert len(env.engine.state["calls"]) == 1
    if partial and target == 1:
        assert "부분 검토" in env.gerrit.vote_posts[0]["message"]
    assert not any(k in env.gerrit.vote_posts[0] for k in ("on_behalf_of", "submit", "ready"))


async def test_explicit_zero_is_posted_even_when_bot_already_has_no_vote(env):
    env.engine.state["count"] = 1
    await run_review(env)
    await run_publish(env)
    assert env.gerrit.vote_posts[0]["labels"] == {"Code-Review": 0}


@pytest.mark.parametrize("skip,enabled", [(True, True), (False, False)])
async def test_skipped_and_disabled_review_never_calls_vote_api(env, skip, enabled):
    env.engine.state["skip"] = skip
    await env.control.set_project_review_policy(
        "team/fw", review_language="INHERIT", auto_code_review=enabled, expected_generation=1
    )
    await run_review(env)
    await run_publish(env)
    assert (await audit_vote(env)).status == "SKIPPED"
    assert not env.gerrit.vote_posts
    assert env.gerrit.comment_posts == 1


async def test_review_failure_has_no_vote_intent_and_no_post(env):
    env.engine.state["error"] = TransientError("invalid review JSON", retry_after_seconds=0)
    await run_review(env)
    assert (await env.store.get(env.job.id)).state == JobState.RETRY_WAIT
    assert await audit_vote(env) is None
    assert not env.gerrit.vote_posts and not env.gerrit.comment_posts


async def test_change_merged_after_review_still_posts_comments_and_skips_vote(env):
    await run_review(env)
    env.gerrit.status = "MERGED"

    await run_publish(env)

    vote = await audit_vote(env)
    assert env.gerrit.comment_posts == 1
    assert not env.gerrit.vote_posts
    assert vote is not None and vote.status == "SKIPPED"
    assert "merged after review" in (vote.last_error or "")
    assert (await env.store.publication_for_job(env.job.id)).status == "POSTED"
    assert (await env.store.get(env.job.id)).state == JobState.DONE


async def test_change_merged_during_inference_same_revision_still_publishes(env):
    async def merge_while_model_is_finishing():
        env.gerrit.status = "MERGED"

    env.engine.state["after_review"] = merge_while_model_is_finishing

    await run_review(env)
    assert env.gerrit.status == "MERGED"
    await run_publish(env)

    vote = await audit_vote(env)
    assert env.gerrit.comment_posts == 1
    assert not env.gerrit.vote_posts
    assert vote is not None and vote.status == "SKIPPED"
    assert (await env.store.get(env.job.id)).state == JobState.DONE


async def test_language_snapshot_survives_project_change_retry_and_restart(env):
    await env.control.set_project_review_policy(
        "team/fw", review_language="en-US", auto_code_review=True, expected_generation=1
    )
    env.engine.state["error"] = TransientError("model JSON invalid", retry_after_seconds=0)
    await run_review(env)
    bound = (await env.store.get(env.job.id)).project_review_policy
    assert bound["output_language"] == "en-US"
    await env.control.set_project_review_policy(
        "team/fw", review_language="ko-KR", auto_code_review=True, expected_generation=2
    )
    await run_review(env)
    assert env.engine.state["calls"] == [("team/fw", "en-US")] * 2
    assert env.engine.language == "ko-KR"
    assert (await env.store.get(env.job.id)).project_review_policy == bound
    await run_publish(env)
    assert "AI review vote" in env.gerrit.vote_posts[0]["message"]


async def test_pre_feature_in_progress_job_never_inherits_new_auto_vote(env):
    claim = await env.store.claim_next(worker_id="old", lease_seconds=120)
    await env.store.start_attempt(claim.id, stage=AttemptStage.REVIEW, worker_id="old")
    bound = await bind_project_review_policy(
        env.db.sessions, claim.id, worker_id="old", default_language="en-US"
    )
    assert bound.source == "legacy" and not bound.auto_code_review


async def test_migrated_pre_feature_fetching_job_without_review_attempt_stays_legacy(env):
    claim = await env.store.claim_next(worker_id="old", lease_seconds=120)
    assert claim is not None
    await env.store.transition(claim.id, JobState.FETCHING, worker_id="old")
    async with env.db.sessions.begin() as session:
        row = await session.get(Job, claim.id)
        assert row is not None
        row.project_review_policy = {"legacy_pre_feature": True}

    bound = await bind_project_review_policy(
        env.db.sessions, claim.id, worker_id="old", default_language="en-US"
    )
    assert bound.source == "legacy"
    assert bound.output_language == "en-US"
    assert bound.review_language == "INHERIT"
    assert not bound.auto_code_review
    async with env.db.sessions() as session:
        row = await session.get(Job, claim.id)
        assert row is not None
        assert row.project_review_policy == bound.model_dump(mode="json")


@pytest.mark.parametrize("failure", ["permission", "403", "ignored_label"])
async def test_vote_failure_preserves_comments_and_has_separate_audit(env, failure):
    if failure == "permission":
        env.gerrit.permission = False
    elif failure == "403":
        env.gerrit.vote_error = PermanentError("Gerrit REST POST failed HTTP 403")
    else:
        env.gerrit.ignore_label = True
    await run_review(env)
    await run_publish(env)
    vote = await audit_vote(env)
    assert vote.status == "FAILED" and vote.last_error
    assert env.gerrit.comment_posts == 1
    assert (await env.store.publication_for_job(env.job.id)).status == "POSTED"
    assert (await env.store.get(env.job.id)).state == JobState.DONE


async def test_lost_vote_ack_recovers_without_comment_or_llm_replay(env):
    env.gerrit.lose_ack = True
    await run_review(env)
    await run_publish(env)
    assert (await audit_vote(env)).status == "AMBIGUOUS"
    assert (await env.store.get(env.job.id)).state == JobState.RETRY_WAIT
    posted_at = (await env.store.publication_for_job(env.job.id)).posted_at
    await due(env)
    env.worker = ReviewWorker(
        env.settings, env.store, env.gerrit, env.worker.repos, env.engine, control=env.control
    )
    await run_publish(env)
    assert (await audit_vote(env)).status == "APPLIED"
    assert (await audit_vote(env)).response["recovered"] is True
    assert env.gerrit.comment_posts == 1 and len(env.gerrit.vote_posts) == 1
    assert (await env.store.publication_for_job(env.job.id)).posted_at == posted_at
    assert len(env.engine.state["calls"]) == 1


async def test_lost_vote_ack_recovers_after_change_merges_without_second_vote_post(env):
    env.gerrit.lose_ack = True
    await run_review(env)
    await run_publish(env)
    assert (await audit_vote(env)).status == "AMBIGUOUS"
    assert len(env.gerrit.vote_posts) == 1

    env.gerrit.status = "MERGED"
    await due(env)
    await run_publish(env)

    vote = await audit_vote(env)
    assert vote.status == "APPLIED"
    assert vote.response["recovered"] is True
    assert len(env.gerrit.vote_posts) == 1
    assert env.gerrit.comment_posts == 1


async def test_changed_bot_vote_is_not_overwritten_after_ambiguous_response(env):
    env.gerrit.lose_ack = True
    await run_review(env)
    await run_publish(env)
    env.gerrit.value = 0  # an operator changed this account's label after the original vote
    await due(env)
    await run_publish(env)
    assert (await audit_vote(env)).status == "FAILED"
    assert len(env.gerrit.vote_posts) == 1 and env.gerrit.value == 0


async def test_changed_rest_account_cannot_replay_vote_for_another_identity(env):
    env.gerrit.lose_ack = True
    await run_review(env)
    await run_publish(env)
    env.gerrit.account_id = 99
    await due(env)
    await run_publish(env)
    assert (await audit_vote(env)).status == "FAILED"
    assert (await audit_vote(env)).account_id == 7
    assert len(env.gerrit.vote_posts) == 1


async def test_newer_patchset_before_vote_prevents_post_without_erasing_comments(env):
    async def new_patchset():
        await env.store.enqueue(
            event(patchset=2, revision="c" * 40), review_policy_version="firmware-v1"
        )
        env.gerrit.current = "c" * 40

    env.gerrit.before_vote = new_patchset
    await run_review(env)
    await run_publish(env)
    assert not env.gerrit.vote_posts
    assert env.gerrit.comment_posts == 1
    assert (await audit_vote(env)).status == "SKIPPED"
    assert (await env.store.get(env.job.id)).state == JobState.DONE


async def test_inflight_vote_uses_snapshotted_policy_when_project_setting_changes(env):
    async def disable():
        await env.control.set_project_review_policy(
            "team/fw", review_language="INHERIT", auto_code_review=False, expected_generation=1
        )

    env.gerrit.before_vote = disable
    await run_review(env)
    await run_publish(env)
    assert len(env.gerrit.vote_posts) == 1
    assert env.gerrit.vote_posts[0]["labels"] == {"Code-Review": 1}
    assert (await audit_vote(env)).status == "APPLIED"
    assert env.gerrit.comment_posts == 1


async def test_vote_read_failures_have_a_bounded_budget_separate_from_review(env):
    env.gerrit.read_error = TransientError("Gerrit label lookup failed", retry_after_seconds=0)
    await run_review(env)
    await run_publish(env)
    assert (await env.store.get(env.job.id)).next_attempt_at > datetime.now(UTC)
    await due(env)
    await run_publish(env)
    assert (await audit_vote(env)).status == "FAILED"
    assert (await audit_vote(env)).retry_count == 2
    assert (await env.store.get(env.job.id)).state == JobState.DONE
    assert env.gerrit.comment_posts == 1 and not env.gerrit.vote_posts
    assert await env.store.count_consumed_retry_attempts(env.job.id, stage=AttemptStage.REVIEW) == 0


async def test_crash_after_comment_commit_recovers_pending_vote(env, monkeypatch):
    await run_review(env)
    original = VotePublisher.process

    async def crash(*args, **kwargs):
        raise asyncio.CancelledError("crash after comment commit")

    monkeypatch.setattr(VotePublisher, "process", crash)
    with pytest.raises(asyncio.CancelledError):
        await run_publish(env)
    assert (await env.store.publication_for_job(env.job.id)).status == "POSTED"
    assert (await env.store.get(env.job.id)).state == JobState.PUBLISHING
    monkeypatch.setattr(VotePublisher, "process", original)
    await due(env)
    await run_publish(env)
    assert (await audit_vote(env)).status == "APPLIED"
    assert env.gerrit.comment_posts == 1 and len(env.gerrit.vote_posts) == 1


async def test_policy_update_does_not_reset_scope_or_global_generation(env):
    before = (await env.control.list_projects())[0]
    status = await env.control.config_change_status()
    changed = await env.control.set_project_review_policy(
        "team/fw", review_language="en-US", auto_code_review=False, expected_generation=1
    )
    assert changed.review_start_at == before.review_start_at
    assert changed.review_start_mode == before.review_start_mode
    assert await env.control.config_change_status() == status
    with pytest.raises(RuntimeError, match="another session"):
        await env.control.set_project_review_policy(
            "team/fw", review_language="ko-KR", auto_code_review=True, expected_generation=1
        )
    async with env.db.sessions() as session:
        assert (
            await session.scalar(select(Job).where(Job.id == env.job.id))
        ).project_review_policy is None
