from __future__ import annotations

from collections.abc import Iterable

from pe_review_agent.retry import PermanentError


class ProjectAllowlist:
    """Exact-match project allowlist shared by event and REST boundaries."""

    def __init__(self, projects: Iterable[str]) -> None:
        values = tuple(projects)
        if any(not project for project in values):
            raise ValueError("Gerrit project allowlist cannot contain empty project names")
        self._projects = frozenset(values)

    @property
    def projects(self) -> tuple[str, ...]:
        return tuple(sorted(self._projects))

    def allows(self, project: str) -> bool:
        return project in self._projects

    def require(self, project: str) -> None:
        if not self.allows(project):
            raise PermanentError(f"Gerrit project is not allowlisted: {project}")
