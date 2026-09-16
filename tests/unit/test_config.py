from pathlib import Path

from pe_review_agent.config import load_settings


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
    settings = load_settings(config)
    assert settings.service.worker_concurrency == 5
