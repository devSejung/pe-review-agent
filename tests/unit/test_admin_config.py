from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pe_review_agent.admin.store import _attempt_display_status
from pe_review_agent.admin.web import create_admin_app
from pe_review_agent.config import Settings
from pe_review_agent.jobs.models import Attempt, Job


def _settings(*, host: str, auth_mode: str) -> Settings:
    return Settings.model_validate(
        {
            "gerrit": {
                "ssh_host": "gerrit",
                "ssh_user": "bot",
                "ssh_key_path": Path("/tmp/key"),
                "rest_url": "https://gerrit",
                "projects": ["team/fw"],
            },
            "llm": {"base_url": "https://llm/v1"},
            "admin": {"host": host, "auth_mode": auth_mode},
        }
    )


def test_no_auth_admin_is_restricted_to_loopback() -> None:
    with pytest.raises(ValueError, match="loopback"):
        create_admin_app(_settings(host="0.0.0.0", auth_mode="none"))


def test_no_auth_admin_is_allowed_on_loopback() -> None:
    app = create_admin_app(_settings(host="127.0.0.1", auth_mode="none"))
    assert app.title == "Gerrit AI Reviewer Admin"


def test_attempt_status_distinguishes_running_from_abandoned() -> None:
    now = datetime.now(UTC)
    job = Job(
        state="REVIEWING",
        lease_owner="worker-live",
        lease_expires_at=now + timedelta(minutes=1),
    )
    running = Attempt(worker_id="worker-live", stage="REVIEW", attempt_number=1)
    abandoned = Attempt(worker_id="worker-old", stage="REVIEW", attempt_number=2)

    assert _attempt_display_status(job, running, now) == "running"
    assert _attempt_display_status(job, abandoned, now) == "abandoned"

    running.success = True
    assert _attempt_display_status(job, running, now) == "success"
    abandoned.success = False
    assert _attempt_display_status(job, abandoned, now) == "failed"
