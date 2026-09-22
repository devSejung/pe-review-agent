import asyncio
import subprocess
from pathlib import Path

import pytest

from pe_review_agent.config import GerritSettings, RepoSettings, ReviewSettings
from pe_review_agent.domain import DiffSide
from pe_review_agent.repos.manager import (
    RepositoryManager,
    _first_parent,
    _git_review_pathspecs,
    _parents,
    _parse_changed_lines,
    _path_is_excluded,
    _project_storage_name,
)
from pe_review_agent.repos.tools import RepositoryToolExecutor
from pe_review_agent.retry import PermanentError


def test_parse_changed_lines_tracks_revision_and_parent_sides() -> None:
    diff = """\
diff --git a/fw/a.c b/fw/a.c
--- a/fw/a.c
+++ b/fw/a.c
@@ -2,2 +2,3 @@
 same
-old
+new
+extra
"""
    lines = _parse_changed_lines(diff)
    assert [(line.path, line.side, line.line, line.text) for line in lines] == [
        ("fw/a.c", DiffSide.PARENT, 3, "old"),
        ("fw/a.c", DiffSide.REVISION, 3, "new"),
        ("fw/a.c", DiffSide.REVISION, 4, "extra"),
    ]


def test_parse_changed_lines_supports_deleted_file_parent_anchors() -> None:
    diff = """\
diff --git a/fw/obsolete.c b/fw/obsolete.c
deleted file mode 100644
--- a/fw/obsolete.c
+++ /dev/null
@@ -4,2 +0,0 @@
-disable_timeout();
-advance();
"""

    lines = _parse_changed_lines(diff)

    assert [(line.path, line.side, line.line, line.text) for line in lines] == [
        ("fw/obsolete.c", DiffSide.PARENT, 4, "disable_timeout();"),
        ("fw/obsolete.c", DiffSide.PARENT, 5, "advance();"),
    ]


def test_repository_tools_block_path_escape(tmp_path: Path) -> None:
    executor = RepositoryToolExecutor(tmp_path, ReviewSettings())
    with pytest.raises(ValueError, match="escapes workspace"):
        executor.read_file("../secret")


def test_repository_read_file_is_line_numbered(tmp_path: Path) -> None:
    target = tmp_path / "fw.c"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    executor = RepositoryToolExecutor(tmp_path, ReviewSettings())
    assert executor.read_file("fw.c", start_line=2, end_line=3) == "2: two\n3: three"


def test_project_storage_name_prevents_sanitization_collision() -> None:
    assert _project_storage_name("team/a/b") != _project_storage_name("team/a_b")
    assert _project_storage_name("team/a/b").startswith("team_a_b-")


def test_generated_path_patterns_filter_nested_files() -> None:
    assert _path_is_excluded("out/training/result.bin", ["*.bin"])
    assert _path_is_excluded("vectors/generated.hex", ["*.hex"])
    assert not _path_is_excluded("src/training.c", ["*.bin", "*.hex"])
    assert _git_review_pathspecs(["*.bin", "generated/**"]) == (
        ".",
        ":(exclude,glob)**/*.bin",
        ":(exclude,glob)generated/**",
    )


def test_first_parent_handles_normal_and_root_commits() -> None:
    assert _first_parent("a" * 40 + " " + "b" * 40) == "b" * 40
    assert _first_parent("a" * 40) is None
    assert _parents("a" * 40 + " " + "b" * 40 + " " + "c" * 40) == (
        "b" * 40,
        "c" * 40,
    )


@pytest.mark.asyncio
async def test_repository_probe_read_access_uses_git_remote_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    await asyncio.to_thread(subprocess.run, ["git", "init", "-q", str(source)], check=True)
    await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(source), "config", "user.email", "bot@example.com"],
        check=True,
    )
    await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(source), "config", "user.name", "Bot"],
        check=True,
    )
    (source / "fw.c").write_text("int probe;\n", encoding="utf-8")
    await asyncio.to_thread(subprocess.run, ["git", "-C", str(source), "add", "."], check=True)
    await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(source), "commit", "-q", "-m", "probe"],
        check=True,
    )
    expected = (
        await asyncio.to_thread(
            subprocess.check_output,
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            text=True,
        )
    ).strip()

    manager = RepositoryManager(
        RepoSettings(cache_root=tmp_path / "cache", work_root=tmp_path / "work"),
        GerritSettings(
            ssh_host="gerrit",
            ssh_user="bot",
            ssh_key_path=tmp_path / "key",
            rest_url="https://gerrit",
            projects=["team/fw"],
        ),
    )
    monkeypatch.setattr(manager, "_ssh_url", lambda _project: str(source))

    assert await manager.probe_read_access("team/fw") == expected


@pytest.mark.asyncio
async def test_malformed_tool_arguments_return_error_instead_of_raising(tmp_path: Path) -> None:
    executor = RepositoryToolExecutor(tmp_path, ReviewSettings())

    missing_path = await executor.execute("read_file", {})
    missing_ranges = await executor.execute("batch_read", {})
    bad_query = await executor.execute("search_text", {"query": 123})
    bad_list_path = await executor.execute("list_files", {"path": ["nope"]})
    bad_end_line = await executor.execute("read_file", {"path": "missing", "end_line": [1]})

    assert "requires non-empty string path" in missing_path
    assert "requires a non-empty ranges array" in missing_ranges
    assert "requires non-empty string query" in bad_query
    assert "path must be a string" in bad_list_path
    assert "end_line must be an integer" in bad_end_line


@pytest.mark.asyncio
async def test_repository_tool_output_is_byte_bounded(tmp_path: Path) -> None:
    await asyncio.to_thread(subprocess.run, ["git", "init", "-q", str(tmp_path)], check=True)
    target = tmp_path / "many.txt"
    target.write_text("\n".join(f"needle-{index}" for index in range(20_000)), encoding="utf-8")
    await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(tmp_path), "add", "many.txt"],
        check=True,
    )
    settings = ReviewSettings(max_tool_output_bytes=4096)
    executor = RepositoryToolExecutor(tmp_path, settings)

    result = await executor.search_text("needle")

    assert "<tool output truncated by byte limit>" in result
    assert len(result.encode("utf-8")) < 5000


@pytest.mark.asyncio
async def test_search_text_adds_plus_minus_twelve_lines_only_for_top_six_matches(
    tmp_path: Path,
) -> None:
    await asyncio.to_thread(subprocess.run, ["git", "init", "-q", str(tmp_path)], check=True)
    lines = [f"filler-{index}" for index in range(1, 301)]
    for ordinal, line_number in enumerate((20, 60, 100, 140, 180, 220, 260), start=1):
        lines[line_number - 1] = f"needle-{ordinal}"
    (tmp_path / "fw.c").write_text("\n".join(lines) + "\n", encoding="utf-8")
    await asyncio.to_thread(
        subprocess.run, ["git", "-C", str(tmp_path), "add", "fw.c"], check=True
    )
    executor = RepositoryToolExecutor(tmp_path, ReviewSettings(max_tool_output_bytes=256_000))

    result = await executor.search_text("needle", max_results=40)

    assert "fw.c:20:needle-1" in result
    assert "fw.c:260" in result
    assert "fw.c:260:needle-7" not in result
    assert "--- fw.c:8-32 (match line(s): 20) ---" in result
    assert "8: filler-8" in result
    assert "32: filler-32" in result
    assert result.count("--- fw.c:") == 6
    assert "--- fw.c:248-272" not in result
    assert "248: filler-248" not in result
    assert "context shown only for the top 6 matches" in result
    assert len(result.encode("utf-8")) <= 32 * 1024


@pytest.mark.asyncio
async def test_search_text_merges_overlapping_top_match_context_windows(tmp_path: Path) -> None:
    await asyncio.to_thread(subprocess.run, ["git", "init", "-q", str(tmp_path)], check=True)
    lines = [f"line-{index}-" + "x" * 80 for index in range(1, 180)]
    lines[99] = "needle-first-" + "a" * 80
    lines[119] = "needle-second-" + "b" * 80
    lines[139] = "needle-third-" + "c" * 80
    (tmp_path / "fw.c").write_text("\n".join(lines) + "\n", encoding="utf-8")
    await asyncio.to_thread(
        subprocess.run, ["git", "-C", str(tmp_path), "add", "fw.c"], check=True
    )
    executor = RepositoryToolExecutor(tmp_path, ReviewSettings())

    result = await executor.search_text("needle")

    assert "--- fw.c:88-152 (match line(s): 100, 120, 140) ---" in result
    assert result.count("--- fw.c:") == 1
    assert "88: line-88-" in result
    assert "100: needle-first-" in result
    assert "120: needle-second-" in result
    assert "140: needle-third-" in result
    assert "152: line-152-" in result


def test_batch_read_processes_only_first_six_ranges_and_returns_followup_hint(
    tmp_path: Path,
) -> None:
    for index in range(1, 9):
        (tmp_path / f"file{index}.c").write_text(
            "\n".join(f"f{index}-{line}" for line in range(1, 10)) + "\n",
            encoding="utf-8",
        )
    executor = RepositoryToolExecutor(tmp_path, ReviewSettings())
    ranges = [
        {"path": f"file{index}.c", "start_line": 2, "end_line": 4}
        for index in range(1, 9)
    ]

    result = executor.batch_read(ranges)
    batch_schema = next(
        item for item in executor.tool_schemas if item["function"]["name"] == "batch_read"
    )

    for index in range(1, 7):
        assert f"[{index}] file{index}.c:2-4" in result
        assert f"2: f{index}-2" in result
    assert "file7.c" not in result
    assert "file8.c" not in result
    assert "processed the first 6 of 8 requested ranges" in result
    assert "remaining 2 range(s) in a separate batch_read" in result
    assert executor.operation_count("batch_read", {"ranges": ranges}) == 6
    assert batch_schema["function"]["parameters"]["properties"]["ranges"]["maxItems"] == 6


def test_batch_read_caps_each_range_at_two_hundred_lines_and_keeps_partial_errors(
    tmp_path: Path,
) -> None:
    (tmp_path / "good.c").write_text(
        "\n".join(f"line-{index}" for index in range(1, 301)) + "\n",
        encoding="utf-8",
    )
    executor = RepositoryToolExecutor(tmp_path, ReviewSettings(max_tool_output_bytes=256_000))

    result = executor.batch_read(
        [
            {"path": "good.c", "start_line": 1, "end_line": 250},
            {"path": "missing.c", "start_line": 1, "end_line": 10},
            {"path": "../escape.c", "start_line": 1, "end_line": 10},
        ]
    )

    assert "[1] good.c:1-200 <clipped from requested end_line 250; max 200 lines/range>" in result
    assert "200: line-200" in result
    assert "201: line-201" not in result
    assert "[2] missing.c:1-10 ERROR: file not found" in result
    assert "[3] ../escape.c:1-10 ERROR:" in result
    assert "escapes workspace" in result
    assert len(result.encode("utf-8")) <= 64 * 1024


def test_batch_read_single_range_can_use_available_batch_bytes_for_all_two_hundred_lines(
    tmp_path: Path,
) -> None:
    target = tmp_path / "wide.c"
    target.write_text(
        "\n".join(f"line-{index}-" + "x" * 90 for index in range(1, 201)) + "\n",
        encoding="utf-8",
    )
    executor = RepositoryToolExecutor(tmp_path, ReviewSettings(max_tool_output_bytes=256_000))

    result = executor.batch_read(
        [{"path": "wide.c", "start_line": 1, "end_line": 200}]
    )

    assert "1: line-1-" in result
    assert "200: line-200-" in result
    assert "<tool output truncated by byte limit>" not in result
    assert len(result.encode("utf-8")) <= 64 * 1024


def test_batch_read_multiple_ranges_from_same_large_file_use_one_file_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "large.c"
    target.write_text(
        "\n".join(f"line-{index}" for index in range(1, 20_001)) + "\n",
        encoding="utf-8",
    )
    executor = RepositoryToolExecutor(tmp_path, ReviewSettings())
    original_open = Path.open
    opens = 0

    def tracking_open(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal opens
        if path == target and args and args[0] == "rb":
            opens += 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracking_open)
    ranges = [
        {"path": "large.c", "start_line": start, "end_line": start + 10}
        for start in (15_000, 16_000, 17_000, 18_000, 19_000, 19_500)
    ]

    result = executor.batch_read(ranges)

    assert opens == 1
    assert "15000: line-15000" in result
    assert "19500: line-19500" in result


def test_repository_read_file_obeys_tool_output_byte_limit(tmp_path: Path) -> None:
    target = tmp_path / "many.txt"
    target.write_text("\n".join(f"long-line-{index}" for index in range(5_000)), encoding="utf-8")
    executor = RepositoryToolExecutor(tmp_path, ReviewSettings(max_tool_output_bytes=4096))

    result = executor.read_file("many.txt", start_line=1, end_line=5_000)

    assert result.endswith("<tool output truncated by byte limit>")
    assert len(result.encode("utf-8")) <= 4096


def test_repository_read_file_streams_range_from_large_file(tmp_path: Path) -> None:
    target = tmp_path / "LPDDR56_PHY.csv"
    target.write_text(
        "\n".join(f"REG_{index},0x{index:08x},FIELD_{index}" for index in range(40_000)),
        encoding="utf-8",
    )
    assert target.stat().st_size > 256_000
    executor = RepositoryToolExecutor(
        tmp_path,
        ReviewSettings(max_context_file_bytes=4096, max_tool_output_bytes=4096),
    )

    result = executor.read_file("LPDDR56_PHY.csv", start_line=20_001, end_line=20_003)

    assert result.splitlines() == [
        "20001: REG_20000,0x00004e20,FIELD_20000",
        "20002: REG_20001,0x00004e21,FIELD_20001",
        "20003: REG_20002,0x00004e22,FIELD_20002",
    ]


def test_repository_read_file_stops_at_output_limit_before_rest_of_large_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "registers.csv"
    with target.open("wb") as handle:
        for index in range(20_000):
            handle.write(f"REG_{index},".encode() + b"x" * 200 + b"\n")
        # If read_file unnecessarily scans/decodes the whole file after the output limit is reached,
        # this invalid UTF-8 tail would turn a valid bounded read into an error.
        handle.write(b"\xff\xfe\x00\n")

    executor = RepositoryToolExecutor(
        tmp_path,
        ReviewSettings(max_context_file_bytes=1024, max_tool_output_bytes=4096),
    )
    original_open = Path.open
    lines_read = 0

    class CountingReader:
        def __init__(self, handle):  # type: ignore[no-untyped-def]
            self.handle = handle

        def __enter__(self):  # type: ignore[no-untyped-def]
            self.handle.__enter__()
            return self

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            return self.handle.__exit__(*args)

        def readline(self, size=-1):  # type: ignore[no-untyped-def]
            nonlocal lines_read
            value = self.handle.readline(size)
            if value.endswith(b"\n"):
                lines_read += 1
            return value

    def tracking_open(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        handle = original_open(path, *args, **kwargs)
        if path == target and args and args[0] == "rb":
            return CountingReader(handle)
        return handle

    monkeypatch.setattr(Path, "open", tracking_open)

    result = executor.read_file("registers.csv", start_line=1, end_line=100_000)

    assert result.endswith("<tool output truncated by byte limit>")
    assert "binary/non-UTF8" not in result
    assert len(result.encode("utf-8")) <= 4096
    assert lines_read < 100


def test_repository_read_file_drains_huge_physical_lines_with_bounded_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "minified.js"
    target.write_bytes(b"x" * (2 * 1024 * 1024) + b"\nneedle\n")
    executor = RepositoryToolExecutor(tmp_path, ReviewSettings(max_tool_output_bytes=4096))
    original_open = Path.open
    max_requested_read = 0

    class BoundedReader:
        def __init__(self, handle):  # type: ignore[no-untyped-def]
            self.handle = handle

        def __enter__(self):  # type: ignore[no-untyped-def]
            self.handle.__enter__()
            return self

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            return self.handle.__exit__(*args)

        def __iter__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("unbounded file iteration is not allowed")

        def readline(self, size=-1):  # type: ignore[no-untyped-def]
            nonlocal max_requested_read
            assert 0 < size <= 64 * 1024
            max_requested_read = max(max_requested_read, size)
            return self.handle.readline(size)

    def tracking_open(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        handle = original_open(path, *args, **kwargs)
        if path == target and args and args[0] == "rb":
            return BoundedReader(handle)
        return handle

    monkeypatch.setattr(Path, "open", tracking_open)

    result = executor.read_file("minified.js", start_line=2, end_line=2)

    assert result == "2: needle"
    assert max_requested_read == 64 * 1024


@pytest.mark.asyncio
async def test_git_diff_capture_uses_host_safety_ceiling(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    await asyncio.to_thread(subprocess.run, ["git", "init", "-q", str(repo)], check=True)
    await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(repo), "config", "user.email", "bot@example.com"],
        check=True,
    )
    await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(repo), "config", "user.name", "Bot"],
        check=True,
    )
    (repo / "large.c").write_text("int x;\n" * 10_000, encoding="utf-8")
    await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(repo), "add", "large.c"],
        check=True,
    )
    await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(repo), "commit", "-q", "-m", "large"],
        check=True,
    )

    manager = RepositoryManager(
        RepoSettings(
            cache_root=tmp_path / "cache",
            work_root=tmp_path / "work",
            max_diff_bytes=4096,
        ),
        GerritSettings(
            ssh_host="gerrit",
            ssh_user="bot",
            ssh_key_path=tmp_path / "key",
            rest_url="https://gerrit",
            projects=["team/fw"],
        ),
    )

    with pytest.raises(PermanentError, match="host-safety ceiling"):
        await manager._git_bounded_stdout(  # noqa: SLF001
            "-C",
            str(repo),
            "show",
            "HEAD",
            "--format=",
            max_stdout_bytes=4096,
        )


@pytest.mark.asyncio
async def test_workspace_turns_oversize_diff_into_visible_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "source"
    await asyncio.to_thread(subprocess.run, ["git", "init", "-q", str(repo)], check=True)
    await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(repo), "config", "user.email", "bot@example.com"],
        check=True,
    )
    await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(repo), "config", "user.name", "Bot"],
        check=True,
    )
    target = repo / "large.c"
    target.write_text("int x = 0;\n", encoding="utf-8")
    await asyncio.to_thread(subprocess.run, ["git", "-C", str(repo), "add", "."], check=True)
    await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(repo), "commit", "-q", "-m", "base"],
        check=True,
    )
    target.write_text(
        "\n".join(f"int x_{index} = {index};" for index in range(2000)),
        encoding="utf-8",
    )
    await asyncio.to_thread(subprocess.run, ["git", "-C", str(repo), "add", "."], check=True)
    await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(repo), "commit", "-q", "-m", "large change"],
        check=True,
    )
    revision = (
        await asyncio.to_thread(
            subprocess.check_output,
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            text=True,
        )
    ).strip()

    manager = RepositoryManager(
        RepoSettings(
            cache_root=tmp_path / "cache",
            work_root=tmp_path / "work",
            max_diff_bytes=4096,
        ),
        GerritSettings(
            ssh_host="gerrit",
            ssh_user="bot",
            ssh_key_path=tmp_path / "key",
            rest_url="https://gerrit",
            projects=["team/fw"],
        ),
    )

    async def local_revision(**_kwargs) -> Path:
        return repo

    monkeypatch.setattr(manager, "ensure_revision", local_revision)

    async with manager.workspace(
        project="team/fw",
        change_number=123,
        revision_sha=revision,
        ref=None,
    ) as workspace:
        assert workspace.diff == ""
        assert workspace.changed_files == []
        assert workspace.skip_reason is not None
        assert "exceeds the configured repository diff safety ceiling" in workspace.skip_reason


@pytest.mark.asyncio
async def test_ensure_revision_rebuilds_structurally_invalid_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    await asyncio.to_thread(subprocess.run, ["git", "init", "-q", str(source)], check=True)
    await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(source), "config", "user.email", "bot@example.com"],
        check=True,
    )
    await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(source), "config", "user.name", "Bot"],
        check=True,
    )
    (source / "fw.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    await asyncio.to_thread(subprocess.run, ["git", "-C", str(source), "add", "."], check=True)
    await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(source), "commit", "-q", "-m", "base"],
        check=True,
    )
    revision = (
        await asyncio.to_thread(
            subprocess.check_output,
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            text=True,
        )
    ).strip()

    manager = RepositoryManager(
        RepoSettings(cache_root=tmp_path / "cache", work_root=tmp_path / "work"),
        GerritSettings(
            ssh_host="gerrit",
            ssh_user="bot",
            ssh_key_path=tmp_path / "key",
            rest_url="https://gerrit",
            projects=["team/fw"],
        ),
    )
    monkeypatch.setattr(manager, "_ssh_url", lambda _project: str(source))
    mirror = manager._mirror_path("team/fw")  # noqa: SLF001
    mirror.mkdir(parents=True)
    (mirror / "partial-clone-marker").write_text("crashed", encoding="utf-8")

    resolved_mirror = await manager.ensure_revision(
        project="team/fw",
        revision_sha=revision,
        ref=revision,
    )

    assert resolved_mirror == mirror
    assert not (mirror / "partial-clone-marker").exists()
    is_bare = await asyncio.to_thread(
        subprocess.check_output,
        ["git", "-C", str(mirror), "rev-parse", "--is-bare-repository"],
        text=True,
    )
    assert is_bare.strip() == "true"

    # A hard stop during fetch can leave the destination ref lock behind even though the mirror is
    # otherwise structurally valid. The next fetch must rebuild cache state instead of exhausting
    # the job retry budget against the same stale lock.
    (source / "fw.c").write_text("int main(void) { return 1; }\n", encoding="utf-8")
    await asyncio.to_thread(subprocess.run, ["git", "-C", str(source), "add", "."], check=True)
    await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(source), "commit", "-q", "-m", "second"],
        check=True,
    )
    next_revision = (
        await asyncio.to_thread(
            subprocess.check_output,
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            text=True,
        )
    ).strip()

    cache_marker = mirror / "cache-marker"
    cache_marker.write_text("before stale lock rebuild", encoding="utf-8")
    stale_lock = mirror / "refs" / "pe-review" / f"{next_revision}.lock"
    stale_lock.parent.mkdir(parents=True, exist_ok=True)
    stale_lock.write_text("stale", encoding="utf-8")

    await manager.ensure_revision(
        project="team/fw",
        revision_sha=next_revision,
        ref=next_revision,
    )

    assert not cache_marker.exists()
    assert not stale_lock.exists()


@pytest.mark.asyncio
async def test_worker_runtime_cleans_crash_orphan_worktrees(tmp_path: Path) -> None:
    manager = RepositoryManager(
        RepoSettings(cache_root=tmp_path / "cache", work_root=tmp_path / "work"),
        GerritSettings(
            ssh_host="gerrit",
            ssh_user="bot",
            ssh_key_path=tmp_path / "key",
            rest_url="https://gerrit",
            projects=["team/fw"],
        ),
    )
    orphan = tmp_path / "work" / "team_fw" / "101" / "deadbeef" / "orphan"
    orphan.mkdir(parents=True)
    (orphan / "large.bin").write_bytes(b"x" * 4096)

    async with manager.worker_runtime():
        assert not orphan.exists()
        assert list((tmp_path / "work").iterdir()) == []


@pytest.mark.asyncio
async def test_worker_runtime_rejects_second_repository_owner(tmp_path: Path) -> None:
    settings = RepoSettings(cache_root=tmp_path / "cache", work_root=tmp_path / "work")
    gerrit = GerritSettings(
        ssh_host="gerrit",
        ssh_user="bot",
        ssh_key_path=tmp_path / "key",
        rest_url="https://gerrit",
        projects=["team/fw"],
    )
    first = RepositoryManager(settings, gerrit)
    second = RepositoryManager(settings, gerrit)

    async with first.worker_runtime():
        with pytest.raises(RuntimeError, match="already owned"):
            async with second.worker_runtime():
                pytest.fail("second worker unexpectedly acquired repository cache")
