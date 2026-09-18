from pathlib import Path

import pytest
from pydantic import ValidationError

from pe_review_agent.config import (
    AdminSettings,
    DatabaseSettings,
    GerritRestAuth,
    GerritSettings,
    ReviewSettings,
    ServiceSettings,
    load_settings,
)


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


def test_admin_and_worker_ports_do_not_conflict() -> None:
    assert AdminSettings().port == 8080
    assert ServiceSettings().health_port == 8081


def test_gerrit_projects_can_start_empty_for_admin_web_bootstrap(tmp_path: Path) -> None:
    settings = GerritSettings(
        ssh_host="gerrit",
        ssh_user="bot",
        ssh_key_path=tmp_path / "key",
        rest_url="https://gerrit",
    )
    assert settings.projects == []


def test_review_language_defaults_to_korean_and_rejects_unknown_values() -> None:
    assert ReviewSettings().output_language == "ko-KR"
    assert ReviewSettings(output_language="en-US").output_language == "en-US"
    with pytest.raises(ValidationError, match="output_language"):
        ReviewSettings.model_validate({"output_language": "ja-JP"})


def test_none_gerrit_auth_never_reports_a_secret(monkeypatch) -> None:
    monkeypatch.setenv("PE_REVIEW_GERRIT_TOKEN", "should-not-be-used")
    auth = GerritRestAuth(mode="none", token_env="PE_REVIEW_GERRIT_TOKEN")
    assert auth.secret() is None


def test_review_tool_round_limit_allows_operational_headroom() -> None:
    assert ReviewSettings(max_tool_rounds=64).max_tool_rounds == 64
    with pytest.raises(ValidationError):
        ReviewSettings(max_tool_rounds=65)


def test_review_budget_defaults_and_bounds_are_validated() -> None:
    settings = ReviewSettings()
    assert settings.max_candidate_chunks == 12
    assert settings.max_llm_calls_per_job == 30
    assert settings.max_tool_calls_per_job == 50
    assert settings.verifier_budget_fraction == pytest.approx(1 / 3)
    assert settings.max_input_tokens_per_job is None
    assert "max_input_tokens_per_job" not in settings.model_dump()
    assert ReviewSettings(max_input_tokens_per_job=300_000).max_input_tokens_per_job == 300_000
    assert settings.chunk_checkpoint_retention_days == 30

    with pytest.raises(ValidationError):
        ReviewSettings(max_candidate_chunks=0)
    with pytest.raises(ValidationError):
        ReviewSettings(max_llm_calls_per_job=0)
    with pytest.raises(ValidationError):
        ReviewSettings(max_tool_calls_per_job=-1)
    with pytest.raises(ValidationError):
        ReviewSettings(verifier_budget_fraction=0)
    with pytest.raises(ValidationError):
        ReviewSettings(verifier_budget_fraction=1)
    with pytest.raises(ValidationError):
        ReviewSettings(max_input_tokens_per_job=999)
    with pytest.raises(ValidationError):
        ReviewSettings(chunk_checkpoint_retention_days=-1)


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
