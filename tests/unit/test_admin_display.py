from datetime import UTC, datetime

from pe_review_agent.admin.display import format_display_time


def test_format_display_time_converts_utc_datetime_and_iso_string() -> None:
    expected = "2026-09-21 14:35:06 KST"
    value = datetime(2026, 9, 21, 5, 35, 6, tzinfo=UTC)

    assert format_display_time(value, "Asia/Seoul") == expected
    assert format_display_time("2026-09-21T05:35:06+00:00", "Asia/Seoul") == expected
    assert format_display_time("2026-09-21T05:35:06Z", "Asia/Seoul") == expected


def test_format_display_time_supports_other_iana_timezones() -> None:
    value = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)

    assert format_display_time(value, "UTC") == "2026-07-01 12:00:00 UTC"
    assert format_display_time(value, "America/New_York") == "2026-07-01 08:00:00 EDT"


def test_format_display_time_preserves_unknown_text_and_handles_none() -> None:
    assert format_display_time("not-a-timestamp", "Asia/Seoul") == "not-a-timestamp"
    assert format_display_time(None, "Asia/Seoul") == "—"
