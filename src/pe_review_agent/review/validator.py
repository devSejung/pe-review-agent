from __future__ import annotations

from pathlib import Path

from pe_review_agent.config import ReviewSettings
from pe_review_agent.domain import DiffSide, Finding, ReviewContext, Severity


class FindingValidator:
    def __init__(self, settings: ReviewSettings) -> None:
        self.settings = settings

    def validate(self, context: ReviewContext, findings: list[Finding]) -> list[Finding]:
        changed_files = set(context.changed_files)
        changed_lines = {
            (line.path, line.side, line.line): line.text for line in context.changed_lines
        }
        previous_semantic_ids = {
            finding.semantic_id
            for finding in context.previous_findings
            if finding.semantic_id is not None
        }
        root = Path(context.repository_root).resolve()
        accepted: list[Finding] = []
        seen: set[str] = set()

        for finding in findings:
            if finding.confidence < self.settings.min_confidence:
                continue
            location = finding.location
            is_previous_finding = (
                finding.semantic_id is not None and finding.semantic_id in previous_semantic_ids
            )
            if location.path not in changed_files and not is_previous_finding:
                continue
            # Native comments are anchored to a changed line. This deliberately rejects a model
            # pointing at arbitrary pre-existing code and keeps the review about this Patch Set.
            # The one exception is a semantic ID from the previous published baseline: it must be
            # able to remain in the durable result even when its original line was untouched, so a
            # successful verifier can explicitly say the old defect still exists. Persisting
            # findings are suppressed from fresh inline publication later in the pipeline.
            if (
                location.path,
                location.side,
                location.start_line,
            ) not in changed_lines and not is_previous_finding:
                continue
            target = (root / location.path).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            anchor_text = changed_lines.get(
                (location.path, location.side, location.start_line)
            )
            if location.side == DiffSide.PARENT:
                # Gerrit anchors deleted-code findings on the PARENT side. The candidate worktree
                # may no longer contain the file at all, so validate active changed-line ranges
                # against the parsed parent-side diff instead of the candidate filesystem.
                if anchor_text is not None:
                    if location.start_character > len(anchor_text):
                        continue
                    if location.end_line is not None:
                        end_text = changed_lines.get(
                            (location.path, DiffSide.PARENT, location.end_line)
                        )
                        if end_text is None:
                            continue
                        if location.end_character is None:
                            location.end_character = len(end_text)
                        if location.end_character > len(end_text):
                            continue
                elif not is_previous_finding:
                    continue
            else:
                if not target.is_file():
                    continue
                try:
                    lines = target.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeDecodeError):
                    continue
                line_count = len(lines)
                if location.start_line > line_count:
                    continue
                start_text = lines[location.start_line - 1]
                if location.start_character > len(start_text):
                    continue
                if location.end_line and location.end_line > line_count:
                    continue
                if location.end_line is not None:
                    end_text = lines[location.end_line - 1]
                    if location.end_character is None:
                        location.end_character = len(end_text)
                    if location.end_character > len(end_text):
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
