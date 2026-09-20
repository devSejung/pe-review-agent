from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pe_review_agent.domain import JobState
from pe_review_agent.jobs.models import Job, Publication, ServiceHeartbeatRow

SERVICE_COMPONENTS = ("receiver", "worker", "reconciler", "admin")
CONFIG_APPLY_COMPONENTS = ("receiver", "worker", "reconciler")

_HEARTBEAT_STALE_AFTER = timedelta(seconds=60)
_RETRY_OVERDUE_GRACE = timedelta(minutes=2)
_LEASE_EXPIRED_GRACE = timedelta(minutes=1)
_UNCLAIMED_ACTIVE_GRACE = timedelta(minutes=5)
_RECEIVED_QUEUE_GRACE = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class ServiceHeartbeatRecord:
    component: str
    instance_id: str
    version: str
    revision: str
    config_fingerprint: str
    applied_config_generation: int
    details: dict[str, Any]
    started_at: datetime
    last_seen_at: datetime
    stopped_at: datetime | None


class OperationsStore:
    """Durable process liveness and low-touch operational diagnostics."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record_service_heartbeat(
        self,
        *,
        component: str,
        instance_id: str,
        version: str,
        revision: str,
        config_fingerprint: str,
        applied_config_generation: int,
        started_at: datetime,
        details: dict[str, Any] | None = None,
    ) -> None:
        if component not in SERVICE_COMPONENTS:
            raise ValueError(f"unsupported service component: {component}")
        now = datetime.now(UTC)
        payload = {
            "component": component,
            "instance_id": instance_id,
            "version": version,
            "revision": revision,
            "config_fingerprint": config_fingerprint,
            "applied_config_generation": applied_config_generation,
            "details": dict(details or {}),
            "started_at": started_at,
            "last_seen_at": now,
            "stopped_at": None,
        }
        async with self._sessions.begin() as session:
            await session.execute(
                pg_insert(ServiceHeartbeatRow)
                .values(**payload)
                .on_conflict_do_update(
                    index_elements=[
                        ServiceHeartbeatRow.component,
                        ServiceHeartbeatRow.instance_id,
                    ],
                    set_={
                        "version": version,
                        "revision": revision,
                        "config_fingerprint": config_fingerprint,
                        "applied_config_generation": applied_config_generation,
                        "details": dict(details or {}),
                        "last_seen_at": now,
                        "stopped_at": None,
                    },
                )
            )

    async def stop_service_heartbeat(self, component: str, instance_id: str) -> None:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            await session.execute(
                update(ServiceHeartbeatRow)
                .where(
                    ServiceHeartbeatRow.component == component,
                    ServiceHeartbeatRow.instance_id == instance_id,
                )
                .values(last_seen_at=now, stopped_at=now)
            )

    async def list_service_heartbeats(self) -> list[ServiceHeartbeatRecord]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(ServiceHeartbeatRow).order_by(
                        ServiceHeartbeatRow.component.asc(),
                        ServiceHeartbeatRow.last_seen_at.desc(),
                    )
                )
            ).all()
        return [_service_heartbeat_record(row) for row in rows]

    async def prune_service_heartbeats(self, *, retention_days: int = 30) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        async with self._sessions.begin() as session:
            result = await session.execute(
                delete(ServiceHeartbeatRow).where(ServiceHeartbeatRow.last_seen_at < cutoff)
            )
            return int(result.rowcount or 0)

    async def operational_status(self, config: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC)
        cutoff = now - _HEARTBEAT_STALE_AFTER
        records = await self.list_service_heartbeats()
        grouped = {
            component: [row for row in records if row.component == component]
            for component in SERVICE_COMPONENTS
        }
        active_by_component = {
            component: [
                row
                for row in rows
                if row.stopped_at is None and row.last_seen_at >= cutoff
            ]
            for component, rows in grouped.items()
        }
        admin_active = active_by_component["admin"]
        target_fingerprint = admin_active[0].config_fingerprint if admin_active else None
        admin_starting = bool(admin_active) and (
            now - admin_active[0].started_at < _HEARTBEAT_STALE_AFTER
        )

        components: list[dict[str, Any]] = []
        pending_components: list[str] = []
        drift_components: list[str] = []
        active_revisions: set[str] = set()
        active_fingerprints: set[str] = set()
        for component in SERVICE_COMPONENTS:
            rows = grouped[component]
            active = active_by_component[component]
            latest = active[0] if active else rows[0] if rows else None
            if active:
                status = "healthy"
            elif latest is None:
                status = "starting" if admin_starting else "missing"
            elif latest.stopped_at is not None:
                status = "stopped"
            else:
                status = "stale"

            generation_current = bool(active) and all(
                row.applied_config_generation == config["generation"] for row in active
            )
            fingerprint_current = bool(active) and (
                target_fingerprint is None
                or all(row.config_fingerprint == target_fingerprint for row in active)
            )
            config_change_exists = bool(config["generation"] or config["changed_at"])
            if (
                component in CONFIG_APPLY_COMPONENTS
                and not generation_current
                and (bool(active) or config_change_exists)
            ):
                pending_components.append(component)
            if component in CONFIG_APPLY_COMPONENTS and active and not fingerprint_current:
                drift_components.append(component)

            for row in active:
                if row.revision and row.revision != "unknown":
                    active_revisions.add(row.revision)
                active_fingerprints.add(row.config_fingerprint)
            components.append(
                {
                    "component": component,
                    "status": status,
                    "active_instances": len(active),
                    "instance_id": latest.instance_id if latest else None,
                    "version": latest.version if latest else None,
                    "revision": latest.revision if latest else None,
                    "revision_short": (
                        latest.revision[:12]
                        if latest and latest.revision and latest.revision != "unknown"
                        else "unknown"
                    ),
                    "applied_config_generation": (
                        latest.applied_config_generation if latest else None
                    ),
                    "generation_current": generation_current,
                    "fingerprint_current": fingerprint_current,
                    "config_current": generation_current and fingerprint_current,
                    "started_at": latest.started_at if latest else None,
                    "last_seen_at": latest.last_seen_at if latest else None,
                    "last_seen_age": (
                        _format_duration(now - latest.last_seen_at) if latest else "never"
                    ),
                    "details": latest.details if latest else {},
                }
            )

        all_components_healthy = all(
            component["status"] in {"healthy", "starting"} for component in components
        )
        mixed_revisions = len(active_revisions) > 1
        mixed_config_fingerprints = len(active_fingerprints) > 1
        return {
            "components": components,
            "config": {
                **config,
                "target_fingerprint": target_fingerprint,
                "pending_components": pending_components,
                "drift_components": drift_components,
                "all_applied": not pending_components,
            },
            "mixed_revisions": mixed_revisions,
            "mixed_config_fingerprints": mixed_config_fingerprints,
            "all_components_healthy": all_components_healthy,
            "needs_attention": (
                not all_components_healthy
                or bool(pending_components)
                or bool(drift_components)
                or mixed_revisions
                or mixed_config_fingerprints
            ),
        }

    async def dashboard_snapshot(
        self,
        *,
        service_enabled: bool,
        enabled_projects: set[str],
        projects_total: int,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        cutoff = now - timedelta(hours=24)
        attention_condition = _job_attention_condition(
            now,
            service_enabled=service_enabled,
            enabled_projects=enabled_projects,
        )
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
            job_attention_count = await session.scalar(
                select(func.count())
                .select_from(Job)
                .outerjoin(Publication, Publication.job_id == Job.id)
                .where(attention_condition)
            )
            attention_rows = (
                await session.execute(
                    select(Job, Publication.status, Publication.updated_at)
                    .outerjoin(Publication, Publication.job_id == Job.id)
                    .where(attention_condition)
                    .order_by(Job.updated_at.asc())
                    .limit(200)
                )
            ).all()

        operations = await self.operational_status(config)
        attention: list[dict[str, Any]] = []
        for component in operations["components"]:
            if component["status"] in {"healthy", "starting"}:
                continue
            heartbeat_detail = (
                f"Last heartbeat: {component['last_seen_age']} ago."
                if component["last_seen_at"]
                else "No heartbeat has been recorded."
            )
            attention.append(
                {
                    "severity": "danger",
                    "kind": "component",
                    "title": f"{component['component'].title()} process is {component['status']}",
                    "detail": (
                        f"{heartbeat_detail} "
                        "Review the component logs and restart the service if needed."
                    ),
                    "href": f"/logs?component={component['component']}",
                    "action": "Open logs",
                    "job": None,
                }
            )
        if operations["config"]["pending_components"]:
            pending = ", ".join(operations["config"]["pending_components"])
            attention.append(
                {
                    "severity": "warning",
                    "kind": "config",
                    "title": (
                        "Configuration generation "
                        f"{operations['config']['generation']} is pending"
                    ),
                    "detail": f"Restart or repair these services: {pending}.",
                    "href": "/settings",
                    "action": "View settings",
                    "job": None,
                }
            )
        if operations["mixed_revisions"]:
            attention.append(
                {
                    "severity": "danger",
                    "kind": "revision",
                    "title": "Mixed application revisions are running",
                    "detail": (
                        "Receiver, worker, reconciler, and admin are not on one Git revision."
                    ),
                    "href": "/",
                    "action": "View processes",
                    "job": None,
                }
            )
        if operations["config"]["drift_components"]:
            drift = ", ".join(operations["config"]["drift_components"])
            attention.append(
                {
                    "severity": "warning",
                    "kind": "config-drift",
                    "title": "Runtime configuration differs between services",
                    "detail": f"Different non-secret settings are loaded by: {drift}.",
                    "href": "/settings",
                    "action": "View settings",
                    "job": None,
                }
            )
        for job, publication_status, publication_updated_at in attention_rows:
            item = _job_attention(
                job,
                publication_status,
                now,
                publication_updated_at=publication_updated_at,
                work_expected=service_enabled and job.project in enabled_projects,
            )
            if item is not None:
                attention.append(item)

        if service_enabled and not enabled_projects:
            attention.append(
                {
                    "severity": "warning",
                    "kind": "projects",
                    "title": "Review service is enabled with no enabled projects",
                    "detail": "No Gerrit Patch Set can be admitted until a project is enabled.",
                    "href": "/projects",
                    "action": "Open projects",
                    "job": None,
                }
            )
        attention.sort(key=_attention_sort_key)
        rendered_job_attention_count = sum(1 for item in attention if item.get("job") is not None)
        non_job_attention_count = len(attention) - rendered_job_attention_count
        exact_attention_count = int(job_attention_count or 0) + non_job_attention_count
        return {
            "states": {state: int(count) for state, count in state_rows},
            "recent_24h": int(recent_24h or 0),
            "failed_24h": int(failed_24h or 0),
            "projects_total": projects_total,
            "projects_enabled": len(enabled_projects),
            "recent_jobs": [_job_summary(job) for job in recent_jobs],
            "operations": operations,
            "attention": attention,
            "attention_count": exact_attention_count,
            "attention_truncated": int(job_attention_count or 0)
            > rendered_job_attention_count,
        }


def _service_heartbeat_record(row: ServiceHeartbeatRow) -> ServiceHeartbeatRecord:
    return ServiceHeartbeatRecord(
        component=row.component,
        instance_id=row.instance_id,
        version=row.version,
        revision=row.revision,
        config_fingerprint=row.config_fingerprint,
        applied_config_generation=row.applied_config_generation,
        details=dict(row.details or {}),
        started_at=row.started_at,
        last_seen_at=row.last_seen_at,
        stopped_at=row.stopped_at,
    )


def _format_duration(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _job_attention_condition(
    now: datetime,
    *,
    service_enabled: bool,
    enabled_projects: set[str],
):
    permanent_failure = Job.state == JobState.FAILED_PERMANENT.value
    ambiguous_publication = and_(
        Publication.status == "AMBIGUOUS",
        Publication.updated_at <= now - timedelta(minutes=10),
    )
    if not service_enabled or not enabled_projects:
        return or_(permanent_failure, ambiguous_publication)

    lease_required = [
        JobState.FETCHING.value,
        JobState.REVIEWING.value,
        JobState.VALIDATING.value,
        JobState.PUBLISHING.value,
    ]
    expected_work = and_(
        Job.project.in_(sorted(enabled_projects)),
        or_(
            and_(
                Job.state == JobState.RETRY_WAIT.value,
                Job.next_attempt_at <= now - _RETRY_OVERDUE_GRACE,
            ),
            and_(
                Job.state == JobState.RECEIVED.value,
                Job.updated_at <= now - _RECEIVED_QUEUE_GRACE,
            ),
            and_(
                Job.state == JobState.READY_TO_PUBLISH.value,
                Job.updated_at <= now - _UNCLAIMED_ACTIVE_GRACE,
            ),
            and_(
                Job.state.in_(lease_required),
                or_(
                    Job.lease_expires_at <= now - _LEASE_EXPIRED_GRACE,
                    Job.updated_at <= now - _UNCLAIMED_ACTIVE_GRACE,
                ),
            ),
        ),
    )
    return or_(permanent_failure, ambiguous_publication, expected_work)


def _job_attention(
    job: Job,
    publication_status: str | None,
    now: datetime,
    *,
    publication_updated_at: datetime | None = None,
    work_expected: bool = True,
) -> dict[str, Any] | None:
    state = JobState(job.state)
    age = now - job.updated_at
    base = {
        "job": _job_summary(job),
        "href": f"/jobs/{job.id}",
        "action": "Open audit",
        "age": _format_duration(age),
    }
    if state is JobState.FAILED_PERMANENT:
        error = job.last_error_class or "Permanent failure"
        message = (job.last_error or "No failure detail was recorded.").replace("\n", " ")[:220]
        return {
            **base,
            "severity": "danger",
            "kind": "failed-job",
            "title": f"{job.project} #{job.change_number} failed permanently",
            "detail": f"{error}: {message}",
        }

    publication_age = now - (publication_updated_at or job.updated_at)
    if publication_status == "AMBIGUOUS" and publication_age >= timedelta(minutes=10):
        return {
            **base,
            "severity": "danger",
            "kind": "ambiguous-publication",
            "title": f"Gerrit publication is unresolved for #{job.change_number}",
            "detail": (
                "The external POST outcome has remained ambiguous for "
                f"{_format_duration(publication_age)}."
            ),
        }

    if not work_expected:
        return None

    if state is JobState.RETRY_WAIT:
        overdue = now - job.next_attempt_at
        if overdue >= _RETRY_OVERDUE_GRACE:
            return {
                **base,
                "severity": "warning",
                "kind": "overdue-retry",
                "title": f"Retry is overdue for {job.project} #{job.change_number}",
                "detail": (
                    f"The {job.retry_state or 'unknown'} retry was due "
                    f"{_format_duration(overdue)} ago."
                ),
            }
        return None

    if state is JobState.RECEIVED:
        if age >= _RECEIVED_QUEUE_GRACE:
            return {
                **base,
                "severity": "warning",
                "kind": "aged-queue",
                "title": f"Job #{job.change_number} has not been claimed",
                "detail": f"It has remained in RECEIVED for {_format_duration(age)}.",
            }
        return None

    if state is JobState.READY_TO_PUBLISH:
        if age >= _UNCLAIMED_ACTIVE_GRACE:
            return {
                **base,
                "severity": "warning",
                "kind": "publish-queue",
                "title": f"Reviewed job #{job.change_number} is waiting to publish",
                "detail": f"READY_TO_PUBLISH has not been claimed for {_format_duration(age)}.",
            }
        return None

    lease_required = {
        JobState.FETCHING,
        JobState.REVIEWING,
        JobState.VALIDATING,
        JobState.PUBLISHING,
    }
    if state in lease_required:
        if (
            job.lease_expires_at is not None
            and job.lease_expires_at <= now - _LEASE_EXPIRED_GRACE
        ):
            return {
                **base,
                "severity": "danger",
                "kind": "expired-lease",
                "title": f"{state.value} lease expired for #{job.change_number}",
                "detail": (
                    f"Worker lease expired {_format_duration(now - job.lease_expires_at)} ago "
                    "and the job has not been reclaimed."
                ),
            }
        if (
            job.lease_owner is None or job.lease_expires_at is None
        ) and age >= _UNCLAIMED_ACTIVE_GRACE:
            return {
                **base,
                "severity": "warning",
                "kind": "missing-lease",
                "title": f"{state.value} job #{job.change_number} has no live owner",
                "detail": f"The state has remained unowned for {_format_duration(age)}.",
            }
        if age >= _UNCLAIMED_ACTIVE_GRACE:
            return {
                **base,
                "severity": "warning",
                "kind": "stale-active",
                "title": f"{state.value} job #{job.change_number} stopped heartbeating",
                "detail": (
                    f"The job still has a lease but updated_at has not advanced for "
                    f"{_format_duration(age)}."
                ),
            }
    return None


def _attention_sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
    severity = {"danger": 0, "warning": 1}.get(str(item.get("severity")), 2)
    return severity, str(item.get("kind", "")), str(item.get("title", ""))


def _job_summary(job: Job) -> dict[str, Any]:
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
        "next_attempt_at": job.next_attempt_at,
        "lease_owner": job.lease_owner,
        "lease_expires_at": job.lease_expires_at,
        "claimed_at": job.claimed_at,
        "last_error_class": job.last_error_class,
        "last_error": job.last_error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
