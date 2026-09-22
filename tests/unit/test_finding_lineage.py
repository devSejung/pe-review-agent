from pe_review_agent.domain import (
    Finding,
    FindingLineage,
    FindingLocation,
    ReviewResult,
    Severity,
)
from pe_review_agent.gerrit.client import build_review_input
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
    assert "### Patch Set tracking" in result.review.summary
    assert "- `Baseline` PS 4" in result.review.summary
    assert "- `New` 1" in result.review.summary
    assert "- `Persisting` 1" in result.review.summary
    assert "- `Reopened` 1" in result.review.summary
    assert "- `Fixed` 1" in result.review.summary
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


def test_persisting_only_followup_uses_markdown_change_summary_without_inline_comment() -> None:
    semantic_id = "d" * 32
    previous = _finding("Timeout is ignored", semantic_id=semantic_id)
    current = _finding("Timeout is ignored", semantic_id=semantic_id, line=14)
    review = ReviewResult(
        summary=(
            "### 변경 요약\n\n- timeout 경로를 수정합니다.\n\n"
            "### 리뷰 결과\n\n> 기존 이슈가 계속 존재합니다."
        ),
        findings=[current],
        review_metadata={"output_language": "ko-KR"},
    )
    reconciled = reconcile_finding_lineage(
        project="soc/fw",
        review=review,
        history=FindingHistory(
            baseline_patchset=4,
            previous_findings=(previous,),
            seen_semantic_ids=frozenset({semantic_id}),
        ),
    ).review
    inline_review = reconciled.model_copy(
        update={"findings": findings_for_inline_publication(reconciled)},
        deep=True,
    )

    payload = build_review_input(inline_review)

    assert "comments" not in payload
    assert "### 변경 요약" in payload["message"]
    assert "### 리뷰 결과" in payload["message"]
    assert "### Patch Set 추적" in payload["message"]
    assert "- `기준` PS 4" in payload["message"]
    assert "- `지속` 1건" in payload["message"]


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
    assert "- `Baseline` no published baseline" in result.review.summary


def test_partial_review_tracks_seen_findings_without_resolving_unseen_prior_findings() -> None:
    persisting_id = "4" * 32
    unseen_id = "5" * 32
    previous_persisting = _finding("Timeout is ignored", semantic_id=persisting_id)
    previous_unseen = _finding("Clock stays enabled", semantic_id=unseen_id, line=20)
    current_persisting = _finding("Timeout is ignored", semantic_id=persisting_id, line=14)

    result = reconcile_finding_lineage(
        project="soc/fw",
        review=ReviewResult(
            summary="Partial review result.",
            findings=[current_persisting],
            review_metadata={"lineage_complete": False},
        ),
        history=FindingHistory(
            baseline_patchset=4,
            previous_findings=(previous_persisting, previous_unseen),
            seen_semantic_ids=frozenset({persisting_id, unseen_id}),
        ),
        complete=False,
    )

    assert result.review.findings[0].lineage is FindingLineage.PERSISTING
    assert result.resolved_findings == ()
    assert result.review.review_metadata["finding_lineage"]["complete"] is False
    assert result.review.review_metadata["finding_lineage"]["resolved"] == 0
    assert "- `Status` partial review" in result.review.summary
    assert "Unreviewed prior findings were not classified as fixed." in result.review.summary
    assert findings_for_inline_publication(result.review) == []
