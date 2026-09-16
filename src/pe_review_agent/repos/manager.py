from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from pe_review_agent.config import GerritSettings, RepoSettings
from pe_review_agent.domain import ChangedLine, ReviewContext
from pe_review_agent.retry import PermanentError, TransientError


@dataclass(frozen=True, slots=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


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
        clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", project).strip("_") or "project"
        suffix = hashlib.sha256(project.encode()).hexdigest()[:12]
        return self.settings.cache_root / f"{clean}-{suffix}.git"

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
            if not mirror.exists():
                await self._git(
                    "clone",
                    "--mirror",
                    self._ssh_url(project),
                    str(mirror),
                    env=env,
                    transient=True,
                )
            fetch_source = ref or revision_sha
            fetch_target = f"refs/pe-review/{revision_sha}"
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
            resolved = (
                await self._git("-C", str(mirror), "rev-parse", f"{fetch_target}^{{commit}}")
            ).stdout.strip()
            if resolved != revision_sha:
                detail = f"expected {revision_sha}, got {resolved}"
                raise PermanentError(f"fetched revision mismatch for {project}: {detail}")
        return mirror

    @asynccontextmanager
    async def workspace(
        self,
        *,
        project: str,
        change_number: int,
        revision_sha: str,
        ref: str | None,
    ) -> AsyncIterator[RepositoryWorkspace]:
        mirror = await self.ensure_revision(project=project, revision_sha=revision_sha, ref=ref)
        base_revision = (
            await self._git("-C", str(mirror), "rev-parse", f"{revision_sha}^1")
        ).stdout.strip()
        safe_project = re.sub(r"[^A-Za-z0-9_.-]+", "_", project).strip("_") or "project"
        root = self.settings.work_root / safe_project / str(change_number) / revision_sha[:16]
        if root.exists():
            await self._remove_worktree(mirror, root)
        root.parent.mkdir(parents=True, exist_ok=True)
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
            diff = (
                await self._git(
                    "-C",
                    str(root),
                    "diff",
                    "--find-renames",
                    "--no-ext-diff",
                    "--unified=20",
                    base_revision,
                    revision_sha,
                    "--",
                )
            ).stdout
            encoded_size = len(diff.encode("utf-8", errors="replace"))
            if encoded_size > self.settings.max_diff_bytes:
                detail = f"configured max {self.settings.max_diff_bytes}"
                raise PermanentError(f"diff is {encoded_size} bytes, above {detail}")
            changed_files_output = (
                await self._git(
                    "-C",
                    str(root),
                    "diff",
                    "--name-only",
                    "--diff-filter=ACMR",
                    base_revision,
                    revision_sha,
                    "--",
                )
            ).stdout
            changed_files = [line for line in changed_files_output.splitlines() if line]
            zero_diff = (
                await self._git(
                    "-C",
                    str(root),
                    "diff",
                    "--no-ext-diff",
                    "--unified=0",
                    base_revision,
                    revision_sha,
                    "--",
                )
            ).stdout
            yield RepositoryWorkspace(
                project=project,
                revision_sha=revision_sha,
                base_revision_sha=base_revision,
                root=root,
                mirror=mirror,
                diff=diff,
                changed_files=changed_files,
                changed_lines=_parse_changed_lines(zero_diff),
            )
        finally:
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
        )

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
    ) -> CommandResult:
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self.settings.command_timeout_seconds
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


def _parse_changed_lines(diff: str) -> list[ChangedLine]:
    current_path: str | None = None
    new_line = 0
    result: list[ChangedLine] = []
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
    for raw in diff.splitlines():
        if raw.startswith("+++ b/"):
            current_path = raw[6:]
            continue
        match = hunk_re.match(raw)
        if match:
            new_line = int(match.group(1))
            continue
        if not current_path or raw.startswith("@@"):
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            result.append(ChangedLine(path=current_path, line=new_line, text=raw[1:]))
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        elif raw.startswith(" "):
            new_line += 1
    return result


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


def _shell_quote(value: str) -> str:
    if not value or re.search(r"[\s'\"]", value):
        return "'" + value.replace("'", "'\\''") + "'"
    return value


def _remove_directory_if_present(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
