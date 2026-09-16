from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
import re
import shutil
import stat
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

from pe_review_agent.config import GerritSettings, RepoSettings
from pe_review_agent.domain import ChangedLine, DiffSide, ReviewContext
from pe_review_agent.retry import PermanentError, TransientError


@dataclass(frozen=True, slots=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


class DiffOutputLimitExceeded(PermanentError):
    pass


@dataclass(slots=True)
class RepositoryWorkspace:
    project: str
    revision_sha: str
    base_revision_sha: str
    root: Path
    mirror: Path
    diff: str
    changed_files: list[str]
    changed_lines: list[ChangedLine]
    skip_reason: str | None = None


class RepositoryManager:
    """Maintains per-project mirrors and creates immutable per-job worktrees."""

    def __init__(self, repo: RepoSettings, gerrit: GerritSettings) -> None:
        self.settings = repo
        self.gerrit = gerrit
        self._locks: dict[str, asyncio.Lock] = {}
        self.settings.cache_root.mkdir(parents=True, exist_ok=True)
        self.settings.work_root.mkdir(parents=True, exist_ok=True)

    def _project_lock(self, project: str) -> asyncio.Lock:
        return self._locks.setdefault(project, asyncio.Lock())

    def _mirror_path(self, project: str) -> Path:
        return self.settings.cache_root / f"{_project_storage_name(project)}.git"

    def _ssh_url(self, project: str) -> str:
        path = quote(project.lstrip("/"), safe="/")
        return (
            f"ssh://{quote(self.gerrit.ssh_user, safe='')}@{self.gerrit.ssh_host}:"
            f"{self.gerrit.ssh_port}/{path}"
        )

    def _ssh_command(self) -> str:
        parts = ["ssh", "-i", str(self.gerrit.ssh_key_path), "-p", str(self.gerrit.ssh_port)]
        if self.gerrit.known_hosts_path:
            parts.extend(["-o", f"UserKnownHostsFile={self.gerrit.known_hosts_path}"])
        parts.extend(
            [
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes"
                if self.gerrit.strict_host_key_checking
                else "StrictHostKeyChecking=no",
            ]
        )
        # Git parses GIT_SSH_COMMAND with shell-like splitting itself.
        return " ".join(_shell_quote(part) for part in parts)

    @asynccontextmanager
    async def worker_runtime(self) -> AsyncIterator[None]:
        """Own the repository volume for one worker process and clean crash leftovers on startup."""

        lock_path = self.settings.cache_root / ".worker.lock"
        handle = await asyncio.to_thread(_acquire_worker_lock, lock_path)
        try:
            await self._cleanup_crash_orphans()
            yield
        finally:
            await asyncio.to_thread(_release_worker_lock, handle)

    async def ensure_revision(
        self,
        *,
        project: str,
        revision_sha: str,
        ref: str | None,
    ) -> Path:
        mirror = self._mirror_path(project)
        async with self._project_lock(project):
            env = {**os.environ, "GIT_SSH_COMMAND": self._ssh_command()}
            await self._ensure_healthy_mirror(project, mirror, env)
            fetch_source = ref or revision_sha
            fetch_target = f"refs/pe-review/{revision_sha}"
            try:
                await self._fetch_revision(
                    mirror,
                    fetch_source=fetch_source,
                    fetch_target=fetch_target,
                    env=env,
                )
            except TransientError as exc:
                if not _looks_rebuildable_mirror_error(str(exc)):
                    raise
                # Mirror contents are cache-only. A hard crash can leave stale lock/corrupt state;
                # rebuild once from origin rather than burning every retry on the same local cache.
                await self._rebuild_mirror(project, mirror, env)
                await self._fetch_revision(
                    mirror,
                    fetch_source=fetch_source,
                    fetch_target=fetch_target,
                    env=env,
                )
            resolved = (
                await self._git("-C", str(mirror), "rev-parse", f"{fetch_target}^{{commit}}")
            ).stdout.strip()
            if resolved != revision_sha:
                detail = f"expected {revision_sha}, got {resolved}"
                raise PermanentError(f"fetched revision mismatch for {project}: {detail}")
        return mirror

    async def _ensure_healthy_mirror(
        self,
        project: str,
        mirror: Path,
        env: dict[str, str],
    ) -> None:
        expected_origin = self._ssh_url(project)
        mirror_exists = await asyncio.to_thread(mirror.exists)
        if mirror_exists:
            bare = await self._git(
                "-C",
                str(mirror),
                "rev-parse",
                "--is-bare-repository",
                check=False,
            )
            origin = await self._git(
                "-C",
                str(mirror),
                "remote",
                "get-url",
                "origin",
                check=False,
            )
            if (
                bare.returncode == 0
                and bare.stdout.strip() == "true"
                and origin.returncode == 0
                and origin.stdout.strip() == expected_origin
            ):
                return
            await asyncio.to_thread(_remove_path_if_present, mirror)
        await self._clone_mirror(project, mirror, env)

    async def _rebuild_mirror(
        self,
        project: str,
        mirror: Path,
        env: dict[str, str],
    ) -> None:
        await asyncio.to_thread(_remove_path_if_present, mirror)
        await self._clone_mirror(project, mirror, env)

    async def _clone_mirror(
        self,
        project: str,
        mirror: Path,
        env: dict[str, str],
    ) -> None:
        await self._git(
            "clone",
            "--mirror",
            self._ssh_url(project),
            str(mirror),
            env=env,
            transient=True,
        )

    async def _fetch_revision(
        self,
        mirror: Path,
        *,
        fetch_source: str,
        fetch_target: str,
        env: dict[str, str],
    ) -> None:
        await self._git(
            "-C",
            str(mirror),
            "fetch",
            "--no-tags",
            "--force",
            "origin",
            f"{fetch_source}:{fetch_target}",
            env=env,
            transient=True,
        )

    async def _cleanup_crash_orphans(self) -> None:
        """Clear worktrees left by a hard-stopped worker before accepting new jobs."""

        await asyncio.to_thread(_clear_owned_work_root, self.settings.work_root)
        for mirror in self.settings.cache_root.glob("*.git"):
            if not mirror.is_dir():
                continue
            bare = await self._git(
                "-C",
                str(mirror),
                "rev-parse",
                "--is-bare-repository",
                check=False,
            )
            if bare.returncode == 0 and bare.stdout.strip() == "true":
                await self._git("-C", str(mirror), "worktree", "prune", check=False)

    @asynccontextmanager
    async def workspace(
        self,
        *,
        project: str,
        change_number: int,
        revision_sha: str,
        ref: str | None,
        exclude_patterns: Sequence[str] = (),
    ) -> AsyncIterator[RepositoryWorkspace]:
        mirror = await self.ensure_revision(project=project, revision_sha=revision_sha, ref=ref)
        parent_line = (
            await self._git("-C", str(mirror), "rev-list", "--parents", "-n", "1", revision_sha)
        ).stdout.strip()
        parents = _parents(parent_line)
        base_revision = parents[0] if parents else None
        merge_skip_reason = None
        if len(parents) > 1:
            merge_skip_reason = (
                "Automated review skipped for this merge commit. Gerrit 3.8 compares merge "
                "revisions against its auto-merge base, while a local first-parent diff can "
                "produce "
                "different changed lines and unsafe inline anchors. No AI findings or review vote "
                "were emitted for this Patch Set."
            )
        if base_revision is None:
            base_revision = (
                await self._git(
                    "-C",
                    str(mirror),
                    "hash-object",
                    "-t",
                    "tree",
                    "--stdin",
                    input_data=b"",
                )
            ).stdout.strip()
        root = (
            self.settings.work_root
            / _project_storage_name(project)
            / str(change_number)
            / revision_sha[:16]
            / uuid.uuid4().hex[:12]
        )
        root.parent.mkdir(parents=True, exist_ok=True)
        async with self._project_lock(project):
            await self._git(
                "-C",
                str(mirror),
                "worktree",
                "add",
                "--detach",
                str(root),
                revision_sha,
            )
        try:
            changed_files: list[str] = []
            review_pathspecs = _git_review_pathspecs(exclude_patterns)
            diff = ""
            zero_diff = ""
            size_skip_reason = None
            if merge_skip_reason is None:
                try:
                    changed_files_output = (
                        await self._git_bounded_stdout(
                            "-C",
                            str(root),
                            "diff",
                            "--name-only",
                            "--diff-filter=ACMRD",
                            base_revision,
                            revision_sha,
                            "--",
                            *review_pathspecs,
                            max_stdout_bytes=self.settings.max_diff_bytes,
                        )
                    ).stdout
                    changed_files = [
                        line
                        for line in changed_files_output.splitlines()
                        if line and not _path_is_excluded(line, exclude_patterns)
                    ]
                    if changed_files:
                        diff = (
                            await self._git_bounded_stdout(
                                "-C",
                                str(root),
                                "diff",
                                "--find-renames",
                                "--no-ext-diff",
                                "--unified=20",
                                base_revision,
                                revision_sha,
                                "--",
                                *review_pathspecs,
                                max_stdout_bytes=self.settings.max_diff_bytes,
                            )
                        ).stdout
                        zero_diff = (
                            await self._git_bounded_stdout(
                                "-C",
                                str(root),
                                "diff",
                                "--no-ext-diff",
                                "--unified=0",
                                base_revision,
                                revision_sha,
                                "--",
                                *review_pathspecs,
                                max_stdout_bytes=self.settings.max_diff_bytes,
                            )
                        ).stdout
                except DiffOutputLimitExceeded:
                    changed_files = []
                    diff = ""
                    zero_diff = ""
                    size_skip_reason = (
                        "Automated review skipped because this Patch Set exceeds the configured "
                        f"repository diff safety ceiling ({self.settings.max_diff_bytes} bytes). "
                        "No AI findings or review vote were emitted. Split the Change or raise "
                        "repos.max_diff_bytes after validating model and host capacity."
                    )
            yield RepositoryWorkspace(
                project=project,
                revision_sha=revision_sha,
                base_revision_sha=base_revision,
                root=root,
                mirror=mirror,
                diff=diff,
                changed_files=changed_files,
                changed_lines=_parse_changed_lines(zero_diff),
                skip_reason=merge_skip_reason or size_skip_reason,
            )
        finally:
            async with self._project_lock(project):
                await self._remove_worktree(mirror, root)

    def to_review_context(
        self,
        workspace: RepositoryWorkspace,
        *,
        change_number: int,
        patchset_number: int,
        subject: str | None,
        branch: str | None,
        policy_text: str,
    ) -> ReviewContext:
        return ReviewContext(
            project=workspace.project,
            change_number=change_number,
            patchset_number=patchset_number,
            revision_sha=workspace.revision_sha,
            base_revision_sha=workspace.base_revision_sha,
            subject=subject,
            branch=branch,
            diff=workspace.diff,
            changed_files=workspace.changed_files,
            changed_lines=workspace.changed_lines,
            policy_text=policy_text,
            repository_root=str(workspace.root),
            skip_reason=workspace.skip_reason,
        )

    async def read_text_at_revision(
        self,
        root: Path,
        revision_sha: str,
        relative_path: str,
        *,
        max_bytes: int,
    ) -> str | None:
        """Read a bounded Git blob without following filesystem symlinks from a worktree."""

        if not relative_path or relative_path.startswith(("/", "\\")):
            return None
        normalized = relative_path.replace("\\", "/")
        if any(part in {"", ".", ".."} for part in normalized.split("/")):
            return None
        object_name = f"{revision_sha}:{normalized}"
        size_result = await self._git(
            "-C",
            str(root),
            "cat-file",
            "-s",
            object_name,
            check=False,
        )
        if size_result.returncode != 0:
            return None
        try:
            size = int(size_result.stdout.strip())
        except ValueError:
            return None
        if size < 0 or size > max_bytes:
            return None
        try:
            result = await self._git_bounded_stdout(
                "-C",
                str(root),
                "show",
                object_name,
                max_stdout_bytes=max_bytes,
            )
        except PermanentError:
            return None
        return result.stdout

    async def _remove_worktree(self, mirror: Path, root: Path) -> None:
        try:
            await self._git(
                "-C", str(mirror), "worktree", "remove", "--force", str(root), check=False
            )
        finally:
            await asyncio.to_thread(_remove_directory_if_present, root)
            await self._git("-C", str(mirror), "worktree", "prune", check=False)

    async def _git(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        transient: bool = False,
        check: bool = True,
        input_data: bytes | None = None,
    ) -> CommandResult:
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE if input_data is not None else None,
                env=env,
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input_data), timeout=self.settings.command_timeout_seconds
            )
        except TimeoutError as exc:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise TransientError(f"git command timed out: {' '.join(args[:4])}") from exc
        except OSError as exc:
            raise TransientError(f"failed to execute git: {exc}") from exc
        result = CommandResult(
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            returncode=proc.returncode or 0,
        )
        if check and result.returncode != 0:
            message = (
                f"git {' '.join(args[:5])} failed ({result.returncode}): {result.stderr.strip()}"
            )
            if transient or _looks_transient_git_error(result.stderr):
                raise TransientError(message)
            raise PermanentError(message)
        return result

    async def _git_bounded_stdout(
        self,
        *args: str,
        max_stdout_bytes: int,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Run git with bounded in-memory capture while draining excess output from the pipe."""
        proc: asyncio.subprocess.Process | None = None
        stdout_task: asyncio.Task[tuple[bytes, bool]] | None = None
        stderr_task: asyncio.Task[tuple[bytes, bool]] | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout_task = asyncio.create_task(_read_stream_bounded(proc.stdout, max_stdout_bytes))
            stderr_task = asyncio.create_task(_read_stream_bounded(proc.stderr, 64_000))
            await asyncio.wait_for(proc.wait(), timeout=self.settings.command_timeout_seconds)
        except TimeoutError as exc:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            if stdout_task is not None and stderr_task is not None:
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise TransientError(f"git command timed out: {' '.join(args[:4])}") from exc
        except OSError as exc:
            raise TransientError(f"failed to execute git: {exc}") from exc

        assert proc is not None and stdout_task is not None and stderr_task is not None
        stdout_b, stdout_truncated = await stdout_task
        stderr_b, _ = await stderr_task
        returncode = proc.returncode or 0
        if returncode != 0:
            stderr = stderr_b.decode("utf-8", errors="replace")
            message = f"git {' '.join(args[:5])} failed ({returncode}): {stderr.strip()}"
            if _looks_transient_git_error(stderr):
                raise TransientError(message)
            raise PermanentError(message)
        if stdout_truncated:
            raise DiffOutputLimitExceeded(
                f"git diff output exceeded configured host-safety ceiling {max_stdout_bytes}; "
                "split the Gerrit Change or raise repos.max_diff_bytes"
            )
        return CommandResult(
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            returncode=returncode,
        )


def _parse_changed_lines(diff: str) -> list[ChangedLine]:
    old_path: str | None = None
    new_path: str | None = None
    old_line = 0
    new_line = 0
    result: list[ChangedLine] = []
    hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    for raw in diff.splitlines():
        if raw.startswith("--- a/"):
            old_path = raw[6:]
            continue
        if raw == "--- /dev/null":
            old_path = None
            continue
        if raw.startswith("+++ b/"):
            new_path = raw[6:]
            continue
        if raw == "+++ /dev/null":
            new_path = None
            continue
        match = hunk_re.match(raw)
        if match:
            old_line = int(match.group(1))
            new_line = int(match.group(3))
            continue
        if raw.startswith("@@"):
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            path = new_path or old_path
            if path:
                result.append(
                    ChangedLine(
                        path=path,
                        side=DiffSide.REVISION,
                        line=new_line,
                        text=raw[1:],
                    )
                )
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            path = new_path or old_path
            if path:
                result.append(
                    ChangedLine(
                        path=path,
                        side=DiffSide.PARENT,
                        line=old_line,
                        text=raw[1:],
                    )
                )
            old_line += 1
        elif raw.startswith(" "):
            old_line += 1
            new_line += 1
    return result


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


def _looks_transient_git_error(stderr: str) -> bool:
    value = stderr.lower()
    return any(
        token in value
        for token in (
            "connection reset",
            "connection timed out",
            "connection refused",
            "remote end hung up",
            "could not resolve host",
            "broken pipe",
        )
    )


def _looks_rebuildable_mirror_error(message: str) -> bool:
    value = message.lower()
    return any(
        token in value
        for token in (
            "not a git repository",
            "does not appear to be a git repository",
            "another git process seems to be running",
            "unable to create",
            ".lock': file exists",
            ".lock\": file exists",
            "bad object",
            "object file",
            "is corrupt",
            "corrupt object",
            "could not parse object",
        )
    )


def _shell_quote(value: str) -> str:
    if not value or re.search(r"[\s'\"]", value):
        return "'" + value.replace("'", "'\\''") + "'"
    return value


def _project_storage_name(project: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", project).strip("_") or "project"
    suffix = hashlib.sha256(project.encode()).hexdigest()[:12]
    return f"{clean}-{suffix}"


def _path_is_excluded(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _git_review_pathspecs(patterns: Sequence[str]) -> tuple[str, ...]:
    """Build a small Git pathspec set without expanding every changed path into argv.

    Basename-only patterns (the defaults) are converted to recursive Git globs. Keeping the argv
    proportional to configured exclusions rather than changed-file count avoids Linux ARG_MAX on
    very wide Changes.
    """

    pathspecs = ["."]
    for raw in patterns:
        pattern = raw.replace("\\", "/").lstrip("/")
        if not pattern:
            continue
        git_glob = pattern if "/" in pattern else f"**/{pattern}"
        pathspecs.append(f":(exclude,glob){git_glob}")
    return tuple(pathspecs)


def _first_parent(rev_list_line: str) -> str | None:
    parents = _parents(rev_list_line)
    return parents[0] if parents else None


def _parents(rev_list_line: str) -> tuple[str, ...]:
    parts = rev_list_line.split()
    return tuple(parts[1:]) if len(parts) > 1 else ()


def _remove_directory_if_present(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _remove_path_if_present(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path, onexc=_remove_readonly_path)


def _remove_readonly_path(function, path: str, error: OSError) -> None:  # type: ignore[no-untyped-def]
    if not isinstance(error, PermissionError):
        raise error
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _clear_owned_work_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        _remove_path_if_present(child)


def _acquire_worker_lock(path: Path) -> BinaryIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError(
            f"repository cache is already owned by another review worker: {path}"
        ) from exc
    return handle


def _release_worker_lock(handle: BinaryIO) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
