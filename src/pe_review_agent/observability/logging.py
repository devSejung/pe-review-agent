from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_RESERVED = set(logging.LogRecord(None, 0, "", 0, "", (), None).__dict__)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "component": os.environ.get("PE_REVIEW_COMPONENT", "unknown"),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            if key in {"message", "asctime"}:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    formatter = JsonFormatter()

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_file := os.environ.get("PE_REVIEW_LOG_FILE"):
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        max_bytes = int(os.environ.get("PE_REVIEW_LOG_MAX_BYTES", str(10 * 1024 * 1024)))
        backups = int(os.environ.get("PE_REVIEW_LOG_BACKUPS", "5"))
        file_handler = RotatingFileHandler(
            path,
            maxBytes=max(1_048_576, max_bytes),
            backupCount=max(1, backups),
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    root.setLevel(level.upper())


def log_event(logger: logging.Logger, message: str, **fields: Any) -> None:
    logger.info(message, extra=fields)
