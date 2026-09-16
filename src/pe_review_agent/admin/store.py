from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pe_review_agent.config import Settings
from pe_review_agent.domain import JobState
from pe_review_agent.jobs.models import (
    Attempt,
    Job,
    ManagedProject,
    Publication,
    ReviewFinding,
    ReviewResultRow,
    ServiceState,
)

_RUNTIME_CONFIG_KEY = "admin-runtime-config"


@dataclass(frozen=True, slots=True)
class ManagedProjectRecord:
    project: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ControlStore:
    """Durable control-plane settings used by the admin web and long-running services."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def ensure_bootstrap(self, settings: Settings) -> None:
        """Seed the DB once from config.yaml without overwriting later web-managed values."""

        async with self._sessions.begin() as session:
            count = await session.scalar(select(func.count()).select_from(ManagedProject))
            if not count and settings.gerrit.projects:
                await session.execute(
                    pg_insert(ManagedProject)
                    .values(
                        [
                            {"project": project, "enabled": True}
                            for project in settings.gerrit.projects
                        ]
                    )
                    .on_conflict_do_nothing(index_elements=[ManagedProject.project])
                )

            # receiver/worker/reconciler/admin start concurrently after the migration container.
            # ON CONFLICT keeps first boot idempotent even when all four processes seed together.
            await session.execute(
                pg_insert(ServiceState)
                .values(key=_RUNTIME_CONFIG_KEY, json_value=_defaults_from_settings(settings))
                .on_conflict_do_nothing(index_elements=[ServiceState.key])
            )

    async def runtime_config(self, settings: Settings) -> dict[str, Any]:
        async with self._sessions() as session:
            row = await session.get(ServiceState, _RUNTIME_CONFIG_KEY)
            if row is None:
                return _defaults_from_settings(settings)
            return _merge_runtime_defaults(_defaults_from_settings(settings), row.json_value)

    async def patch_runtime_config(
        self,
        settings: Settings,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        async with self._sessions.begin() as session:
            row = await session.get(ServiceState, _RUNTIME_CONFIG_KEY, with_for_update=True)
            if row is None:
                # The normal startup path seeds this row first. Keep this method independently safe
                # for administrative tooling/tests that reach it before ensure_bootstrap().
                await session.execute(
                    pg_insert(ServiceState)
                    .values(
                        key=_RUNTIME_CONFIG_KEY,
                        json_value=_defaults_from_settings(settings),
                    )
                    .on_conflict_do_nothing(index_elements=[ServiceState.key])
                )
                row = await session.get(ServiceState, _RUNTIME_CONFIG_KEY, with_for_update=True)
                if row is None:
                    raise RuntimeError("failed to initialize durable runtime configuration")

            current = _merge_runtime_defaults(
                _defaults_from_settings(settings),
                row.json_value,
            )
            for key, value in updates.items():
                if isinstance(value, dict) and isinstance(current.get(key), dict):
                    current[key] = {**current[key], **value}
                else:
                    current[key] = value
            # Assign a fresh JSON object so SQLAlchemy always detects the mutation.
            row.json_value = current
            row.updated_at = datetime.now(UTC)
            await session.flush()
            return current

    async def service_enabled(self, *, default: bool) -> bool:
        # The bootstrap setting is a hard operational kill switch. A stale DB value must never
        # override service.enabled=false after an operator restarts the deployment.
        if not default:
            return False
        async with self._sessions() as session:
            row = await session.get(ServiceState, _RUNTIME_CONFIG_KEY)
            if row is None:
                return default
            value = row.json_value.get("service_enabled")
            return value if isinstance(value, bool) else default

    async def set_service_enabled(self, settings: Settings, enabled: bool) -> None:
        await self.patch_runtime_config(settings, {"service_enabled": enabled})

    async def effective_settings(self, base: Settings) -> Settings:
        runtime = await self.runtime_config(base)
        managed_projects = await self.list_projects()
        projects = (
            [item.project for item in managed_projects if item.enabled]
            if managed_projects
            else list(base.gerrit.projects)
        )

        gerrit_cfg = runtime.get("gerrit") or {}
        rest_auth = base.gerrit.rest_auth.model_copy(
            update={
                "mode": gerrit_cfg.get("rest_auth_mode", base.gerrit.rest_auth.mode),
                "username": gerrit_cfg.get("rest_username", base.gerrit.rest_auth.username),
            }
        )
        gerrit = base.gerrit.model_copy(
            update={
                "ssh_host": gerrit_cfg.get("ssh_host", base.gerrit.ssh_host),
                "ssh_port": gerrit_cfg.get("ssh_port", base.gerrit.ssh_port),
                "ssh_user": gerrit_cfg.get("ssh_user", base.gerrit.ssh_user),
                "rest_url": gerrit_cfg.get("rest_url", base.gerrit.rest_url),
                "rest_auth": rest_auth,
                "projects": projects,
            }
        )

        llm_cfg = runtime.get("llm") or {}
        llm = base.llm.model_copy(
            update={
                "base_url": llm_cfg.get("base_url", base.llm.base_url),
                "model": llm_cfg.get("model", base.llm.model),
                "temperature": llm_cfg.get("temperature", base.llm.temperature),
                "max_output_tokens": llm_cfg.get(
                    "max_output_tokens", base.llm.max_output_tokens
                ),
            }
        )

        review_cfg = runtime.get("review") or {}
        review = base.review.model_copy(
            update={
                "policy_version": review_cfg.get("policy_version", base.review.policy_version),
                "max_findings": review_cfg.get("max_findings", base.review.max_findings),
                "min_confidence": review_cfg.get("min_confidence", base.review.min_confidence),
            }
        )

        runtime_enabled = runtime.get("service_enabled", True)
        service = base.service.model_copy(
            update={"enabled": base.service.enabled and runtime_enabled is True}
        )
        return base.model_copy(
            update={"gerrit": gerrit, "llm": llm, "review": review, "service": service}
        )

    async def list_projects(self) -> list[ManagedProjectRecord]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(select(ManagedProject).order_by(ManagedProject.project.asc()))
            ).all()
            return [
                ManagedProjectRecord(
                    project=row.project,
                    enabled=row.enabled,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
                for row in rows
            ]

    async def enabled_projects(self, *, fallback: tuple[str, ...] = ()) -> tuple[str, ...]:
        async with self._sessions() as session:
            any_count = await session.scalar(select(func.count()).select_from(ManagedProject))
            if not any_count:
                return tuple(fallback)
            rows = await session.scalars(
                select(ManagedProject.project)
                .where(ManagedProject.enabled.is_(True))
                .order_by(ManagedProject.project.asc())
            )
            return tuple(rows.all())

    async def upsert_project(self, project: str, *, enabled: bool = True) -> ManagedProjectRecord:
        normalized = project.strip()
        if not normalized or len(normalized) > 512:
            raise ValueError("project must be between 1 and 512 characters")
        if any(ord(char) < 32 for char in normalized):
            raise ValueError("project cannot contain control characters")
        async with self._sessions.begin() as session:
            statement = (
                pg_insert(ManagedProject)
                .values(project=normalized, enabled=enabled)
                .on_conflict_do_update(
                    index_elements=[ManagedProject.project],
                    set_={"enabled": enabled, "updated_at": func.now()},
                )
                .returning(ManagedProject)
            )
            row = (await session.execute(statement)).scalar_one()
            return ManagedProjectRecord(
                project=row.project,
                enabled=row.enabled,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )

    async def set_project_enabled(self, project: str, enabled: bool) -> ManagedProjectRecord:
        async with self._sessions.begin() as session:
            row = await session.get(ManagedProject, project, with_for_update=True)
            if row is None:
                raise KeyError(project)
            row.enabled = enabled
            row.updated_at = datetime.now(UTC)
            await session.flush()
            return ManagedProjectRecord(
                project=row.project,
                enabled=row.enabled,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )

    async def project_enabled(self, project: str, *, fallback: tuple[str, ...] = ()) -> bool:
        async with self._sessions() as session:
            any_count = await session.scalar(select(func.count()).select_from(ManagedProject))
            if not any_count:
                return project in fallback
            value = await session.scalar(
                select(ManagedProject.enabled).where(ManagedProject.project == project)
            )
            return bool(value)

    async def dashboard_snapshot(self) -> dict[str, Any]:
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        async with self._sessions() as session:
            state_rows = (
                await session.execute(
                    select(Job.state, func.count()).group_by(Job.state).order_by(Job.state.asc())
                )
            ).all()
            recent_24h = await session.scalar(
                select(func.count()).select_from(Job).where(Job.created_at >= cutoff)
            )
            failed_24h = await session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.created_at >= cutoff, Job.state == JobState.FAILED_PERMANENT.value)
            )
            recent_jobs = (
                await session.scalars(select(Job).order_by(Job.updated_at.desc()).limit(10))
            ).all()
        projects = await self.list_projects()
        return {
            "states": {state: int(count) for state, count in state_rows},
            "recent_24h": int(recent_24h or 0),
            "failed_24h": int(failed_24h or 0),
            "projects_total": len(projects),
            "projects_enabled": sum(1 for project in projects if project.enabled),
            "recent_jobs": [_job_dict(job) for job in recent_jobs],
        }

    async def list_jobs(
        self,
        *,
        limit: int = 100,
        state: str | None = None,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 500)
        statement = select(Job)
        if state:
            statement = statement.where(Job.state == state)
        if project:
            statement = statement.where(Job.project == project)
        statement = statement.order_by(Job.updated_at.desc()).limit(limit)
        async with self._sessions() as session:
            rows = (await session.scalars(statement)).all()
            return [_job_dict(job) for job in rows]

    async def get_job(self, job_id: uuid.UUID) -> dict[str, Any] | None:
        async with self._sessions() as session:
            job = await session.get(Job, job_id)
            return _job_dict(job) if job is not None else None

    async def get_job_audit(self, job_id: uuid.UUID) -> dict[str, Any] | None:
        """Return the durable audit trail for one review job, including exact Gerrit payload."""

        async with self._sessions() as session:
            job = await session.get(Job, job_id)
            if job is None:
                return None

            attempts = (
                await session.scalars(
                    select(Attempt)
                    .where(Attempt.job_id == job_id)
                    .order_by(Attempt.attempt_number.asc())
                )
            ).all()
            result = await session.get(ReviewResultRow, job_id)
            findings = (
                (
                    await session.scalars(
                        select(ReviewFinding)
                        .where(ReviewFinding.job_id == job_id)
                        .order_by(ReviewFinding.ordinal.asc())
                    )
                ).all()
                if result is not None
                else []
            )
            publication = await session.scalar(
                select(Publication).where(Publication.job_id == job_id)
            )

            audit = _job_dict(job)
            audit["attempts"] = [
                {
                    "attempt_number": attempt.attempt_number,
                    "stage": attempt.stage,
                    "worker_id": attempt.worker_id,
                    "started_at": attempt.started_at,
                    "finished_at": attempt.finished_at,
                    "success": attempt.success,
                    "retryable": attempt.retryable,
                    "error_class": attempt.error_class,
                    "error_message": attempt.error_message,
                }
                for attempt in attempts
            ]
            audit["review"] = (
                {
                    "summary": result.summary,
                    "model": result.model,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "metadata": result.review_metadata,
                    "created_at": result.created_at,
                    "findings": [
                        {
                            "ordinal": finding.ordinal,
                            "fingerprint": finding.fingerprint,
                            "semantic_id": finding.semantic_id,
                            "lineage": finding.lineage_state,
                            "severity": finding.severity,
                            "category": finding.category,
                            "title": finding.title,
                            "message": finding.message,
                            "impact": finding.impact,
                            "evidence": finding.evidence,
                            "remediation": finding.remediation,
                            "path": finding.path,
                            "side": finding.side,
                            "start_line": finding.start_line,
                            "start_character": finding.start_character,
                            "end_line": finding.end_line,
                            "end_character": finding.end_character,
                            "confidence": finding.confidence,
                        }
                        for finding in findings
                    ],
                }
                if result is not None
                else None
            )
            audit["publication"] = (
                {
                    "id": publication.id,
                    "fingerprint": publication.publication_fingerprint,
                    "status": publication.status,
                    "finding_fingerprints": publication.finding_fingerprints,
                    "request_payload": publication.request_payload,
                    "gerrit_response": publication.gerrit_response,
                    "last_error": publication.last_error,
                    "created_at": publication.created_at,
                    "posted_at": publication.posted_at,
                    "updated_at": publication.updated_at,
                }
                if publication is not None
                else None
            )
            return audit


def _defaults_from_settings(settings: Settings) -> dict[str, Any]:
    return {
        "service_enabled": settings.service.enabled,
        "gerrit": {
            "ssh_host": settings.gerrit.ssh_host,
            "ssh_port": settings.gerrit.ssh_port,
            "ssh_user": settings.gerrit.ssh_user,
            "rest_url": settings.gerrit.rest_url,
            "rest_auth_mode": settings.gerrit.rest_auth.mode,
            "rest_username": settings.gerrit.rest_auth.username,
        },
        "llm": {
            "base_url": settings.llm.base_url,
            "model": settings.llm.model,
            "temperature": settings.llm.temperature,
            "max_output_tokens": settings.llm.max_output_tokens,
        },
        "review": {
            "policy_version": settings.review.policy_version,
            "max_findings": settings.review.max_findings,
            "min_confidence": settings.review.min_confidence,
        },
    }


def _merge_runtime_defaults(defaults: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(defaults)
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _job_dict(job: Job) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "project": job.project,
        "change_number": job.change_number,
        "patchset_number": job.patchset_number,
        "revision_sha": job.revision_sha,
        "policy_version": job.review_policy_version,
        "state": job.state,
        "retry_state": job.retry_state,
        "attempt_count": job.attempt_count,
        "last_error_class": job.last_error_class,
        "last_error": job.last_error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
