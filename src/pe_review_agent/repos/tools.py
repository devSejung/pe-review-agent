from __future__ import annotations

import asyncio
import codecs
import json
from pathlib import Path
from typing import Any

from pe_review_agent.config import ReviewSettings

_SEARCH_CONTEXT_RADIUS = 12
_SEARCH_CONTEXT_MATCH_LIMIT = 6
_SEARCH_OUTPUT_BYTES = 32 * 1024
_SEARCH_MATCH_LIST_BYTES = 6 * 1024
_SEARCH_CONTEXT_WINDOW_BYTES = 24 * 1024
_SEARCH_CONTEXT_LINE_BYTES = 128
_SEARCH_MATCH_PREVIEW_CHARS = 220
_BATCH_READ_MAX_RANGES = 6
_BATCH_READ_MAX_LINES = 200
_BATCH_READ_OUTPUT_BYTES = 64 * 1024
_FILE_READ_CHUNK_BYTES = 64 * 1024


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
                        "bounds reads at most 200 lines. Use batch_read when several explicit "
                        "ranges are needed at once."
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
                    "name": "batch_read",
                    "description": (
                        "Read up to six high-priority repository file ranges in one tool call. "
                        "Each range is capped at 200 lines. If more than six ranges are relevant, "
                        "request the six most important first and use another batch_read only if "
                        "the remaining evidence is still needed."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "ranges": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": _BATCH_READ_MAX_RANGES,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "path": {"type": "string"},
                                        "start_line": {"type": "integer", "minimum": 1},
                                        "end_line": {"type": "integer", "minimum": 1},
                                    },
                                    "required": ["path", "start_line", "end_line"],
                                    "additionalProperties": False,
                                },
                            }
                        },
                        "required": ["ranges"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_text",
                    "description": (
                        "Search tracked repository files for a literal text or symbol. The top six "
                        "matches include approximately 12 lines of surrounding context on each "
                        "side; remaining matches stay location-only. Overlapping context windows "
                        "are merged."
                    ),
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
        if name == "batch_read":
            ranges = arguments.get("ranges")
            if not isinstance(ranges, list) or not ranges:
                return json.dumps({"error": "batch_read requires a non-empty ranges array"})
            return await asyncio.to_thread(self.batch_read, ranges)
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

    def batch_read(self, ranges: list[Any]) -> str:
        selected = ranges[:_BATCH_READ_MAX_RANGES]
        total_limit = min(self.settings.max_tool_output_bytes, _BATCH_READ_OUTPUT_BYTES)
        per_range_limit = max(512, (total_limit - 1024) // max(1, len(selected)))
        sections: dict[int, str] = {}
        pending_by_file: dict[Path, list[tuple[int, str, int, int, int]]] = {}
        for index, item in enumerate(selected, start=1):
            if not isinstance(item, dict):
                sections[index] = f"[{index}] ERROR: range must be an object"
                continue
            path = item.get("path")
            if not isinstance(path, str) or not path:
                sections[index] = f"[{index}] ERROR: range requires non-empty string path"
                continue
            start = _positive_int(item.get("start_line"))
            end = _positive_int(item.get("end_line"))
            if start is None or end is None:
                sections[index] = (
                    f"[{index}] {path} ERROR: start_line and end_line must be positive integers"
                )
                continue
            if end < start:
                sections[index] = (
                    f"[{index}] {path}:{start}-{end} ERROR: end_line must be >= start_line"
                )
                continue
            requested_end = end
            end = min(end, start + _BATCH_READ_MAX_LINES - 1)
            try:
                target = self._resolve(path)
            except ValueError as exc:
                sections[index] = f"[{index}] {path}:{start}-{end} ERROR: {exc}"
                continue
            if not target.is_file():
                sections[index] = f"[{index}] {path}:{start}-{end} ERROR: file not found"
                continue
            pending_by_file.setdefault(target, []).append(
                (index, path, start, end, requested_end)
            )

        for target, requests in pending_by_file.items():
            contents = _read_text_ranges_bounded(
                target,
                path=requests[0][1],
                ranges=[(index, start, end) for index, _path, start, end, _requested in requests],
                max_output_bytes=per_range_limit,
            )
            for index, path, start, end, requested_end in requests:
                header = f"[{index}] {path}:{start}-{end}"
                if requested_end != end:
                    header += (
                        f" <clipped from requested end_line {requested_end}; max "
                        f"{_BATCH_READ_MAX_LINES} lines/range>"
                    )
                sections[index] = f"{header}\n{contents[index]}"

        remaining = max(0, len(ranges) - len(selected))
        rendered = [sections[index] for index in range(1, len(selected) + 1)]
        if remaining:
            rendered.append(
                f"<batch_read limit: processed the first {_BATCH_READ_MAX_RANGES} of "
                f"{len(ranges)} requested ranges. Request the remaining {remaining} range(s) in a "
                "separate batch_read only if they are still needed.>"
            )
        return _truncate_text_bytes("\n\n".join(rendered), total_limit)

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
        raw_lines = [
            line for line in text.splitlines() if line != "<tool output truncated by byte limit>"
        ][:max_results]
        if not raw_lines:
            return "<no matches>"

        parsed = [_parse_grep_match(line) for line in raw_lines]
        match_lines = ["Matches:"]
        for rank, (raw, match) in enumerate(zip(raw_lines, parsed, strict=True)):
            if match is None:
                match_lines.append(_truncate_chars(raw, _SEARCH_MATCH_PREVIEW_CHARS))
                continue
            match_path, line_number, content = match
            if rank < _SEARCH_CONTEXT_MATCH_LIMIT:
                match_lines.append(
                    f"{match_path}:{line_number}:"
                    f"{_truncate_chars(content, _SEARCH_MATCH_PREVIEW_CHARS)}"
                )
            else:
                match_lines.append(f"{match_path}:{line_number}")

        context_matches = [
            item for item in parsed[:_SEARCH_CONTEXT_MATCH_LIMIT] if item is not None
        ]
        windows = _merge_context_windows(context_matches)
        context_sections: dict[int, str] = {}
        pending_context: dict[Path, list[tuple[int, str, int, int, tuple[int, ...]]]] = {}
        for index, window in enumerate(windows):
            context_path, start, end, match_numbers = window
            try:
                target = self._resolve(context_path)
            except ValueError:
                continue
            if not target.is_file():
                continue
            pending_context.setdefault(target, []).append(
                (index, context_path, start, end, match_numbers)
            )

        for target, requests in pending_context.items():
            contents = _read_text_ranges_bounded(
                target,
                path=requests[0][1],
                ranges=[(index, start, end) for index, _path, start, end, _matches in requests],
                max_output_bytes=_SEARCH_CONTEXT_WINDOW_BYTES,
                max_line_bytes=_SEARCH_CONTEXT_LINE_BYTES,
            )
            for index, context_path, start, end, match_numbers in requests:
                matched = ", ".join(str(number) for number in match_numbers)
                context_sections[index] = (
                    f"--- {context_path}:{start}-{end} (match line(s): {matched}) ---\n"
                    f"{contents[index]}"
                )

        match_block = _truncate_text_bytes("\n".join(match_lines), _SEARCH_MATCH_LIST_BYTES)
        output = [match_block]
        if context_sections:
            output.extend(
                [
                    "",
                    (
                        "Context for the top six matches (±12 lines; overlapping windows merged):"
                    ),
                    *(context_sections[index] for index in sorted(context_sections)),
                ]
            )
        if len(raw_lines) > _SEARCH_CONTEXT_MATCH_LIMIT:
            output.extend(
                [
                    "",
                    (
                        f"<context shown only for the top {_SEARCH_CONTEXT_MATCH_LIMIT} matches; "
                        "remaining matches above are location-only. Use batch_read for up to six "
                        "high-priority ranges if deeper inspection is needed.>"
                    ),
                ]
            )
        if truncated_by_bytes:
            output.append("<tool output truncated by byte limit>")
        output_limit = min(self.settings.max_tool_output_bytes, _SEARCH_OUTPUT_BYTES)
        return _truncate_text_bytes("\n".join(output), output_limit)

    def operation_count(self, name: str, arguments: dict[str, Any]) -> int:
        """Return attempted repository operations for audit only, not budget charging."""

        if name != "batch_read":
            return 1
        ranges = arguments.get("ranges")
        return min(len(ranges), _BATCH_READ_MAX_RANGES) if isinstance(ranges, list) else 0

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


def _positive_int(value: Any) -> int | None:
    if type(value) is not int or value < 1:
        return None
    return value


def _truncate_chars(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + "…"


def _parse_grep_match(value: str) -> tuple[str, int, str] | None:
    path, separator, remainder = value.partition(":")
    if not separator:
        return None
    line_text, separator, content = remainder.partition(":")
    if not separator:
        return None
    try:
        line_number = int(line_text)
    except ValueError:
        return None
    if line_number < 1 or not path:
        return None
    return path, line_number, content


def _merge_context_windows(
    matches: list[tuple[str, int, str]],
) -> list[tuple[str, int, int, tuple[int, ...]]]:
    by_path: dict[str, list[tuple[int, int, int, int]]] = {}
    for rank, (path, line_number, _content) in enumerate(matches):
        by_path.setdefault(path, []).append(
            (
                max(1, line_number - _SEARCH_CONTEXT_RADIUS),
                line_number + _SEARCH_CONTEXT_RADIUS,
                rank,
                line_number,
            )
        )

    merged: list[tuple[int, str, int, int, tuple[int, ...]]] = []
    for path, windows in by_path.items():
        windows.sort(key=lambda item: (item[0], item[1], item[2]))
        current_start, current_end, current_rank, current_match = windows[0]
        ranks = [current_rank]
        match_numbers = [current_match]
        for start, end, rank, match_number in windows[1:]:
            if start <= current_end:
                current_end = max(current_end, end)
                ranks.append(rank)
                match_numbers.append(match_number)
                continue
            merged.append(
                (
                    min(ranks),
                    path,
                    current_start,
                    current_end,
                    tuple(match_numbers),
                )
            )
            current_start, current_end = start, end
            ranks = [rank]
            match_numbers = [match_number]
        merged.append(
            (min(ranks), path, current_start, current_end, tuple(match_numbers))
        )
    merged.sort(key=lambda item: item[0])
    return [(path, start, end, numbers) for _, path, start, end, numbers in merged]


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

    return _read_text_ranges_bounded(
        target,
        path=path,
        ranges=[(0, start_line, end_line)],
        max_output_bytes=max_output_bytes,
    )[0]


def _read_text_ranges_bounded(
    target: Path,
    *,
    path: str,
    ranges: list[tuple[int, int, int]],
    max_output_bytes: int,
    max_line_bytes: int | None = None,
) -> dict[int, str]:
    """Read several line ranges from one file in one pass, with an independent cap per range."""

    states: dict[int, dict[str, Any]] = {}
    valid_ranges: list[tuple[int, int, int]] = []
    for key, start_line, end_line in ranges:
        if end_line < start_line:
            states[key] = {"result": "<empty>"}
            continue
        states[key] = {"parts": [], "used": 0, "result": None, "done": False}
        valid_ranges.append((key, start_line, end_line))
    if not valid_ranges:
        return {key: str(state["result"]) for key, state in states.items()}

    min_start = min(start for _key, start, _end in valid_ranges)
    max_end = max(end for _key, _start, end in valid_ranges)
    capture_bytes = max_line_bytes or max_output_bytes
    try:
        with target.open("rb") as handle:
            for line_number, raw_line, physical_truncated in _iter_bounded_binary_lines(
                handle,
                capture_from_line=min_start,
                stop_after_line=max_end,
                capture_bytes=capture_bytes,
            ):
                if line_number < min_start:
                    continue
                active = [
                    key
                    for key, start, end in valid_ranges
                    if start <= line_number <= end and not states[key]["done"]
                ]
                if not active:
                    continue
                raw_line = raw_line.rstrip(b"\r\n")
                if b"\x00" in raw_line:
                    error = json.dumps({"error": "binary/non-UTF8 file", "path": path})
                    for key in active:
                        states[key]["result"] = error
                        states[key]["done"] = True
                    if all(
                        states[key]["done"] or line_number >= end
                        for key, _start, end in valid_ranges
                    ):
                        break
                    continue
                try:
                    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
                    text = decoder.decode(raw_line, final=not physical_truncated)
                except UnicodeDecodeError:
                    error = json.dumps({"error": "binary/non-UTF8 file", "path": path})
                    for key in active:
                        states[key]["result"] = error
                        states[key]["done"] = True
                    if all(
                        states[key]["done"] or line_number >= end
                        for key, _start, end in valid_ranges
                    ):
                        break
                    continue
                if physical_truncated:
                    text += "…"
                if max_line_bytes is not None:
                    text = _truncate_inline_utf8(text, max_line_bytes)

                rendered = f"{line_number}: {text}"
                rendered_bytes = len(rendered.encode("utf-8"))
                for key in active:
                    state = states[key]
                    parts = state["parts"]
                    separator_bytes = 1 if parts else 0
                    if state["used"] + separator_bytes + rendered_bytes > max_output_bytes:
                        candidate = "\n".join([*parts, rendered])
                        state["result"] = _truncate_text_bytes(candidate, max_output_bytes)
                        state["done"] = True
                        continue
                    parts.append(rendered)
                    state["used"] += separator_bytes + rendered_bytes
                if all(
                    states[key]["done"] or line_number >= end
                    for key, _start, end in valid_ranges
                ):
                    break
    except OSError as exc:
        error = json.dumps({"error": str(exc), "path": path})
        return {key: error for key in states}

    results: dict[int, str] = {}
    for key, state in states.items():
        if state.get("result") is not None:
            results[key] = str(state["result"])
            continue
        parts = state["parts"]
        results[key] = "\n".join(parts) if parts else "<empty>"
    return results


def _iter_bounded_binary_lines(
    handle: Any,
    *,
    capture_from_line: int,
    stop_after_line: int,
    capture_bytes: int,
):
    """Yield physical lines without ever materializing more than a bounded prefix of one line."""

    line_number = 1
    kept = bytearray()
    truncated = False
    saw_segment = False
    while line_number <= stop_after_line:
        segment = handle.readline(_FILE_READ_CHUNK_BYTES)
        if not segment:
            if saw_segment:
                yield line_number, bytes(kept).rstrip(b"\r\n"), truncated
            return
        saw_segment = True
        if line_number >= capture_from_line:
            remaining = capture_bytes - len(kept)
            if remaining > 0:
                kept.extend(segment[:remaining])
            if len(segment) > max(remaining, 0):
                truncated = True
        if not segment.endswith(b"\n"):
            continue
        yield line_number, bytes(kept).rstrip(b"\r\n"), truncated
        line_number += 1
        kept.clear()
        truncated = False
        saw_segment = False


def _truncate_inline_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    marker = "…"
    marker_bytes = marker.encode("utf-8")
    keep = max(0, limit - len(marker_bytes))
    return encoded[:keep].decode("utf-8", errors="ignore") + marker
