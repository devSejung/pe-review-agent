from __future__ import annotations

from dataclasses import dataclass

from pe_review_agent.domain import Finding, FindingLineage, ReviewResult


@dataclass(frozen=True, slots=True)
class FindingHistory:
    baseline_patchset: int | None
    previous_findings: tuple[Finding, ...]
    seen_semantic_ids: frozenset[str]
    historical_findings: tuple[Finding, ...] = ()


@dataclass(frozen=True, slots=True)
class LineageReconciliation:
    review: ReviewResult
    resolved_findings: tuple[Finding, ...]
    new_count: int
    persisting_count: int
    reopened_count: int


def reconcile_finding_lineage(
    *,
    project: str,
    review: ReviewResult,
    history: FindingHistory,
    complete: bool = True,
) -> LineageReconciliation:
    previous_by_id: dict[str, Finding] = {}
    for finding in history.previous_findings:
        finding.ensure_semantic_id(project=project)
        assert finding.semantic_id is not None
        previous_by_id[finding.semantic_id] = finding

    current_ids: set[str] = set()
    new_count = 0
    persisting_count = 0
    reopened_count = 0
    for finding in review.findings:
        finding.ensure_semantic_id(project=project)
        assert finding.semantic_id is not None
        current_ids.add(finding.semantic_id)
        if finding.semantic_id in previous_by_id:
            finding.lineage = FindingLineage.PERSISTING
            persisting_count += 1
        elif finding.semantic_id in history.seen_semantic_ids:
            finding.lineage = FindingLineage.REOPENED
            reopened_count += 1
        else:
            finding.lineage = FindingLineage.NEW
            new_count += 1

    resolved = (
        tuple(
            finding
            for semantic_id, finding in previous_by_id.items()
            if semantic_id not in current_ids
        )
        if complete
        else ()
    )
    tracking = {
        "baseline_patchset": history.baseline_patchset,
        "complete": complete,
        "new": new_count,
        "persisting": persisting_count,
        "reopened": reopened_count,
        "resolved": len(resolved),
        "resolved_semantic_ids": [finding.semantic_id for finding in resolved],
    }
    metadata = dict(review.review_metadata)
    metadata["finding_lineage"] = tracking
    summary = _tracking_summary(
        original=review.summary,
        baseline_patchset=history.baseline_patchset,
        new_count=new_count,
        persisting_count=persisting_count,
        reopened_count=reopened_count,
        resolved=resolved,
        complete=complete,
        language=str(review.review_metadata.get("output_language", "en-US")),
    )
    reconciled = review.model_copy(
        update={"summary": summary, "review_metadata": metadata},
        deep=True,
    )
    return LineageReconciliation(
        review=reconciled,
        resolved_findings=resolved,
        new_count=new_count,
        persisting_count=persisting_count,
        reopened_count=reopened_count,
    )


def findings_for_inline_publication(review: ReviewResult) -> list[Finding]:
    """Do not repeat an already-known finding as a fresh inline comment on every Patch Set."""
    return [
        finding for finding in review.findings if finding.lineage is not FindingLineage.PERSISTING
    ]


def _tracking_summary(
    *,
    original: str,
    baseline_patchset: int | None,
    new_count: int,
    persisting_count: int,
    reopened_count: int,
    resolved: tuple[Finding, ...],
    complete: bool,
    language: str = "en-US",
) -> str:
    if language == "ko-KR":
        baseline = (
            f"PS {baseline_patchset} 대비"
            if baseline_patchset is not None
            else "이전 게시 기준 없음"
        )
        header = (
            f"Patch Set 추적 ({baseline}{'' if complete else ', 부분 검토'}): "
            f"신규 {new_count}건, 지속 {persisting_count}건, 재발 {reopened_count}건"
        )
        header += (
            f", 해결 {len(resolved)}건."
            if complete
            else (". 미검토된 이전 이슈는 해결된 것으로 판정하지 않았습니다.")
        )
        sections = [header, original.strip()]
        if resolved:
            lines = [f"- [{f.severity.value}] {f.title} ({f.location.path})" for f in resolved[:10]]
            if len(resolved) > 10:
                lines.append(f"- 외 {len(resolved) - 10}건")
            sections.append("이전 게시 Patch Set 이후 해결된 이슈:\n" + "\n".join(lines))
        return "\n\n".join(section for section in sections if section)
    if baseline_patchset is None and complete:
        header = (
            f"Patch Set tracking: {new_count} new finding(s); no previously published baseline."
        )
    elif baseline_patchset is None:
        header = (
            f"Patch Set tracking (partial review): {new_count} new finding(s); "
            "no previously published baseline."
        )
    elif not complete:
        header = (
            f"Patch Set tracking vs PS {baseline_patchset} (partial review): {new_count} new, "
            f"{persisting_count} still present, {reopened_count} reopened. "
            "Unreviewed prior findings were not classified as fixed."
        )
    else:
        header = (
            f"Patch Set tracking vs PS {baseline_patchset}: {new_count} new, "
            f"{persisting_count} still present, {reopened_count} reopened, {len(resolved)} fixed."
        )
    sections = [header, original.strip()]
    if resolved:
        resolved_lines = [
            f"- [{finding.severity.value}] {finding.title} ({finding.location.path})"
            for finding in resolved[:10]
        ]
        if len(resolved) > 10:
            resolved_lines.append(f"- ... and {len(resolved) - 10} more")
        sections.append(
            "Fixed since the previous published Patch Set:\n" + "\n".join(resolved_lines)
        )
    return "\n\n".join(section for section in sections if section)
