from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from pe_review_agent.config import ReviewSettings
from pe_review_agent.domain import Finding, FindingLineage, FindingLocation, ReviewResult, Severity
from pe_review_agent.gerrit.client import build_review_input
from pe_review_agent.review.lineage import FindingHistory, reconcile_finding_lineage
from pe_review_agent.review.native import NativeFirmwareReviewEngine
from pe_review_agent.review.project_policy import ProjectReviewPolicy, decide_code_review_vote


def policy(**updates):
    return ProjectReviewPolicy(
        output_language="ko-KR", auto_code_review=True, bound_at=datetime.now(UTC), **updates
    )


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("count,expected", [(0, 1), (1, 0), (8, 0)])
def test_vote_uses_total_validated_count_not_published_comments(partial, count, expected):
    result = ReviewResult(
        summary="result",
        findings=[],
        review_metadata={
            "validated_finding_count": count,
            "lineage_complete": not partial,
            "review_budget": {"stop_reasons": ["max_tool_rounds"] if partial else []},
        },
    )
    decision = decide_code_review_vote(policy(), result)
    assert decision.value == expected
    assert decision.finding_count == count


@pytest.mark.parametrize(
    "metadata",
    [
        {"validated_finding_count": 0, "skipped_no_reviewable_text": True},
        {"validated_finding_count": 0, "skipped_reason": "merge commit"},
    ],
)
def test_skipped_result_never_votes(metadata):
    result = ReviewResult(summary="skip", findings=[], review_metadata=metadata)
    assert decide_code_review_vote(policy(), result).value is None


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"validated_finding_count": None},
        {"validated_finding_count": "0"},
        {"validated_finding_count": False},
        {"validated_finding_count": -1},
    ],
)
def test_valid_result_without_native_count_falls_back_to_full_findings(metadata):
    clean = ReviewResult(summary="clean", findings=[], review_metadata=metadata)
    dirty = ReviewResult(summary="dirty", findings=[issue()], review_metadata=metadata)
    assert decide_code_review_vote(policy(), clean).value == 1
    assert decide_code_review_vote(policy(), dirty).value == 0


def test_legacy_and_disabled_policy_never_vote():
    result = ReviewResult(
        summary="valid", findings=[], review_metadata={"validated_finding_count": 0}
    )
    assert decide_code_review_vote(None, result).value is None
    assert (
        decide_code_review_vote(
            policy().model_copy(update={"auto_code_review": False}), result
        ).value
        is None
    )


def issue():
    return Finding(
        severity=Severity.P1,
        category="timeout",
        title="Timeout ignored",
        message="Unhandled timeout",
        impact="Stale state",
        evidence="return discarded",
        location=FindingLocation(path="fw.c", start_line=2),
        confidence=0.98,
        lineage=FindingLineage.PERSISTING,
    )


def test_remaining_finding_cannot_become_plus_one_even_with_bad_count_metadata():
    result = ReviewResult(
        summary="remaining", findings=[issue()], review_metadata={"validated_finding_count": 0}
    )
    assert decide_code_review_vote(policy(), result).value == 0


def test_language_clone_shares_provider_without_mutating_other_jobs():
    llm = SimpleNamespace(settings=SimpleNamespace(model="test"))
    settings = ReviewSettings(output_language="ko-KR")
    engine = NativeFirmwareReviewEngine(llm, settings)
    english = engine.with_output_language("en-US")
    assert english.llm is engine.llm
    assert english.validator is not engine.validator
    assert english.settings.output_language == "en-US"
    assert engine.settings.output_language == "ko-KR"
    assert engine.with_output_language("ko-KR") is engine
    assert "English" in english._language_instruction()
    assert "Korean" in engine._language_instruction()


def test_server_generated_tracking_and_finding_labels_follow_language():
    review = ReviewResult(
        summary="검토 결과", findings=[issue()], review_metadata={"output_language": "ko-KR"}
    )
    tracked = reconcile_finding_lineage(
        project="team/fw",
        review=review,
        history=FindingHistory(None, (), frozenset()),
        complete=False,
    ).review
    assert "부분 검토" in tracked.summary
    assert "해결된 것으로 판정하지 않았습니다" in tracked.summary
    payload = build_review_input(tracked)
    message = payload["comments"]["fw.c"][0]["message"]
    assert "### `P1` Timeout ignored" in message
    assert "`영향`" in message
    assert "`근거`" in message
