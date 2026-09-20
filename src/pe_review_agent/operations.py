from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pe_review_agent import __version__
from pe_review_agent.admin import OperationsStore
from pe_review_agent.config import Settings

logger = logging.getLogger(__name__)

ServiceComponent = Literal["receiver", "worker", "reconciler", "admin"]


def settings_fingerprint(settings: Settings) -> str:
    """Hash effective non-secret settings that long-running clients actually load."""

    payload = {
        # Project enablement is a live DB control. It must not look like startup-config drift when
        # one process restarts after an operator adds or disables a project.
        "gerrit": settings.gerrit.model_dump(mode="json", exclude={"projects"}),
        "llm": settings.llm.model_dump(mode="json"),
        "repos": settings.repos.model_dump(mode="json"),
        "review": settings.review.model_dump(mode="json"),
        "retry": settings.retry.model_dump(mode="json"),
        "service": settings.service.model_dump(mode="json"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def runtime_revision() -> str:
    configured = os.environ.get("PE_REVIEW_BUILD_SHA", "").strip()
    if configured:
        return configured
    root = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    revision = result.stdout.strip() or "unknown"
    return f"{revision}-dirty" if dirty.stdout.strip() else revision


class ServiceHeartbeat:
    """Publish process liveness and the exact runtime configuration loaded by this process."""

    def __init__(
        self,
        operations: OperationsStore,
        component: ServiceComponent,
        *,
        applied_config_generation: int,
        effective_settings: Settings,
        details: dict[str, Any] | None = None,
        interval_seconds: float = 15.0,
    ) -> None:
        self.operations = operations
        self.component = component
        self.instance_id = str(uuid.uuid4())
        self.version = (
            os.environ.get("PE_REVIEW_BUILD_VERSION", __version__).strip() or __version__
        )[:64]
        self.revision = runtime_revision()[:64]
        self.applied_config_generation = applied_config_generation
        self.config_fingerprint = settings_fingerprint(effective_settings)
        self.details = dict(details or {})
        self.interval_seconds = interval_seconds
        self.started_at = datetime.now(UTC)
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> ServiceHeartbeat:
        await self.operations.prune_service_heartbeats()
        await self._beat()
        self._task = asyncio.create_task(
            self._run(),
            name=f"{self.component}-service-heartbeat",
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        try:
            await self.operations.stop_service_heartbeat(self.component, self.instance_id)
        except Exception:
            logger.exception("failed to mark %s heartbeat stopped", self.component)

    async def apply_configuration(
        self,
        generation: int,
        effective_settings: Settings,
    ) -> None:
        self.applied_config_generation = generation
        self.config_fingerprint = settings_fingerprint(effective_settings)
        await self._beat()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            try:
                await self._beat()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("failed to publish %s service heartbeat", self.component)

    async def _beat(self) -> None:
        await self.operations.record_service_heartbeat(
            component=self.component,
            instance_id=self.instance_id,
            version=self.version,
            revision=self.revision,
            config_fingerprint=self.config_fingerprint,
            applied_config_generation=self.applied_config_generation,
            started_at=self.started_at,
            details=self.details,
        )
