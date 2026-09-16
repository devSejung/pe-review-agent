from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from alembic import command
from alembic.config import Config

from pe_review_agent.config import load_settings
from pe_review_agent.observability import HealthServer, configure_logging
from pe_review_agent.service import (
    database_ready,
    run_receiver,
    run_reconciler,
    service_components,
)


def main() -> None:
    parser = argparse.ArgumentParser(prog="pe-review-agent")
    parser.add_argument(
        "command", choices=("receiver", "worker", "reconcile", "migrate", "check-config")
    )
    parser.add_argument("--config", help="path to YAML config (or PE_REVIEW_CONFIG)")
    parser.add_argument("--log-level", default=os.environ.get("PE_REVIEW_LOG_LEVEL", "INFO"))
    args = parser.parse_args()
    configure_logging(args.log_level)
    settings = load_settings(args.config)

    if args.command == "check-config":
        print("configuration valid")
        return
    if args.command == "migrate":
        _migrate(settings.database.dsn)
        return
    asyncio.run(_run_async(args.command, settings))


async def _run_async(command_name: str, settings) -> None:  # type: ignore[no-untyped-def]
    async with service_components(settings) as (database, store, gerrit, _llm, worker):
        if command_name == "receiver":
            await run_receiver(settings, store)
            return
        if command_name == "reconcile":
            await run_reconciler(settings, store, gerrit)
            return
        if command_name == "worker":
            health = HealthServer(
                settings.service.health_host,
                settings.service.health_port,
                ready_check=lambda: database_ready(database),
            )
            await health.start()
            try:
                await worker.run_forever()
            finally:
                await health.close()
            return
        raise ValueError(f"unsupported command {command_name}")


def _migrate(dsn: str) -> None:
    root = Path(__file__).resolve().parents[2]
    ini = root / "alembic.ini"
    if not ini.is_file():
        # Installed container layout copies Alembic resources under /app.
        ini = Path.cwd() / "alembic.ini"
    config = Config(str(ini))
    config.set_main_option("sqlalchemy.url", dsn.replace("%", "%%"))
    command.upgrade(config, "head")


if __name__ == "__main__":
    main()
