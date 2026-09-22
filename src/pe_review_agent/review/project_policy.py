from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pe_review_agent.domain import ReviewResult

ReviewLanguage = Literal["INHERIT", "ko-KR", "en-US"]


class ProjectReviewPolicy(BaseModel):
    """Immutable per-job policy. Never mutate the shared worker/LLM settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    review_language: ReviewLanguage = "INHERIT"
    output_language: Literal["ko-KR", "en-US"]
    auto_code_review: bool = False
    generation: int = Field(default=0, ge=0)
    bound_at: datetime
    source: Literal["project", "default", "legacy"] = "default"


@dataclass(frozen=True, slots=True)
class VoteDecision:
    value: int | None
    reason: str
    finding_count: int | None = None


def decide_code_review_vote(
    policy: ProjectReviewPolicy | None, review: ReviewResult
) -> VoteDecision:
    """Apply the operator policy, not a model-authored verdict or inline comment count.

    Partial, budget-limited results ARE eligible. Failed reviews never reach this function.
    Skipped/no-review results are not eligible.
    """

    if policy is None:
        return VoteDecision(None, "legacy_job_without_project_policy")
    if not policy.auto_code_review:
        return VoteDecision(None, "project_voting_disabled")
    metadata = review.review_metadata
    if metadata.get("skipped_reason") or metadata.get("skipped_no_reviewable_text"):
        return VoteDecision(None, "review_skipped")
    count = metadata.get("validated_finding_count")
    if type(count) is not int or count < 0:
        # A durable ReviewResult is the contract boundary for voting. Native review results expose
        # validated_finding_count so max_findings/inline suppression cannot hide findings, while
        # alternate engines may legitimately provide the complete list without that metadata.
        count = len(review.findings)
    # Persisting findings may be omitted from fresh inline comments. max_findings may be zero.
    # Neither presentation choice may turn an actual finding into a +1.
    count = max(count, len(review.findings))
    return VoteDecision(
        1 if count == 0 else 0, "zero_findings" if count == 0 else "findings_present", count
    )
