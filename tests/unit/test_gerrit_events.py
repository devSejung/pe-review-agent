from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pe_review_agent.config import GerritSettings
from pe_review_agent.gerrit import GerritEventStream, ProjectAllowlist, parse_patchset_created
from pe_review_agent.retry import PermanentError


def _settings(**overrides: object) -> GerritSettings:
    values: dict[str, object] = {
        "ssh_host": "gerrit.internal",
        "ssh_port": 29418,
        "ssh_user": "review-bot",
        "ssh_key_path": Path("/run/secrets/gerrit_key"),
        "known_hosts_path": Path("/run/secrets/known_hosts"),
        "rest_url": "https://gerrit.internal",
        "projects": ["team/fw"],
        "event_reconnect_min_seconds": 0.1,
        "event_reconnect_max_seconds": 1.0,
    }
    values.update(overrides)
    return GerritSettings.model_validate(values)


def _event(*, project: str = "team/fw", revision: str = "a" * 40) -> dict[str, object]:
    return {
        "type": "patchset-created",
        "change": {
            "project": project,
            "number": "123",
            "branch": "main",
            "id": "Ideadbeef",
        },
        "patchSet": {
            "number": "4",
            "revision": revision,
            "ref": "refs/changes/23/123/4",
        },
        "uploader": {"username": "alice"},
        "eventCreatedOn": 1_789_611_600,
    }


def test_project_allowlist_is_exact_match() -> None:
    allowlist = ProjectAllowlist(["team/fw", "team/drivers"])

    assert allowlist.allows("team/fw")
    assert not allowlist.allows("team/fw/subrepo")
    assert not allowlist.allows("team/*")

    with pytest.raises(PermanentError):
        allowlist.require("other/fw")


def test_parse_patchset_created_maps_gerrit_stream_schema() -> None:
    parsed = parse_patchset_created(_event(), allowlist=ProjectAllowlist(["team/fw"]))

    assert parsed is not None
    assert parsed.project == "team/fw"
    assert parsed.change_number == 123
    assert parsed.patchset_number == 4
    assert parsed.revision_sha == "a" * 40
    assert parsed.ref == "refs/changes/23/123/4"
    assert parsed.branch == "main"
    assert parsed.change_id == "Ideadbeef"
    assert parsed.uploader == "alice"
    assert parsed.occurred_at == datetime.fromtimestamp(1_789_611_600, tz=UTC)


def test_parse_patchset_created_filters_non_allowlisted_projects() -> None:
    parsed = parse_patchset_created(
        _event(project="team/secret"),
        allowlist=ProjectAllowlist(["team/fw"]),
    )

    assert parsed is None


def test_ssh_command_subscribes_only_to_patchset_created() -> None:
    stream = GerritEventStream(_settings(strict_host_key_checking=True))

    command = stream.ssh_command
    assert command[-5:] == (
        "review-bot@gerrit.internal",
        "gerrit",
        "stream-events",
        "-s",
        "patchset-created",
    )
    assert "StrictHostKeyChecking=yes" in command
    known_hosts_options = [arg for arg in command if arg.startswith("UserKnownHostsFile=")]
    assert len(known_hosts_options) == 1
    assert known_hosts_options[0].replace("\\", "/").endswith("/run/secrets/known_hosts")


class _FakeReader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def readline(self) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    async def read(self, _: int = -1) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class _FakeProcess:
    def __init__(self, stdout_lines: list[bytes], *, exit_code: int, stderr: bytes = b"") -> None:
        self.stdout = _FakeReader(stdout_lines)
        self.stderr = _FakeReader([stderr] if stderr else [])
        self._exit_code = exit_code
        self.returncode: int | None = None

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = self._exit_code
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15


class _FakeEventStream(GerritEventStream):
    def __init__(self, settings: GerritSettings, processes: list[_FakeProcess], *, sleeper) -> None:  # type: ignore[no-untyped-def]
        super().__init__(settings, sleeper=sleeper)
        self._processes = list(processes)

    async def _open_process(self) -> _FakeProcess:  # type: ignore[override]
        return self._processes.pop(0)


@pytest.mark.asyncio
async def test_stream_reconnects_after_transport_disconnect() -> None:
    first = _FakeProcess(
        [(json.dumps(_event(revision="a" * 40)) + "\n").encode()],
        exit_code=255,
        stderr=b"Connection reset by peer",
    )
    second = _FakeProcess(
        [(json.dumps(_event(revision="b" * 40)) + "\n").encode()],
        exit_code=0,
    )
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    stream = _FakeEventStream(_settings(), [first, second], sleeper=sleeper)
    events = stream.events()

    event_one = await anext(events)
    event_two = await anext(events)
    await events.aclose()

    assert event_one.revision_sha == "a" * 40
    assert event_two.revision_sha == "b" * 40
    assert delays == [0.1]


@pytest.mark.asyncio
async def test_stream_fails_permanently_when_capability_is_missing() -> None:
    process = _FakeProcess([], exit_code=1, stderr=b"fatal: stream events not permitted")

    async def sleeper(_: float) -> None:
        pytest.fail("permanent SSH failures must not reconnect")

    stream = _FakeEventStream(_settings(), [process], sleeper=sleeper)
    events = stream.events()

    with pytest.raises(PermanentError, match="stream events not permitted"):
        await anext(events)
