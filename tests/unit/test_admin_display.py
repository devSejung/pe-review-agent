from datetime import UTC, datetime

from pe_review_agent.admin.display import format_seoul_time


def test_format_seoul_time_converts_utc_datetime_and_iso_string() -> None:
    expected = "2026-09-21 14:35:06 KST"
    value = datetime(2026, 9, 21, 5, 35, 6, tzinfo=UTC)

    assert format_seoul_time(value) == expected
    assert format_seoul_time("2026-09-21T05:35:06+00:00") == expected
    assert format_seoul_time("2026-09-21T05:35:06Z") == expected


def test_format_seoul_time_preserves_unknown_text_and_handles_none() -> None:
    assert format_seoul_time("not-a-timestamp") == "not-a-timestamp"
    assert format_seoul_time(None) == "—"
