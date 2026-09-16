from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from pe_review_agent.admin import ControlStore
from pe_review_agent.admin.web import create_admin_app
from pe_review_agent.config import Settings
from pe_review_agent.db import Database
from pe_review_agent.domain import GerritPatchsetEvent
from pe_review_agent.jobs import JobStore
from pe_review_agent.retry import PermanentError

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
            },
        }
    )


async def _truncate(settings: Settings) -> None:
    database = Database(settings.database)
    try:
        async with database.session() as session:
            await session.execute(
                text(
                    "TRUNCATE review_managed_projects, review_service_state, review_publications, "
                    "review_findings, review_results, review_attempts, review_jobs "
                    "RESTART IDENTITY CASCADE"
                )
            )
            await session.commit()
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
        runtime = await control.runtime_config(settings)
        assert runtime["gerrit"]["ssh_host"] == "gerrit"
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

        projects = client.get("/projects", auth=("ops", "correct-horse"))
        assert "team/new-fw" in projects.text


def test_admin_live_controls_runtime_config_and_requeue(
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

        saved_review = client.put(
            "/api/runtime-config",
            auth=auth,
            headers=headers,
            json={
                "review": {
                    "policy_version": "firmware-v2",
                    "max_findings": 6,
                    "min_confidence": 0.9,
                },
            },
        )
        assert saved_review.status_code == 200
        assert saved_review.json()["restart_required"] is True

        connections = client.get("/connections", auth=auth)
        assert "gerrit-new" in connections.text
        assert "qwen-new" in connections.text

        requeued = client.post(
            f"/api/jobs/{failed_job_id}/requeue",
            auth=auth,
            headers=headers,
            json={},
        )
        assert requeued.status_code == 200
        assert requeued.json()["state"] == "RECEIVED"
