from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

from pe_review_agent.config import GerritSettings
from pe_review_agent.domain import GerritPatchsetEvent
from pe_review_agent.retry import PermanentError

from .allowlist import ProjectAllowlist

logger = logging.getLogger(__name__)


def parse_patchset_created(
    payload: str | bytes | Mapping[str, Any],
    *,
    allowlist: ProjectAllowlist | None,
) -> GerritPatchsetEvent | None:
    """Parse one stream-events record, returning None for irrelevant/unallowed events."""

    if isinstance(payload, (str, bytes)):
        decoded = json.loads(payload)
    else:
        decoded = dict(payload)
    if not isinstance(decoded, dict):
        raise ValueError("Gerrit stream event must be a JSON object")
    if decoded.get("type") != "patchset-created":
        return None

    change = _mapping(decoded, "change")
    patchset = _mapping(decoded, "patchSet")
    project = _string(change, "project")
    if allowlist is not None and not allowlist.allows(project):
        return None

    uploader = decoded.get("uploader")
    if not isinstance(uploader, Mapping):
        uploader = patchset.get("uploader")

    return GerritPatchsetEvent(
        project=project,
        change_number=_positive_int(change, "number"),
        patchset_number=_positive_int(patchset, "number"),
        revision_sha=_string(patchset, "revision"),
        ref=_optional_string(patchset.get("ref")),
        branch=_optional_string(change.get("branch")),
        change_id=_optional_string(change.get("id")),
        uploader=_account_name(uploader),
        raw=decoded,
    )


class GerritEventStream:
    """Reconnect Gerrit's SSH stream-events feed and yield allowlisted patch sets."""

    def __init__(
        self,
        settings: GerritSettings,
        *,
        sleeper=asyncio.sleep,
        filter_projects: bool = True,
    ) -> None:  # type: ignore[no-untyped-def]
        self._settings = settings
        self._allowlist = ProjectAllowlist(settings.projects) if filter_projects else None
        self._sleep = sleeper

    @property
    def ssh_command(self) -> tuple[str, ...]:
        settings = self._settings
        command = [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            f"StrictHostKeyChecking={'yes' if settings.strict_host_key_checking else 'no'}",
            "-i",
            str(settings.ssh_key_path),
            "-p",
            str(settings.ssh_port),
        ]
        if settings.known_hosts_path is not None:
            command.extend(["-o", f"UserKnownHostsFile={settings.known_hosts_path}"])
        command.extend(
            [
                f"{settings.ssh_user}@{settings.ssh_host}",
                "gerrit",
                "stream-events",
                "-s",
                "patchset-created",
            ]
        )
        return tuple(command)

    async def events(self) -> AsyncIterator[GerritPatchsetEvent]:
        reconnect_attempt = 0
        while True:
            process = await self._open_process()
            if process.stdout is None or process.stderr is None:
                _stop_process(process)
                await process.wait()
                raise RuntimeError("SSH stream process did not expose stdout/stderr")

            stderr_task = asyncio.create_task(_capture_stderr(process.stderr))
            saw_input = False
            try:
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    saw_input = True
                    try:
                        event = parse_patchset_created(line, allowlist=self._allowlist)
                    except (json.JSONDecodeError, ValueError) as exc:
                        logger.warning("Ignoring malformed Gerrit stream event: %s", exc)
                        continue
                    if event is not None:
                        yield event

                exit_code = await process.wait()
                stderr = await stderr_task
                if exit_code != 0 and _is_permanent_ssh_failure(stderr):
                    raise PermanentError(
                        f"Gerrit SSH stream-events failed permanently (exit {exit_code}): "
                        f"{_safe_stderr(stderr)}"
                    )
            finally:
                if process.returncode is None:
                    _stop_process(process)
                    await process.wait()
                if not stderr_task.done():
                    stderr_task.cancel()
                    await asyncio.gather(stderr_task, return_exceptions=True)

            reconnect_attempt = 1 if saw_input else reconnect_attempt + 1
            delay = min(
                self._settings.event_reconnect_max_seconds,
                self._settings.event_reconnect_min_seconds * (2 ** (reconnect_attempt - 1)),
            )
            logger.warning("Gerrit SSH event stream disconnected; reconnecting in %.1fs", delay)
            await self._sleep(delay)

    def __aiter__(self) -> AsyncIterator[GerritPatchsetEvent]:
        return self.events()

    async def _open_process(self) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
                *self.ssh_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise PermanentError(f"Unable to start ssh for Gerrit stream-events: {exc}") from exc


async def _capture_stderr(stream: asyncio.StreamReader, *, limit: int = 16_384) -> str:
    captured = bytearray()
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        remaining = limit - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
    return captured.decode("utf-8", errors="replace")


def _is_permanent_ssh_failure(stderr: str) -> bool:
    lowered = stderr.lower()
    markers = (
        "stream events not permitted",
        "permission denied",
        "host key verification failed",
        "no such identity",
        "bad permissions",
    )
    return any(marker in lowered for marker in markers)


def _safe_stderr(stderr: str) -> str:
    compact = " ".join(stderr.split())
    return compact[:500] or "no stderr"


def _stop_process(process: asyncio.subprocess.Process) -> None:
    try:
        process.terminate()
    except ProcessLookupError:
        pass


def _mapping(parent: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"Gerrit stream event is missing object field {key!r}")
    return value


def _string(parent: Mapping[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Gerrit stream event is missing string field {key!r}")
    return value


def _positive_int(parent: Mapping[str, Any], key: str) -> int:
    value = parent.get(key)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Gerrit stream event has invalid integer field {key!r}") from exc
    if parsed < 1:
        raise ValueError(f"Gerrit stream event has non-positive integer field {key!r}")
    return parsed


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _account_name(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    for key in ("username", "email", "name"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None
