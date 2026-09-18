from datetime import UTC, datetime, timedelta

import pytest

from pe_review_agent.admin.store import ManagedProjectRecord
from pe_review_agent.config import Settings
from pe_review_agent.jobs import ProjectReviewStartMode
from pe_review_agent.service import (
    _CHECKPOINT_CLEANUP_WATERMARK_KEY,
    _RECONCILIATION_FULL_SWEEP_KEY,
    _RECONCILIATION_WATERMARK_KEY,
    run_reconciler,
)


class _Store:
    def __init__(self, *, watermark: datetime, full_sweep: datetime | None) -> None:
        self.values = {
            _RECONCILIATION_WATERMARK_KEY: watermark,
            _RECONCILIATION_FULL_SWEEP_KEY: full_sweep,
            _CHECKPOINT_CLEANUP_WATERMARK_KEY: None,
        }
        self.advanced: list[tuple[str, datetime]] = []
        self.pruned_retention_days: list[int] = []

    async def get_service_watermark(self, key: str):
        return self.values.get(key)

    async def advance_service_watermark(self, key: str, value: datetime) -> None:
        self.values[key] = value
        self.advanced.append((key, value))

    async def enqueue(self, *_args, **_kwargs):
        raise AssertionError("no events expected")

    async def prune_review_recovery_cache(self, *, retention_days: int) -> int:
        self.pruned_retention_days.append(retention_days)
        return 0


class _Gerrit:
    def __init__(self) -> None:
        self.since_values: list[datetime | None] = []
        self.project_since_values: list[dict[str, datetime | None] | None] = []

    async def reconciliation_events(
        self,
        *,
        since: datetime | None,
        project_since: dict[str, datetime | None] | None = None,
    ):
        self.since_values.append(since)
        self.project_since_values.append(project_since)
        return []

    def replace_projects(self, _projects) -> None:
        return None


class _Control:
    def __init__(self, scope: ManagedProjectRecord) -> None:
        self.scope = scope

    async def service_enabled(self, *, default: bool) -> bool:
        return default

    async def enabled_project_scopes(self):
        return (self.scope,)


def _settings(tmp_path) -> Settings:
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
            "service": {
                "reconcile_interval_seconds": 300,
                "reconcile_full_sweep_interval_seconds": 3600,
            },
        }
    )


@pytest.mark.asyncio
async def test_reconciler_runs_periodic_full_open_change_sweep(tmp_path, monkeypatch) -> None:
    now = datetime.now(UTC)
    store = _Store(watermark=now - timedelta(minutes=5), full_sweep=now - timedelta(hours=2))
    gerrit = _Gerrit()

    async def stop_after_pass(_seconds: float) -> None:
        raise RuntimeError("stop")

    monkeypatch.setattr("pe_review_agent.service.asyncio.sleep", stop_after_pass)
    with pytest.raises(RuntimeError, match="stop"):
        await run_reconciler(_settings(tmp_path), store, gerrit)  # type: ignore[arg-type]

    assert gerrit.since_values == [None]
    assert gerrit.project_since_values == [None]
    assert any(key == _RECONCILIATION_FULL_SWEEP_KEY for key, _ in store.advanced)


@pytest.mark.asyncio
async def test_reconciler_uses_overlap_between_full_sweeps(tmp_path, monkeypatch) -> None:
    now = datetime.now(UTC)
    watermark = now - timedelta(minutes=1)
    store = _Store(watermark=watermark, full_sweep=now - timedelta(minutes=10))
    gerrit = _Gerrit()

    async def stop_after_pass(_seconds: float) -> None:
        raise RuntimeError("stop")

    monkeypatch.setattr("pe_review_agent.service.asyncio.sleep", stop_after_pass)
    with pytest.raises(RuntimeError, match="stop"):
        await run_reconciler(_settings(tmp_path), store, gerrit)  # type: ignore[arg-type]

    assert gerrit.since_values == [watermark - timedelta(seconds=300)]
    assert gerrit.project_since_values == [None]
    assert not any(key == _RECONCILIATION_FULL_SWEEP_KEY for key, _ in store.advanced)


@pytest.mark.asyncio
async def test_reconciler_full_sweep_still_respects_from_now_project_cutoff(
    tmp_path, monkeypatch
) -> None:
    now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=2)
    store = _Store(watermark=now - timedelta(minutes=5), full_sweep=now - timedelta(hours=2))
    gerrit = _Gerrit()
    control = _Control(
        ManagedProjectRecord(
            project="team/fw",
            enabled=True,
            review_start_mode=ProjectReviewStartMode.FROM_NOW,
            review_start_at=cutoff,
            created_at=cutoff,
            updated_at=cutoff,
        )
    )

    async def stop_after_pass(_seconds: float) -> None:
        raise RuntimeError("stop")

    monkeypatch.setattr("pe_review_agent.service.asyncio.sleep", stop_after_pass)
    with pytest.raises(RuntimeError, match="stop"):
        await run_reconciler(
            _settings(tmp_path),
            store,
            gerrit,
            control=control,  # type: ignore[arg-type]
        )

    assert gerrit.since_values == [None]
    assert gerrit.project_since_values == [{"team/fw": cutoff}]


@pytest.mark.asyncio
async def test_switching_to_include_open_requests_immediate_full_sweep(
    tmp_path, monkeypatch
) -> None:
    now = datetime.now(UTC)
    last_full = now - timedelta(minutes=10)
    store = _Store(watermark=now - timedelta(minutes=1), full_sweep=last_full)
    gerrit = _Gerrit()
    control = _Control(
        ManagedProjectRecord(
            project="team/fw",
            enabled=True,
            review_start_mode=ProjectReviewStartMode.INCLUDE_OPEN,
            review_start_at=None,
            created_at=now - timedelta(days=1),
            updated_at=now,
        )
    )

    async def stop_after_pass(_seconds: float) -> None:
        raise RuntimeError("stop")

    monkeypatch.setattr("pe_review_agent.service.asyncio.sleep", stop_after_pass)
    with pytest.raises(RuntimeError, match="stop"):
        await run_reconciler(
            _settings(tmp_path),
            store,
            gerrit,
            control=control,  # type: ignore[arg-type]
        )

    assert gerrit.since_values == [None]
    assert gerrit.project_since_values == [{"team/fw": None}]
    assert any(key == _RECONCILIATION_FULL_SWEEP_KEY for key, _ in store.advanced)
