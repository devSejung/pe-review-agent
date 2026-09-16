from datetime import UTC, datetime, timedelta

import pytest

from pe_review_agent.config import Settings
from pe_review_agent.service import (
    _RECONCILIATION_FULL_SWEEP_KEY,
    _RECONCILIATION_WATERMARK_KEY,
    run_reconciler,
)


class _Store:
    def __init__(self, *, watermark: datetime, full_sweep: datetime | None) -> None:
        self.values = {
            _RECONCILIATION_WATERMARK_KEY: watermark,
            _RECONCILIATION_FULL_SWEEP_KEY: full_sweep,
        }
        self.advanced: list[tuple[str, datetime]] = []

    async def get_service_watermark(self, key: str):
        return self.values.get(key)

    async def advance_service_watermark(self, key: str, value: datetime) -> None:
        self.values[key] = value
        self.advanced.append((key, value))

    async def enqueue(self, *_args, **_kwargs):
        raise AssertionError("no events expected")


class _Gerrit:
    def __init__(self) -> None:
        self.since_values: list[datetime | None] = []

    async def reconciliation_events(self, *, since: datetime | None):
        self.since_values.append(since)
        return []


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
    assert not any(key == _RECONCILIATION_FULL_SWEEP_KEY for key, _ in store.advanced)
