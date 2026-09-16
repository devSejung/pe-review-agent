from __future__ import annotations

import json
import logging

from pe_review_agent.observability import configure_logging


def test_configure_logging_writes_rotating_structured_file_with_exception(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "worker.jsonl"
    monkeypatch.setenv("PE_REVIEW_COMPONENT", "worker")
    monkeypatch.setenv("PE_REVIEW_LOG_FILE", str(path))
    monkeypatch.setenv("PE_REVIEW_LOG_MAX_BYTES", "1048576")
    monkeypatch.setenv("PE_REVIEW_LOG_BACKUPS", "2")

    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    try:
        configure_logging("INFO")
        logger = logging.getLogger("pe_review_agent.test")
        logger.info("review started", extra={"job_id": "job-123"})
        try:
            raise ValueError("full failure")
        except ValueError:
            logger.exception("review crashed", extra={"job_id": "job-123"})
        for handler in root.handlers:
            handler.flush()

        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert entries[0]["component"] == "worker"
        assert entries[0]["job_id"] == "job-123"
        assert entries[0]["message"] == "review started"
        assert entries[1]["level"] == "ERROR"
        assert "ValueError: full failure" in entries[1]["exception"]
    finally:
        for handler in list(root.handlers):
            if handler not in original_handlers:
                handler.close()
        root.handlers[:] = original_handlers
        root.setLevel(original_level)
