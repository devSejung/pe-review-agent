from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from pe_review_agent.config import ReviewSettings


class RepositoryToolExecutor:
    """Read-only, path-contained tools exposed to the review model."""

    def __init__(self, root: str | Path, settings: ReviewSettings) -> None:
        self.root = Path(root).resolve()
        self.settings = settings

    @property
    def tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": (
                        "Read a bounded line range from a repository text file. Large text files "
                        "are supported; prefer a targeted range after search_text. Omitting line "
                        "bounds reads at most 200 lines."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "start_line": {"type": "integer", "minimum": 1},
                            "end_line": {"type": "integer", "minimum": 1},
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_text",
                    "description": "Search tracked repository files for a literal text or symbol.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "minLength": 1},
                            "path": {"type": "string"},
                            "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": (
                        "List tracked files under an optional repository-relative directory."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "additionalProperties": False,
                    },
                },
            },
        ]

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "read_file":
            path = arguments.get("path")
            if not isinstance(path, str) or not path:
                return json.dumps({"error": "read_file requires non-empty string path"})
            end_line = arguments.get("end_line")
            if end_line is not None:
                try:
                    end_line = int(end_line)
                except (TypeError, ValueError):
                    return json.dumps({"error": "read_file end_line must be an integer"})
                if end_line < 1:
                    return json.dumps({"error": "read_file end_line must be positive"})
            return await asyncio.to_thread(
                self.read_file,
                path,
                start_line=_bounded_int(arguments.get("start_line", 1), default=1, low=1),
                end_line=end_line,
            )
        if name == "search_text":
            query = arguments.get("query")
            if not isinstance(query, str) or not query:
                return json.dumps({"error": "search_text requires non-empty string query"})
            path = arguments.get("path")
            if path is not None and not isinstance(path, str):
                return json.dumps({"error": "search_text path must be a string"})
            return await self.search_text(
                query,
                path=path,
                max_results=_bounded_int(
                    arguments.get("max_results", 40), default=40, low=1, high=100
                ),
            )
        if name == "list_files":
            path = arguments.get("path")
            if path is not None and not isinstance(path, str):
                return json.dumps({"error": "list_files path must be a string"})
            return await self.list_files(path=path)
        return json.dumps({"error": f"unknown repository tool: {name}"})

    def read_file(self, path: str, *, start_line: int = 1, end_line: int | None = None) -> str:
        target = self._resolve(path)
        if not target.is_file():
            return json.dumps({"error": "file not found", "path": path})
        start = max(1, start_line)
        end = int(end_line) if end_line else start + 199
        return _read_text_range_bounded(
            target,
            path=path,
            start_line=start,
            end_line=end,
            max_output_bytes=self.settings.max_tool_output_bytes,
        )

    async def search_text(
        self, query: str, *, path: str | None = None, max_results: int = 40
    ) -> str:
        args = [
            "git",
            "-C",
            str(self.root),
            "grep",
            "-n",
            "-F",
            "--full-name",
            "-e",
            query,
        ]
        if path:
            self._resolve(path)
            args.extend(["--", path])
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await self._communicate(proc, "git grep")
        if proc.returncode not in (0, 1):
            return json.dumps(
                {"error": "git grep failed", "detail": stderr.decode(errors="replace")[:1000]}
            )
        text = stdout.decode("utf-8", errors="replace")
        truncated_by_bytes = "<tool output truncated by byte limit>" in text
        lines = [
            line for line in text.splitlines() if line != "<tool output truncated by byte limit>"
        ][:max_results]
        if truncated_by_bytes:
            lines.append("<tool output truncated by byte limit>")
        return "\n".join(lines) if lines else "<no matches>"

    async def list_files(self, *, path: str | None = None) -> str:
        args = ["git", "-C", str(self.root), "ls-files"]
        if path:
            self._resolve(path)
            args.extend(["--", path])
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await self._communicate(proc, "git ls-files")
        if proc.returncode != 0:
            return json.dumps(
                {"error": "git ls-files failed", "detail": stderr.decode(errors="replace")[:1000]}
            )
        text = stdout.decode("utf-8", errors="replace")
        truncated_by_bytes = "<tool output truncated by byte limit>" in text
        lines = [
            line for line in text.splitlines() if line != "<tool output truncated by byte limit>"
        ][:500]
        if truncated_by_bytes:
            lines.append("<tool output truncated by byte limit>")
        return "\n".join(lines) if lines else "<no tracked files>"

    def _resolve(self, relative: str) -> Path:
        value = relative.replace("\\", "/").lstrip("/")
        target = (self.root / value).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"repository path escapes workspace: {relative!r}") from exc
        return target

    async def _communicate(
        self, proc: asyncio.subprocess.Process, operation: str
    ) -> tuple[bytes, bytes]:
        stdout_task = asyncio.create_task(
            _read_stream_bounded(proc.stdout, self.settings.max_tool_output_bytes)
        )
        stderr_task = asyncio.create_task(_read_stream_bounded(proc.stderr, 64_000))
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.settings.tool_command_timeout_seconds)
        except TimeoutError:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            stdout_task.cancel()
            stderr_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            return b"", json.dumps({"error": f"{operation} timed out"}).encode()
        stdout, stdout_truncated = await stdout_task
        stderr, stderr_truncated = await stderr_task
        if stdout_truncated:
            stdout += b"\n<tool output truncated by byte limit>"
        if stderr_truncated:
            stderr += b"\n<stderr truncated by byte limit>"
        return stdout, stderr


async def _read_stream_bounded(
    stream: asyncio.StreamReader | None, limit: int
) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    kept = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            break
        remaining = limit - len(kept)
        if remaining > 0:
            kept.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            truncated = True
    return bytes(kept), truncated


def _bounded_int(
    value: Any,
    *,
    default: int,
    low: int,
    high: int | None = None,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    parsed = max(low, parsed)
    return min(parsed, high) if high is not None else parsed


def _truncate_text_bytes(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    marker = "\n<tool output truncated by byte limit>"
    marker_bytes = marker.encode("utf-8")
    keep = max(0, limit - len(marker_bytes))
    prefix = encoded[:keep].decode("utf-8", errors="ignore")
    return prefix + marker


def _read_text_range_bounded(
    target: Path,
    *,
    path: str,
    start_line: int,
    end_line: int,
    max_output_bytes: int,
) -> str:
    """Read only the requested lines without loading an arbitrarily large file into memory."""

    if end_line < start_line:
        return "<empty>"

    parts: list[str] = []
    used_bytes = 0
    try:
        with target.open("rb") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if line_number < start_line:
                    continue
                if line_number > end_line:
                    break

                raw_line = raw_line.rstrip(b"\r\n")
                if b"\x00" in raw_line:
                    return json.dumps({"error": "binary/non-UTF8 file", "path": path})
                try:
                    text = raw_line.decode("utf-8")
                except UnicodeDecodeError:
                    return json.dumps({"error": "binary/non-UTF8 file", "path": path})

                rendered = f"{line_number}: {text}"
                separator_bytes = 1 if parts else 0
                rendered_bytes = len(rendered.encode("utf-8"))
                if used_bytes + separator_bytes + rendered_bytes > max_output_bytes:
                    candidate = "\n".join([*parts, rendered])
                    return _truncate_text_bytes(candidate, max_output_bytes)

                parts.append(rendered)
                used_bytes += separator_bytes + rendered_bytes
    except OSError as exc:
        return json.dumps({"error": str(exc), "path": path})

    return "\n".join(parts) if parts else "<empty>"
