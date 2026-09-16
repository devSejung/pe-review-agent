from pathlib import Path

from pe_review_agent.config import ReviewSettings
from pe_review_agent.domain import (
    ChangedLine,
    Finding,
    FindingLocation,
    ReviewContext,
    Severity,
)
from pe_review_agent.review.validator import FindingValidator


def _finding(
    *, line: int = 3, category: str = "error-propagation", confidence: float = 0.95
) -> Finding:
    return Finding(
        severity=Severity.P1,
        category=category,
        title="Timeout result is discarded",
        message="The timeout return value is ignored and execution continues.",
        impact="Later code consumes stale training data.",
        evidence="poll_training_done() can return -ETIMEDOUT.",
        remediation="Propagate or handle the error before advancing.",
        location=FindingLocation(path="fw/train.c", start_line=line),
        confidence=confidence,
    )


def _context(root: Path) -> ReviewContext:
    target = root / "fw" / "train.c"
    target.parent.mkdir(parents=True)
    target.write_text("a\nb\nchanged();\nd\n", encoding="utf-8")
    return ReviewContext(
        project="soc/fw",
        change_number=42,
        patchset_number=2,
        revision_sha="a" * 40,
        diff="",
        changed_files=["fw/train.c"],
        changed_lines=[ChangedLine(path="fw/train.c", line=3, text="changed();")],
        policy_text="policy",
        repository_root=str(root),
    )


def test_validator_accepts_only_high_signal_changed_line(tmp_path: Path) -> None:
    context = _context(tmp_path)
    validator = FindingValidator(ReviewSettings(min_confidence=0.82))

    accepted = validator.validate(
        context,
        [
            _finding(),
            _finding(line=2),
            _finding(category="style"),
            _finding(confidence=0.4),
        ],
    )

    assert len(accepted) == 1
    assert accepted[0].fingerprint
    assert accepted[0].location.start_line == 3


def test_validator_deduplicates_fingerprint(tmp_path: Path) -> None:
    context = _context(tmp_path)
    validator = FindingValidator(ReviewSettings())
    one = _finding().ensure_fingerprint(project=context.project)
    duplicate = _finding().ensure_fingerprint(project=context.project)

    accepted = validator.validate(context, [one, duplicate])

    assert len(accepted) == 1
