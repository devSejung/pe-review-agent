from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pe_review_agent.retry import TransientError


@dataclass(frozen=True, slots=True)
class VoteObservation:
    current_value: int
    can_vote: bool
    marker_found: bool
    label_exists: bool


def parse_vote_observation(
    detail: Mapping[str, Any],
    messages: list[Any],
    *,
    account_id: int,
    patchset_number: int,
    tag: str,
    marker: str,
    target: int,
) -> VoteObservation:
    labels = detail.get("labels")
    if not isinstance(labels, Mapping):
        raise TransientError("Gerrit review response lacks label details")
    label = labels.get("Code-Review")
    if label is not None and not isinstance(label, Mapping):
        raise TransientError("Gerrit Code-Review label has an invalid shape")
    value = 0
    if label is not None:
        approvals = label.get("all", [])
        if not isinstance(approvals, list):
            raise TransientError("Gerrit label approvals have an invalid shape")
        for approval in approvals:
            if not isinstance(approval, Mapping):
                raise TransientError("Gerrit returned an invalid approval")
            if approval.get("_account_id") == account_id:
                raw = approval.get("value", 0)
                if type(raw) is not int:
                    raise TransientError("Gerrit approval value is not an integer")
                value = raw
    permitted = detail.get("permitted_labels", {})
    if not isinstance(permitted, Mapping):
        raise TransientError("Gerrit permitted_labels has an invalid shape")
    allowed = permitted.get("Code-Review", [])
    if not isinstance(allowed, list):
        raise TransientError("Gerrit Code-Review permission range is invalid")
    # Gerrit 3.8.10 PostReview.checkLabels always permits zero for an existing label.
    can_vote = label is not None and (
        target == 0 or str(target) in [str(v).strip().lstrip("+") for v in allowed]
    )
    marker_found = False
    for item in messages:
        if not isinstance(item, Mapping):
            raise TransientError("Gerrit returned an invalid change message")
        author = item.get("author")
        if (
            isinstance(author, Mapping)
            and author.get("_account_id") == account_id
            and item.get("_revision_number") == patchset_number
            and item.get("tag") == tag
            and isinstance(item.get("message"), str)
            and " ".join(item["message"].split()).endswith(" ".join(marker.split()))
        ):
            marker_found = True
    return VoteObservation(value, can_vote, marker_found, label is not None)
