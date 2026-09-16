from __future__ import annotations

import re
from dataclasses import dataclass

_DIFF_HEADER = re.compile(r"(?m)^diff --git ")
_NEW_PATH = re.compile(r"(?m)^\+\+\+ b/(.+)$")
_GIT_PATH = re.compile(r"^diff --git a/(.+?) b/(.+)$")


@dataclass(frozen=True, slots=True)
class DiffChunk:
    text: str
    paths: tuple[str, ...]


def chunk_diff(diff: str, *, max_chars: int) -> list[DiffChunk]:
    """Split a git diff without dropping bytes, preferring whole-file boundaries."""
    if not diff:
        return [DiffChunk(text="", paths=())]
    if len(diff) <= max_chars:
        return [DiffChunk(text=diff, paths=_paths(diff))]

    starts = [match.start() for match in _DIFF_HEADER.finditer(diff)]
    if not starts:
        return [DiffChunk(text=part, paths=()) for part in _split_text(diff, max_chars)]
    if starts[0] != 0:
        starts.insert(0, 0)
    starts.append(len(diff))

    sections = [diff[starts[index] : starts[index + 1]] for index in range(len(starts) - 1)]
    chunks: list[DiffChunk] = []
    pending = ""
    pending_paths: list[str] = []

    def flush() -> None:
        nonlocal pending, pending_paths
        if pending:
            chunks.append(DiffChunk(text=pending, paths=tuple(dict.fromkeys(pending_paths))))
            pending = ""
            pending_paths = []

    for section in sections:
        section_paths = list(_paths(section))
        if len(section) > max_chars:
            flush()
            chunks.extend(
                DiffChunk(text=part, paths=tuple(section_paths))
                for part in _split_text(section, max_chars)
            )
            continue
        if pending and len(pending) + len(section) > max_chars:
            flush()
        pending += section
        pending_paths.extend(section_paths)
    flush()
    return chunks


def _split_text(text: str, max_chars: int) -> list[str]:
    parts: list[str] = []
    offset = 0
    while offset < len(text):
        end = min(len(text), offset + max_chars)
        if end < len(text):
            newline = text.rfind("\n", offset + max_chars // 2, end)
            if newline > offset:
                end = newline + 1
        parts.append(text[offset:end])
        offset = end
    return parts


def _paths(section: str) -> tuple[str, ...]:
    paths = [
        match.group(1) for match in _NEW_PATH.finditer(section) if match.group(1) != "/dev/null"
    ]
    if paths:
        return tuple(dict.fromkeys(paths))
    first = section.splitlines()[0] if section else ""
    match = _GIT_PATH.match(first)
    if match:
        return (match.group(2),)
    return ()
