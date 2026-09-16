from pathlib import Path

import pytest

from pe_review_agent.admin.web import create_admin_app
from pe_review_agent.config import Settings


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
