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
                    "description": "Read a bounded line range from a repository text file.",
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
            return self.read_file(
                arguments["path"],
                start_line=int(arguments.get("start_line", 1)),
                end_line=arguments.get("end_line"),
            )
        if name == "search_text":
            return await self.search_text(
                arguments["query"],
                path=arguments.get("path"),
                max_results=int(arguments.get("max_results", 40)),
            )
        if name == "list_files":
            return await self.list_files(path=arguments.get("path"))
        return json.dumps({"error": f"unknown repository tool: {name}"})

    def read_file(self, path: str, *, start_line: int = 1, end_line: int | None = None) -> str:
        target = self._resolve(path)
        if not target.is_file():
            return json.dumps({"error": "file not found", "path": path})
        if target.stat().st_size > self.settings.max_context_file_bytes:
            return json.dumps(
                {
                    "error": "file exceeds context size limit",
                    "path": path,
                    "size": target.stat().st_size,
                }
            )
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return json.dumps({"error": "binary/non-UTF8 file", "path": path})
        lines = text.splitlines()
        start = max(1, start_line)
        end = min(len(lines), int(end_line) if end_line else start + 199)
        payload = "\n".join(f"{idx}: {lines[idx - 1]}" for idx in range(start, end + 1))
        return payload or "<empty>"

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
        stdout, stderr = await proc.communicate()
        if proc.returncode not in (0, 1):
            return json.dumps(
                {"error": "git grep failed", "detail": stderr.decode(errors="replace")[:1000]}
            )
        lines = stdout.decode("utf-8", errors="replace").splitlines()[:max_results]
        return "\n".join(lines) if lines else "<no matches>"

    async def list_files(self, *, path: str | None = None) -> str:
        args = ["git", "-C", str(self.root), "ls-files"]
        if path:
            self._resolve(path)
            args.extend(["--", path])
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return json.dumps(
                {"error": "git ls-files failed", "detail": stderr.decode(errors="replace")[:1000]}
            )
        lines = stdout.decode("utf-8", errors="replace").splitlines()[:500]
        return "\n".join(lines) if lines else "<no tracked files>"

    def _resolve(self, relative: str) -> Path:
        value = relative.replace("\\", "/").lstrip("/")
        target = (self.root / value).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"repository path escapes workspace: {relative!r}") from exc
        return target
