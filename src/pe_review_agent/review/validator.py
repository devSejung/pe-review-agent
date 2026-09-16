from __future__ import annotations

from pathlib import Path

from pe_review_agent.config import ReviewSettings
from pe_review_agent.domain import Finding, ReviewContext, Severity


class FindingValidator:
    def __init__(self, settings: ReviewSettings) -> None:
        self.settings = settings

    def validate(self, context: ReviewContext, findings: list[Finding]) -> list[Finding]:
        changed_files = set(context.changed_files)
        changed_lines = {(line.path, line.line) for line in context.changed_lines}
        root = Path(context.repository_root).resolve()
        accepted: list[Finding] = []
        seen: set[str] = set()

        for finding in findings:
            if finding.confidence < self.settings.min_confidence:
                continue
            location = finding.location
            if location.path not in changed_files:
                continue
            # Native comments are anchored to a changed line. This deliberately rejects a model
            # pointing at arbitrary pre-existing code and keeps the review about this Patch Set.
            if (location.path, location.start_line) not in changed_lines:
                continue
            target = (root / location.path).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            if not target.is_file():
                continue
            try:
                line_count = sum(1 for _ in target.open("r", encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            if location.start_line > line_count:
                continue
            if location.end_line and location.end_line > line_count:
                continue
            category = finding.category.strip().lower()
            if category in {"style", "naming", "documentation", "formatting"}:
                continue
            finding.ensure_fingerprint(project=context.project)
            assert finding.fingerprint is not None
            if finding.fingerprint in seen:
                continue
            seen.add(finding.fingerprint)
            accepted.append(finding)

        severity_order = {Severity.P0: 0, Severity.P1: 1, Severity.P2: 2}
        accepted.sort(key=lambda item: (severity_order[item.severity], -item.confidence))
        return accepted[: self.settings.max_findings]
