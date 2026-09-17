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
    bad_query = await executor.execute("search_text", {"query": 123})
    bad_list_path = await executor.execute("list_files", {"path": ["nope"]})
    bad_end_line = await executor.execute("read_file", {"path": "missing", "end_line": [1]})

    assert "requires non-empty string path" in missing_path
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
    tmp_path: Path,
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

    result = executor.read_file("registers.csv", start_line=1, end_line=100_000)

    assert result.endswith("<tool output truncated by byte limit>")
    assert "binary/non-UTF8" not in result
    assert len(result.encode("utf-8")) <= 4096


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
