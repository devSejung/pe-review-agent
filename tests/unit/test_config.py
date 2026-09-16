from pathlib import Path

import pytest
from pydantic import ValidationError

from pe_review_agent.config import DatabaseSettings, ServiceSettings, load_settings


def test_nested_env_override(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """\
gerrit:
  ssh_host: gerrit
  ssh_user: bot
  ssh_key_path: /tmp/key
  rest_url: https://gerrit
  projects: [team/fw]
llm:
  base_url: https://llm/v1
service:
  worker_concurrency: 2
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PE_REVIEW__SERVICE__WORKER_CONCURRENCY", "5")
    monkeypatch.setenv("PE_REVIEW__SERVICE__ENABLED", "false")
    settings = load_settings(config)
    assert settings.service.worker_concurrency == 5
    assert settings.service.enabled is False


def test_database_url_structurally_encodes_reserved_password(monkeypatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:/word%with?chars#")
    settings = DatabaseSettings()

    rendered = settings.connection_url().render_as_string(hide_password=False)

    assert rendered.startswith("postgresql+asyncpg://pe_review:")
    assert "p%40ss%3A%2Fword%25with%3Fchars%23" in rendered
    assert rendered.endswith("@postgres:5432/pe_review")


def test_unknown_service_setting_is_rejected() -> None:
    with pytest.raises(ValidationError, match="enabld"):
        ServiceSettings.model_validate({"enabld": False})


def test_unknown_nested_yaml_setting_fails_check_config(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """\
gerrit:
  ssh_host: gerrit
  ssh_user: bot
  ssh_key_path: /tmp/key
  rest_url: https://gerrit
  projects: [team/fw]
llm:
  base_url: https://llm/v1
service:
  enabld: false
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("PE_REVIEW__SERVICE__ENABLED", raising=False)

    with pytest.raises(ValidationError, match="enabld"):
        load_settings(config)
