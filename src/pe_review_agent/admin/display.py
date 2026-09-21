from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

_KST = timezone(timedelta(hours=9), name="KST")


def format_seoul_time(value: Any) -> str:
    """Render stored UTC timestamps for operators in Korea without changing persistence."""

    if value is None or value == "":
        return "—"
    if isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            value = datetime.fromisoformat(normalized)
        except ValueError:
            return value
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(_KST).strftime("%Y-%m-%d %H:%M:%S KST")
