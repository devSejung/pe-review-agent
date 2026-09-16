from pe_review_agent.domain import (
    Finding,
    FindingLineage,
    FindingLocation,
    ReviewResult,
    Severity,
)
from pe_review_agent.review.lineage import (
    FindingHistory,
    findings_for_inline_publication,
    reconcile_finding_lineage,
)


def _finding(
    title: str,
    *,
    semantic_id: str | None = None,
    line: int = 10,
) -> Finding:
    return Finding(
        severity=Severity.P1,
        category="timeout",
        title=title,
        message=f"{title} triggers on the timeout path.",
        impact="Training can continue with stale state.",
        evidence="The timeout return value is discarded.",
        remediation="Propagate the timeout.",
        location=FindingLocation(path="fw/train.c", start_line=line),
        confidence=0.97,
        semantic_id=semantic_id,
    )


def test_lineage_reconciles_new_persisting_reopened_and_resolved() -> None:
    persisting_id = "1" * 32
    resolved_id = "2" * 32
    reopened_id = "3" * 32
    previous_persisting = _finding("Timeout is ignored", semantic_id=persisting_id)
    previous_resolved = _finding("Clock stays enabled", semantic_id=resolved_id, line=20)
    current_persisting = _finding("Timeout is ignored", semantic_id=persisting_id, line=14)
    current_reopened = _finding("IRQ state leaks", semantic_id=reopened_id, line=30)
    current_new = _finding("Width truncates address", line=40)

    result = reconcile_finding_lineage(
        project="soc/fw",
        review=ReviewResult(
            summary="Three actionable defects were verified.",
            findings=[current_persisting, current_reopened, current_new],
        ),
        history=FindingHistory(
            baseline_patchset=4,
            previous_findings=(previous_persisting, previous_resolved),
            seen_semantic_ids=frozenset({persisting_id, resolved_id, reopened_id}),
        ),
    )

    assert result.new_count == 1
    assert result.persisting_count == 1
    assert result.reopened_count == 1
    assert [finding.semantic_id for finding in result.resolved_findings] == [resolved_id]
    assert [finding.lineage for finding in result.review.findings] == [
        FindingLineage.PERSISTING,
        FindingLineage.REOPENED,
        FindingLineage.NEW,
    ]
    assert result.review.findings[2].semantic_id is not None
    assert "vs PS 4: 1 new, 1 still present, 1 reopened, 1 fixed" in result.review.summary
    assert "Clock stays enabled" in result.review.summary
    assert result.review.review_metadata["finding_lineage"]["resolved"] == 1


def test_persisting_finding_is_not_reposted_as_inline_comment() -> None:
    persisting = _finding("Timeout is ignored", semantic_id="a" * 32)
    persisting.lineage = FindingLineage.PERSISTING
    reopened = _finding("Timeout came back", semantic_id="b" * 32)
    reopened.lineage = FindingLineage.REOPENED
    new = _finding("New timeout", semantic_id="c" * 32)
    new.lineage = FindingLineage.NEW

    published = findings_for_inline_publication(
        ReviewResult(summary="tracking", findings=[persisting, reopened, new])
    )

    assert [finding.semantic_id for finding in published] == ["b" * 32, "c" * 32]


def test_initial_review_marks_every_verified_finding_new() -> None:
    current = _finding("Timeout is ignored")
    result = reconcile_finding_lineage(
        project="soc/fw",
        review=ReviewResult(summary="one issue", findings=[current]),
        history=FindingHistory(
            baseline_patchset=None,
            previous_findings=(),
            seen_semantic_ids=frozenset(),
        ),
    )

    assert result.review.findings[0].lineage is FindingLineage.NEW
    assert result.review.findings[0].semantic_id is not None
    assert "no previously published baseline" in result.review.summary
