from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from pe_review_agent.admin import ControlStore, OperationsStore
from pe_review_agent.admin.web import create_admin_app
from pe_review_agent.config import Settings
from pe_review_agent.db import Database
from pe_review_agent.domain import (
    AttemptStage,
    Finding,
    FindingLocation,
    GerritPatchsetEvent,
    JobState,
    ReviewResult,
    Severity,
)
from pe_review_agent.jobs import JobStore, ProjectReviewStartMode
from pe_review_agent.jobs.models import Job, ServiceState
from pe_review_agent.operations import settings_fingerprint
from pe_review_agent.retry import PermanentError
from pe_review_agent.review.checkpoints import CandidateChunkCheckpoint

DSN = os.environ.get("PE_REVIEW_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="PE_REVIEW_TEST_POSTGRES_DSN is not configured")


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
            "llm": {"base_url": "https://llm/v1", "model": "Qwen3.6-27B"},
            "database": {"dsn": DSN},
            "repos": {
                "cache_root": str(tmp_path / "repos"),
                "work_root": str(tmp_path / "work"),
            },
            "admin": {
                "host": "127.0.0.1",
                "port": 8080,
                "auth_mode": "basic",
                "username": "ops",
                "password_env": "PE_REVIEW_TEST_ADMIN_PASSWORD",
                "log_root": str(tmp_path / "logs"),
            },
        }
    )


async def _truncate(settings: Settings) -> None:
    database = Database(settings.database)
    try:
        async with database.session() as session:
            await session.execute(
                text(
                    "TRUNCATE review_service_heartbeats, review_managed_projects, "
                    "review_service_state, review_publications, "
                    "review_findings, review_results, review_attempts, review_jobs "
                    "RESTART IDENTITY CASCADE"
                )
            )
            await session.commit()
    finally:
        await database.close()


async def _effective_review_settings(settings: Settings):  # type: ignore[no-untyped-def]
    database = Database(settings.database)
    try:
        control = ControlStore(database.sessions)
        effective = await control.effective_settings(settings)
        return effective.review
    finally:
        await database.close()


async def _dashboard_snapshot(
    control: ControlStore,
    operations: OperationsStore,
    *,
    service_enabled: bool,
) -> dict[str, object]:
    projects = await control.list_projects()
    return await operations.dashboard_snapshot(
        service_enabled=service_enabled,
        enabled_projects={project.project for project in projects if project.enabled},
        projects_total=len(projects),
        config=await control.config_change_status(),
    )


async def _display_timezone_state(settings: Settings) -> tuple[int, str]:
    database = Database(settings.database)
    try:
        control = ControlStore(database.sessions)
        generation = (await control.config_change_status())["generation"]
        timezone_name = await control.display_timezone(settings.admin.timezone)
        return generation, timezone_name
    finally:
        await database.close()


async def _failed_job(settings: Settings):  # type: ignore[no-untyped-def]
    database = Database(settings.database)
    try:
        store = JobStore(database.sessions)
        job, _ = await store.enqueue(
            GerritPatchsetEvent(
                project="team/fw",
                change_number=777,
                patchset_number=1,
                revision_sha="a" * 40,
                ref="refs/changes/77/777/1",
                branch="main",
            ),
            review_policy_version="firmware-v1",
        )
        claimed = await store.claim_next(worker_id="test-worker", lease_seconds=120)
        assert claimed is not None
        await store.mark_failed_permanent(
            job.id,
            worker_id="test-worker",
            error=PermanentError("intentional test failure"),
        )
        return job.id
    finally:
        await database.close()


async def _published_job(settings: Settings):  # type: ignore[no-untyped-def]
    database = Database(settings.database)
    try:
        store = JobStore(database.sessions)
        job, _ = await store.enqueue(
            GerritPatchsetEvent(
                project="team/fw",
                change_number=778,
                patchset_number=2,
                revision_sha="b" * 40,
                ref="refs/changes/78/778/2",
                branch="main",
            ),
            review_policy_version="firmware-v1",
        )
        claimed = await store.claim_next(worker_id="audit-worker", lease_seconds=120)
        assert claimed is not None
        claimed = await store.transition(job.id, JobState.FETCHING, worker_id="audit-worker")
        fetch_attempt = await store.start_attempt(
            job.id,
            stage=AttemptStage.FETCH,
            worker_id="audit-worker",
        )
        await store.finish_attempt(fetch_attempt, success=True)
        claimed = await store.transition(job.id, JobState.REVIEWING, worker_id="audit-worker")
        review_attempt = await store.start_attempt(
            job.id,
            stage=AttemptStage.REVIEW,
            worker_id="audit-worker",
        )
        await store.save_candidate_chunk_checkpoint(
            job.id,
            worker_id="audit-worker",
            checkpoint_version="candidate-v1",
            checkpoint=CandidateChunkCheckpoint(
                chunk_key="c" * 64,
                parent_chunk_key=None,
                status="DONE",
                paths=("fw/train.c",),
                change_summary="- training timeout handling changed",
                findings=(),
                input_tokens=100,
                output_tokens=20,
                llm_calls=1,
                tool_calls=1,
            ),
        )
        await store.append_attempt_tool_event(
            review_attempt,
            {
                "event": "tool_call",
                "phase": "candidate:1",
                "round": 1,
                "tool": "read_file",
                "arguments": {"path": "fw/train.c", "start_line": 40, "end_line": 45},
                "status": "ok",
                "result_bytes": 96,
                "result_preview": "40: int rc = poll_done();\n41: advance();",
            },
        )
        claimed = await store.transition(job.id, JobState.VALIDATING, worker_id="audit-worker")
        review = ReviewResult(
            summary="Audit summary: one actionable issue was found.",
            model="Qwen3.6-27B",
            input_tokens=1234,
            output_tokens=321,
            review_metadata={
                "lineage_complete": True,
                "review_budget": {
                    "candidate_chunks_reviewed": 2,
                    "candidate_chunks_total": 2,
                    "reviewable_files_fully_reviewed": 1,
                    "reviewable_files_total": 1,
                    "verification_complete": True,
                    "complete": True,
                    "stop_reasons": [],
                    "llm_calls": 3,
                    "tool_calls": 1,
                    "input_tokens": 1234,
                    "output_tokens": 321,
                    "uncovered_files": [],
                    "uncovered_files_truncated": False,
                    "limits": {
                        "max_candidate_chunks": 12,
                        "max_llm_calls_per_job": 30,
                        "max_tool_calls_per_job": 50,
                        "max_input_tokens_per_job": 300000,
                    },
                },
            },
            findings=[
                Finding(
                    severity=Severity.P1,
                    category="timeout",
                    title="Timeout is ignored",
                    message="The timeout return value is discarded.",
                    impact="Training can advance with stale state.",
                    evidence="poll_done() returns -ETIMEDOUT.",
                    remediation="Propagate the timeout.",
                    location=FindingLocation(path="fw/train.c", start_line=42),
                    confidence=0.97,
                )
            ],
        )
        await store.save_review_result_and_mark_ready(job.id, review, worker_id="audit-worker")
        await store.finish_attempt(review_attempt, success=True)

        publish_claim = await store.claim_next(worker_id="audit-publisher", lease_seconds=120)
        assert publish_claim is not None
        await store.transition(job.id, JobState.PUBLISHING, worker_id="audit-publisher")
        publication = await store.begin_publication(
            job.id,
            worker_id="audit-publisher",
            request_payload={
                "message": review.summary,
                "tag": "autogenerated:pe-ai-review~firmware-v1",
                "comments": {
                    "fw/train.c": [
                        {
                            "line": 42,
                            "message": "[P1] Timeout is ignored: exact inline audit message",
                        }
                    ]
                },
            },
            finding_fingerprints=[review.findings[0].fingerprint or ""],
        )
        await store.complete_publication_and_mark_done(
            publication.id,
            job_id=job.id,
            worker_id="audit-publisher",
            gerrit_response={"labels": {}, "ready": True},
        )
        return job.id
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_concurrent_control_plane_bootstrap_is_idempotent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _truncate(settings)
    database = Database(settings.database)
    try:
        control = ControlStore(database.sessions)
        await asyncio.gather(*(control.ensure_bootstrap(settings) for _ in range(8)))
        projects = await control.list_projects()
        assert [(item.project, item.enabled) for item in projects] == [("team/fw", True)]
        assert projects[0].review_start_mode is ProjectReviewStartMode.FROM_NOW
        assert projects[0].review_start_at is not None
        runtime = await control.runtime_config(settings)
        assert runtime["gerrit"]["ssh_host"] == "gerrit"
        assert await control.runtime_override_sections() == set()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_connection_overrides_can_be_reset_to_current_config_yaml(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _truncate(settings)
    database = Database(settings.database)
    try:
        control = ControlStore(database.sessions)
        await control.ensure_bootstrap(settings)
        await control.patch_runtime_config(
            settings,
            {
                "gerrit": {
                    "rest_auth_mode": "basic",
                    "rest_username": "bot",
                    "rest_url": "https://override-gerrit",
                },
                "llm": {"base_url": "https://override-llm/v1"},
            },
        )
        assert await control.runtime_override_sections() == {"gerrit", "llm"}

        effective = await control.effective_settings(settings)
        assert effective.gerrit.rest_auth.mode == "basic"
        assert effective.gerrit.rest_auth.password_env == "PE_REVIEW_GERRIT_HTTP_PASSWORD"
        assert effective.gerrit.rest_url == "https://override-gerrit"

        await control.reset_runtime_sections(settings, "gerrit", "llm")
        assert await control.runtime_override_sections() == set()
        reset = await control.effective_settings(settings)
        assert reset.gerrit.rest_auth.mode == settings.gerrit.rest_auth.mode
        assert reset.gerrit.rest_url == settings.gerrit.rest_url
        assert reset.llm.base_url == settings.llm.base_url
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_full_admin_form_persists_only_fields_different_from_config_yaml(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    await _truncate(settings)
    database = Database(settings.database)
    try:
        control = ControlStore(database.sessions)
        await control.ensure_bootstrap(settings)
        await control.replace_runtime_sections(
            settings,
            {
                "gerrit": {
                    "ssh_host": settings.gerrit.ssh_host,
                    "ssh_port": settings.gerrit.ssh_port,
                    "ssh_user": settings.gerrit.ssh_user,
                    "rest_url": "https://admin-override-gerrit",
                    "rest_auth_mode": settings.gerrit.rest_auth.mode,
                    "rest_username": settings.gerrit.rest_auth.username,
                },
                "llm": {
                    "base_url": settings.llm.base_url,
                    "model": settings.llm.model,
                    "temperature": settings.llm.temperature,
                    "max_output_tokens": settings.llm.max_output_tokens,
                },
            },
        )

        changed_base = settings.model_copy(
            update={
                "gerrit": settings.gerrit.model_copy(update={"ssh_host": "gerrit-from-new-yaml"}),
                "llm": settings.llm.model_copy(update={"model": "Qwen3.6-New-Yaml"}),
            }
        )
        effective = await control.effective_settings(changed_base)
        assert effective.gerrit.rest_url == "https://admin-override-gerrit"
        assert effective.gerrit.ssh_host == "gerrit-from-new-yaml"
        assert effective.llm.model == "Qwen3.6-New-Yaml"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_bootstrap_prunes_only_semantics_preserving_legacy_full_snapshot(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    await _truncate(settings)
    database = Database(settings.database)
    try:
        control = ControlStore(database.sessions)
        await control.ensure_bootstrap(settings)
        full_gerrit = {
            "ssh_host": settings.gerrit.ssh_host,
            "ssh_port": settings.gerrit.ssh_port,
            "ssh_user": settings.gerrit.ssh_user,
            "rest_url": settings.gerrit.rest_url,
            "rest_auth_mode": settings.gerrit.rest_auth.mode,
            "rest_username": settings.gerrit.rest_auth.username,
        }
        full_llm = {
            "base_url": settings.llm.base_url,
            "model": settings.llm.model,
            "temperature": settings.llm.temperature,
            "max_output_tokens": settings.llm.max_output_tokens,
        }
        legacy_full_review = {
            "policy_version": settings.review.policy_version,
            "output_language": settings.review.output_language,
            "max_findings": settings.review.max_findings,
            "min_confidence": settings.review.min_confidence,
        }
        async with database.sessions.begin() as session:
            row = await session.get(ServiceState, "admin-runtime-config", with_for_update=True)
            assert row is not None
            row.json_value = {
                "service_enabled": True,
                "gerrit": full_gerrit,
                "llm": full_llm,
                "review": legacy_full_review,
            }

        await control.ensure_bootstrap(settings)
        assert await control.runtime_override_sections() == set()
        assert await control.legacy_runtime_snapshot_sections(settings) == set()
        effective_after_upgrade = await control.effective_settings(settings)
        assert effective_after_upgrade.review.max_candidate_chunks == 12
        assert effective_after_upgrade.review.verifier_budget_fraction == pytest.approx(1 / 3)
        assert effective_after_upgrade.review.max_input_tokens_per_job is None

        async with database.sessions.begin() as session:
            row = await session.get(ServiceState, "admin-runtime-config", with_for_update=True)
            assert row is not None
            row.json_value = {
                "service_enabled": True,
                "gerrit": full_gerrit,
                "llm": full_llm,
            }
        changed_base = settings.model_copy(
            update={"gerrit": settings.gerrit.model_copy(update={"ssh_host": "new-yaml-host"})}
        )
        await control.ensure_bootstrap(changed_base)
        assert "gerrit" in await control.legacy_runtime_snapshot_sections(changed_base)
        assert "gerrit" in await control.runtime_override_sections()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_concurrent_runtime_patches_do_not_clobber_other_controls(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _truncate(settings)
    database = Database(settings.database)
    try:
        control = ControlStore(database.sessions)
        await control.ensure_bootstrap(settings)
        await asyncio.gather(
            control.set_service_enabled(settings, False),
            control.patch_runtime_config(
                settings,
                {"llm": {"model": "Qwen3.6-Admin-Test"}},
            ),
        )
        runtime = await control.runtime_config(settings)
        assert runtime["service_enabled"] is False
        assert runtime["llm"]["model"] == "Qwen3.6-Admin-Test"

        await control.set_project_enabled("team/fw", False)
        effective = await control.effective_settings(settings)
        assert effective.gerrit.projects == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_project_review_scope_can_switch_and_reenable_resets_from_now_cutoff(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    await _truncate(settings)
    database = Database(settings.database)
    try:
        control = ControlStore(database.sessions)
        await control.ensure_bootstrap(settings)
        initial = (await control.list_projects())[0]
        assert initial.review_start_mode is ProjectReviewStartMode.FROM_NOW
        assert initial.review_start_at is not None

        backfill = await control.set_project_review_start_mode(
            "team/fw", ProjectReviewStartMode.INCLUDE_OPEN
        )
        assert backfill.review_start_mode is ProjectReviewStartMode.INCLUDE_OPEN
        assert backfill.review_start_at is None

        jobs = JobStore(database.sessions)
        queued, _ = await jobs.enqueue(
            GerritPatchsetEvent(
                project="team/fw",
                change_number=990,
                patchset_number=1,
                revision_sha="c" * 40,
                ref="refs/changes/90/990/1",
                branch="main",
            ),
            review_policy_version="firmware-v1",
        )

        async with database.session() as session:
            before_from_now = await session.scalar(text("SELECT now()"))
        assert before_from_now is not None
        from_now = await control.set_project_review_start_mode(
            "team/fw", ProjectReviewStartMode.FROM_NOW
        )
        assert from_now.review_start_at is not None
        assert from_now.review_start_at >= before_from_now
        queued_after = await jobs.get(queued.id)
        assert queued_after is not None
        assert queued_after.state is JobState.SKIPPED_SCOPE

        await control.set_project_enabled("team/fw", False)
        async with database.session() as session:
            before_reenable = await session.scalar(text("SELECT now()"))
        assert before_reenable is not None
        reenabled = await control.set_project_enabled("team/fw", True)
        assert reenabled.review_start_at is not None
        assert reenabled.review_start_at >= before_reenable
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_bootstrap_disabled_is_a_hard_kill_switch(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _truncate(settings)
    database = Database(settings.database)
    try:
        control = ControlStore(database.sessions)
        await control.ensure_bootstrap(settings)
        await control.set_service_enabled(settings, True)
        assert await control.service_enabled(default=True) is True

        # A restarted deployment with service.enabled=false must win over a stale DB ON value.
        hard_disabled = settings.model_copy(
            update={
                "service": settings.service.model_copy(update={"enabled": False}),
            }
        )
        assert await control.service_enabled(default=hard_disabled.service.enabled) is False
        effective = await control.effective_settings(hard_disabled)
        assert effective.service.enabled is False
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_admin_pause_restart_then_resume_keeps_bootstrap_switch_live(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _truncate(settings)
    database = Database(settings.database)
    try:
        control = ControlStore(database.sessions)
        await control.ensure_bootstrap(settings)

        # Simulate pausing from Admin, then restarting receiver/worker/reconciler.
        await control.set_service_enabled(settings, False)
        restarted_effective = await control.effective_settings(settings)

        # The DB pause still wins while paused, but it must not rewrite the immutable
        # bootstrap hard-switch value captured by the restarted services.
        assert restarted_effective.service.enabled is True
        assert await control.service_enabled(default=restarted_effective.service.enabled) is False

        # Admin resume must take effect live without requiring another process restart.
        await control.set_service_enabled(settings, True)
        assert await control.service_enabled(default=restarted_effective.service.enabled) is True
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_config_generation_and_heartbeat_application_are_durable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _truncate(settings)
    database = Database(settings.database)
    try:
        control = ControlStore(database.sessions)
        operations = OperationsStore(database.sessions)
        await control.ensure_bootstrap(settings)
        original, generation = await control.effective_settings_with_generation(settings)
        assert generation == 0

        # Live pause/resume does not require process restart and therefore does not advance the
        # startup configuration generation.
        await control.set_service_enabled(settings, False)
        assert await control.current_config_generation() == 0

        changed_model = original.llm.model + "-ops"
        await control.replace_runtime_sections(
            settings,
            {
                "llm": {
                    "base_url": original.llm.base_url,
                    "model": changed_model,
                    "temperature": original.llm.temperature,
                    "max_output_tokens": original.llm.max_output_tokens,
                }
            },
        )
        changed, generation = await control.effective_settings_with_generation(settings)
        assert generation == 1
        assert (await control.config_change_status())["changed_sections"] == ["llm"]

        now = datetime.now(UTC)
        old_fingerprint = settings_fingerprint(original)
        new_fingerprint = settings_fingerprint(changed)
        for component in ("admin", "worker", "reconciler"):
            await operations.record_service_heartbeat(
                component=component,
                instance_id=f"{component}-1",
                version="0.1.0",
                revision="a" * 40,
                config_fingerprint=new_fingerprint,
                applied_config_generation=1,
                started_at=now,
            )
        await operations.record_service_heartbeat(
            component="receiver",
            instance_id="receiver-1",
            version="0.1.0",
            revision="a" * 40,
            config_fingerprint=old_fingerprint,
            applied_config_generation=0,
            started_at=now,
        )
        await operations.record_service_heartbeat(
            component="receiver",
            instance_id="receiver-2",
            version="0.1.0",
            revision="a" * 40,
            config_fingerprint=new_fingerprint,
            applied_config_generation=1,
            started_at=now,
        )

        status = await operations.operational_status(await control.config_change_status())
        assert status["config"]["pending_components"] == ["receiver"]
        assert status["config"]["drift_components"] == ["receiver"]
        assert status["config"]["all_applied"] is False

        await operations.record_service_heartbeat(
            component="receiver",
            instance_id="receiver-1",
            version="0.1.0",
            revision="a" * 40,
            config_fingerprint=new_fingerprint,
            applied_config_generation=1,
            started_at=now,
        )
        status = await operations.operational_status(await control.config_change_status())
        assert status["config"]["pending_components"] == []
        assert status["config"]["drift_components"] == []
        assert status["config"]["all_applied"] is True
        assert status["mixed_revisions"] is False

        # Submitting the identical form is not a configuration change.
        await control.replace_runtime_sections(
            settings,
            {
                "llm": {
                    "base_url": changed.llm.base_url,
                    "model": changed.llm.model,
                    "temperature": changed.llm.temperature,
                    "max_output_tokens": changed.llm.max_output_tokens,
                }
            },
        )
        assert await control.current_config_generation() == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_dashboard_groups_failed_overdue_and_stuck_jobs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _truncate(settings)
    database = Database(settings.database)
    try:
        control = ControlStore(database.sessions)
        operations = OperationsStore(database.sessions)
        await control.ensure_bootstrap(settings)
        effective, generation = await control.effective_settings_with_generation(settings)
        now = datetime.now(UTC)
        fingerprint = settings_fingerprint(effective)
        for component in ("receiver", "worker", "reconciler", "admin"):
            await operations.record_service_heartbeat(
                component=component,
                instance_id=f"{component}-attention",
                version="0.1.0",
                revision="b" * 40,
                config_fingerprint=fingerprint,
                applied_config_generation=generation,
                started_at=now,
            )

        async with database.sessions.begin() as session:
            common = {
                "project": "team/fw",
                "patchset_number": 1,
                "review_policy_version": "firmware-v1",
                "event_payload": {},
                "attempt_count": 1,
                "retry_epoch_start_attempt": 0,
                "created_at": now - timedelta(hours=1),
            }
            session.add_all(
                [
                    Job(
                        **common,
                        change_number=1201,
                        revision_sha="1" * 40,
                        state=JobState.FAILED_PERMANENT.value,
                        next_attempt_at=now,
                        last_error_class="PermanentError",
                        last_error="review failed for attention test",
                        updated_at=now - timedelta(minutes=20),
                    ),
                    Job(
                        **common,
                        change_number=1202,
                        revision_sha="2" * 40,
                        state=JobState.RETRY_WAIT.value,
                        retry_state=JobState.REVIEWING.value,
                        next_attempt_at=now - timedelta(minutes=5),
                        updated_at=now - timedelta(minutes=5),
                    ),
                    Job(
                        **common,
                        change_number=1203,
                        revision_sha="3" * 40,
                        state=JobState.REVIEWING.value,
                        next_attempt_at=now,
                        lease_owner="dead-worker",
                        lease_expires_at=now - timedelta(minutes=3),
                        claimed_at=now - timedelta(minutes=20),
                        updated_at=now - timedelta(minutes=3),
                    ),
                    Job(
                        **common,
                        change_number=1204,
                        revision_sha="4" * 40,
                        state=JobState.READY_TO_PUBLISH.value,
                        next_attempt_at=now,
                        updated_at=now - timedelta(minutes=10),
                    ),
                    Job(
                        **common,
                        change_number=1205,
                        revision_sha="5" * 40,
                        state=JobState.PUBLISHING.value,
                        next_attempt_at=now,
                        lease_owner="silent-worker",
                        lease_expires_at=now + timedelta(minutes=5),
                        claimed_at=now - timedelta(minutes=10),
                        updated_at=now - timedelta(minutes=6),
                    ),
                ]
            )

        snapshot = await _dashboard_snapshot(control, operations, service_enabled=True)
        kinds = {item["kind"] for item in snapshot["attention"]}
        assert {
            "failed-job",
            "overdue-retry",
            "expired-lease",
            "publish-queue",
            "stale-active",
        } <= kinds
        assert not {item["kind"] for item in snapshot["attention"]} & {
            "component",
            "config",
        }

        paused = await _dashboard_snapshot(control, operations, service_enabled=False)
        paused_kinds = {item["kind"] for item in paused["attention"]}
        assert "failed-job" in paused_kinds
        assert not paused_kinds & {
            "overdue-retry",
            "expired-lease",
            "publish-queue",
            "stale-active",
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_dashboard_attention_count_is_exact_when_rendering_is_capped(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _truncate(settings)
    database = Database(settings.database)
    try:
        control = ControlStore(database.sessions)
        operations = OperationsStore(database.sessions)
        await control.ensure_bootstrap(settings)
        effective, generation = await control.effective_settings_with_generation(settings)
        now = datetime.now(UTC)
        fingerprint = settings_fingerprint(effective)
        for component in ("receiver", "worker", "reconciler", "admin"):
            await operations.record_service_heartbeat(
                component=component,
                instance_id=f"{component}-bulk",
                version="0.1.0",
                revision="d" * 40,
                config_fingerprint=fingerprint,
                applied_config_generation=generation,
                started_at=now,
            )
        async with database.sessions.begin() as session:
            session.add_all(
                [
                    Job(
                        project="team/fw",
                        change_number=20_000 + index,
                        patchset_number=1,
                        revision_sha=f"{index:040x}",
                        review_policy_version="firmware-v1",
                        state=JobState.FAILED_PERMANENT.value,
                        event_payload={},
                        attempt_count=1,
                        retry_epoch_start_attempt=0,
                        next_attempt_at=now,
                        last_error_class="PermanentError",
                        last_error="bulk attention test",
                        created_at=now - timedelta(hours=1),
                        updated_at=now - timedelta(minutes=index + 1),
                    )
                    for index in range(205)
                ]
            )

        snapshot = await _dashboard_snapshot(control, operations, service_enabled=True)
        rendered_jobs = [item for item in snapshot["attention"] if item.get("job") is not None]
        assert snapshot["attention_count"] == 205
        assert snapshot["attention_truncated"] is True
        assert len(rendered_jobs) == 200
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_service_heartbeat_reports_stale_stop_and_prunes_history(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _truncate(settings)
    database = Database(settings.database)
    try:
        control = ControlStore(database.sessions)
        operations = OperationsStore(database.sessions)
        await control.ensure_bootstrap(settings)
        effective, generation = await control.effective_settings_with_generation(settings)
        now = datetime.now(UTC)
        await operations.record_service_heartbeat(
            component="receiver",
            instance_id="receiver-lifecycle",
            version="0.1.0",
            revision="c" * 40,
            config_fingerprint=settings_fingerprint(effective),
            applied_config_generation=generation,
            started_at=now - timedelta(hours=1),
        )
        async with database.sessions.begin() as session:
            await session.execute(
                text(
                    "UPDATE review_service_heartbeats "
                    "SET last_seen_at = :last_seen "
                    "WHERE component = 'receiver' AND instance_id = 'receiver-lifecycle'"
                ),
                {"last_seen": now - timedelta(minutes=2)},
            )

        status = await operations.operational_status(await control.config_change_status())
        receiver = next(
            item for item in status["components"] if item["component"] == "receiver"
        )
        assert receiver["status"] == "stale"

        await operations.stop_service_heartbeat("receiver", "receiver-lifecycle")
        status = await operations.operational_status(await control.config_change_status())
        receiver = next(
            item for item in status["components"] if item["component"] == "receiver"
        )
        assert receiver["status"] == "stopped"

        async with database.sessions.begin() as session:
            await session.execute(
                text(
                    "UPDATE review_service_heartbeats "
                    "SET last_seen_at = :last_seen "
                    "WHERE component = 'receiver' AND instance_id = 'receiver-lifecycle'"
                ),
                {"last_seen": now - timedelta(days=31)},
            )
        assert await operations.prune_service_heartbeats(retention_days=30) == 1
        assert await operations.list_service_heartbeats() == []
    finally:
        await database.close()


def test_admin_requires_auth_and_mutations_are_csrf_protected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PE_REVIEW_TEST_ADMIN_PASSWORD", "correct-horse")
    settings = _settings(tmp_path)
    asyncio.run(_truncate(settings))

    with TestClient(create_admin_app(settings)) as client:
        assert client.get("/").status_code == 401
        response = client.get("/", auth=("ops", "correct-horse"))
        assert response.status_code == 200
        assert "Gerrit AI Reviewer" in response.text
        assert "Process liveness" in response.text
        assert "starting" in response.text

        no_csrf = client.post(
            "/api/projects",
            auth=("ops", "correct-horse"),
            json={"project": "team/new-fw", "enabled": True},
        )
        assert no_csrf.status_code == 403

        csrf = client.cookies.get("pe_review_csrf")
        assert csrf
        headers = {"X-CSRF-Token": csrf}
        added = client.post(
            "/api/projects",
            auth=("ops", "correct-horse"),
            headers=headers,
            json={"project": "team/new-fw", "enabled": True},
        )
        assert added.status_code == 200
        assert added.json()["project"] == "team/new-fw"
        assert added.json()["review_start_mode"] == "FROM_NOW"
        assert added.json()["review_start_at"] is not None

        backfill = client.post(
            "/api/projects/review-start",
            auth=("ops", "correct-horse"),
            headers=headers,
            json={"project": "team/new-fw", "review_start_mode": "INCLUDE_OPEN"},
        )
        assert backfill.status_code == 200
        assert backfill.json()["review_start_mode"] == "INCLUDE_OPEN"
        assert backfill.json()["review_start_at"] is None

        projects = client.get("/projects", auth=("ops", "correct-horse"))
        assert "team/new-fw" in projects.text
        assert "Backfill enabled" in projects.text


def test_admin_live_controls_runtime_config_and_requeue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PE_REVIEW_TEST_ADMIN_PASSWORD", "correct-horse")
    settings = _settings(tmp_path)
    asyncio.run(_truncate(settings))
    failed_job_id = asyncio.run(_failed_job(settings))

    with TestClient(create_admin_app(settings)) as client:
        auth = ("ops", "correct-horse")
        dashboard = client.get("/", auth=auth)
        assert dashboard.status_code == 200
        assert "Needs attention" in dashboard.text
        assert "failed permanently" in dashboard.text

        page = client.get("/settings", auth=auth)
        csrf = client.cookies.get("pe_review_csrf")
        assert page.status_code == 200 and csrf
        headers = {"X-CSRF-Token": csrf}

        paused = client.post(
            "/api/service",
            auth=auth,
            headers=headers,
            json={"enabled": False},
        )
        assert paused.status_code == 200
        assert paused.json()["enabled"] is False

        saved_connections = client.put(
            "/api/runtime-config",
            auth=auth,
            headers=headers,
            json={
                "gerrit": {
                    "ssh_host": "gerrit-new",
                    "ssh_port": 29418,
                    "ssh_user": "bot",
                    "rest_url": "https://gerrit-new",
                    "rest_auth_mode": "none",
                    "rest_username": "",
                },
                "llm": {
                    "base_url": "https://qwen-new/v1",
                    "model": "Qwen3.6-27B",
                    "temperature": 0.2,
                    "max_output_tokens": 4096,
                },
            },
        )
        assert saved_connections.status_code == 200
        assert saved_connections.json()["restart_required"] is True
        assert saved_connections.json()["config_generation"] == 1

        saved_review = client.put(
            "/api/runtime-config",
            auth=auth,
            headers=headers,
            json={
                "review": {
                    "policy_version": "firmware-v2",
                    "output_language": "en-US",
                    "max_findings": 6,
                    "min_confidence": 0.9,
                    "max_candidate_chunks": 9,
                    "max_llm_calls_per_job": 24,
                    "max_tool_calls_per_job": 40,
                    "verifier_budget_fraction": 0.4,
                },
            },
        )
        assert saved_review.status_code == 200
        assert saved_review.json()["restart_required"] is True
        assert saved_review.json()["config_generation"] == 2

        settings_page = client.get("/settings", auth=auth)
        assert 'value="en-US" selected' in settings_page.text
        assert 'name="review.max_candidate_chunks" value="9"' in settings_page.text
        assert 'name="review.max_llm_calls_per_job" value="24"' in settings_page.text
        assert 'name="review.max_tool_calls_per_job" value="40"' in settings_page.text
        assert 'name="review.verifier_budget_fraction" value="0.4"' in settings_page.text
        assert 'name="review.max_input_tokens_per_job"' not in settings_page.text
        assert "Restart required:" in settings_page.text
        assert 'name="gerrit.ssh_host"' not in settings_page.text
        assert "Saved DB review-policy override is active" in settings_page.text
        assert "snapshot from an earlier release" not in settings_page.text

        effective = asyncio.run(_effective_review_settings(settings))
        assert effective.max_candidate_chunks == 9
        assert effective.max_llm_calls_per_job == 24
        assert effective.max_tool_calls_per_job == 40
        assert effective.verifier_budget_fraction == 0.4

        reset_review = client.post(
            "/api/runtime-config/reset-review",
            auth=auth,
            headers=headers,
            json={},
        )
        assert reset_review.status_code == 200
        assert reset_review.json()["restart_required"] is True
        assert reset_review.json()["config_generation"] == 3
        reset_settings = client.get("/settings", auth=auth)
        assert 'value="ko-KR" selected' in reset_settings.text
        assert "Review policy source:</strong> config.yaml" in reset_settings.text

        connections = client.get("/connections", auth=auth)
        assert "gerrit-new" in connections.text
        assert "qwen-new" in connections.text
        assert "Saved DB connection overrides are active" in connections.text
        assert "Configuration generation 3 is not fully applied" in connections.text

        reset_connections = client.post(
            "/api/runtime-config/reset-connections",
            auth=auth,
            headers=headers,
            json={},
        )
        assert reset_connections.status_code == 200
        assert reset_connections.json()["restart_required"] is True
        assert reset_connections.json()["config_generation"] == 4
        reset_page = client.get("/connections", auth=auth)
        assert "config.yaml (no saved Admin connection overrides)" in reset_page.text

        failed_audit = client.get(f"/jobs/{failed_job_id}", auth=auth)
        assert failed_audit.status_code == 200
        assert "Current failure" in failed_audit.text
        assert "Recorded " in failed_audit.text
        assert " KST" in failed_audit.text
        assert "+00:00" not in failed_audit.text

        requeued = client.post(
            f"/api/jobs/{failed_job_id}/requeue",
            auth=auth,
            headers=headers,
            json={},
        )
        assert requeued.status_code == 200
        assert requeued.json()["state"] == "RECEIVED"


def test_admin_display_timezone_changes_live_without_config_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PE_REVIEW_TEST_ADMIN_PASSWORD", "correct-horse")
    settings = _settings(tmp_path)
    asyncio.run(_truncate(settings))
    failed_job_id = asyncio.run(_failed_job(settings))

    with TestClient(create_admin_app(settings)) as client:
        auth = ("ops", "correct-horse")
        page = client.get("/settings", auth=auth)
        csrf = client.cookies.get("pe_review_csrf")
        assert page.status_code == 200 and csrf
        assert 'name="timezone"' in page.text
        assert 'value="Asia/Seoul"' in page.text

        changed = client.post(
            "/api/display-timezone",
            auth=auth,
            headers={"X-CSRF-Token": csrf},
            json={"timezone": "America/New_York"},
        )
        assert changed.status_code == 200
        assert changed.json()["timezone"] == "America/New_York"

        changed_page = client.get(f"/jobs/{failed_job_id}", auth=auth)
        assert changed_page.status_code == 200
        assert "America/New_York" in changed_page.text
        assert " KST" not in changed_page.text

        invalid = client.post(
            "/api/display-timezone",
            auth=auth,
            headers={"X-CSRF-Token": csrf},
            json={"timezone": "Mars/Olympus"},
        )
        assert invalid.status_code == 422
        invalid_path = client.post(
            "/api/display-timezone",
            auth=auth,
            headers={"X-CSRF-Token": csrf},
            json={"timezone": "/etc/passwd"},
        )
        assert invalid_path.status_code == 422

    generation, timezone_name = asyncio.run(_display_timezone_state(settings))
    assert generation == 0
    assert timezone_name == "America/New_York"


def test_job_audit_shows_exact_review_findings_attempts_and_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PE_REVIEW_TEST_ADMIN_PASSWORD", "correct-horse")
    settings = _settings(tmp_path)
    asyncio.run(_truncate(settings))
    job_id = asyncio.run(_published_job(settings))

    with TestClient(create_admin_app(settings)) as client:
        response = client.get(f"/jobs/{job_id}", auth=("ops", "correct-horse"))

    assert response.status_code == 200
    assert "Audit summary: one actionable issue was found." in response.text
    assert "Timeout is ignored" in response.text
    assert "poll_done() returns -ETIMEDOUT." in response.text
    assert "exact inline audit message" in response.text
    assert "Gerrit publication audit" in response.text
    assert "POSTED" in response.text
    assert "Attempt timeline" in response.text
    assert "Repository tool trace" in response.text
    assert "read_file" in response.text
    assert "fw/train.c" in response.text
    assert "Review budget / coverage" in response.text
    assert "3 / 30" in response.text
    assert "1 / 50" in response.text
    assert "Legacy candidate chunk checkpoints" in response.text
    assert "cccccccccccc" in response.text
    assert " KST" in response.text
    assert "+00:00" not in response.text


def test_logs_page_and_api_expose_full_structured_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PE_REVIEW_TEST_ADMIN_PASSWORD", "correct-horse")
    settings = _settings(tmp_path)
    asyncio.run(_truncate(settings))
    settings.admin.log_root.mkdir(parents=True)
    (settings.admin.log_root / "worker.jsonl").write_text(
        "\n".join(
            [
                '{"ts":"2026-09-17T00:00:00+00:00","level":"INFO",'
                '"component":"worker","logger":"review","message":"normal"}',
                '{"ts":"2026-09-17T00:00:01+00:00","level":"ERROR",'
                '"component":"worker","logger":"review","message":"boom",'
                '"job_id":"abc","exception":"Traceback\\nValueError: full failure"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with TestClient(create_admin_app(settings)) as client:
        page = client.get(
            "/logs?component=worker&level=ERROR&q=full+failure",
            auth=("ops", "correct-horse"),
        )
        api = client.get(
            "/api/logs?component=worker&level=ERROR&q=full+failure",
            auth=("ops", "correct-horse"),
        )

    assert page.status_code == 200
    assert "ValueError: full failure" in page.text
    assert "2026-09-17 09:00:01 KST" in page.text
    assert api.status_code == 200
    assert len(api.json()["entries"]) == 1
    assert api.json()["entries"][0]["exception"].endswith("ValueError: full failure")
