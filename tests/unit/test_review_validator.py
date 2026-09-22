from pathlib import Path

from pe_review_agent.config import ReviewSettings
from pe_review_agent.domain import (
    ChangedLine,
    DiffSide,
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


def test_validator_derives_multiline_end_character_from_file(tmp_path: Path) -> None:
    context = _context(tmp_path)
    finding = _finding()
    finding.location = FindingLocation(
        path="fw/train.c",
        start_line=3,
        start_character=0,
        end_line=4,
    )

    accepted = FindingValidator(ReviewSettings()).validate(context, [finding])

    assert len(accepted) == 1
    assert accepted[0].location.end_character == len("d")


def test_validator_keeps_line_anchor_when_optional_start_character_exceeds_line(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    finding = _finding()
    finding.location = FindingLocation(
        path="fw/train.c",
        start_line=3,
        start_character=999,
    )

    accepted = FindingValidator(ReviewSettings()).validate(context, [finding])

    assert len(accepted) == 1
    assert accepted[0].location.start_line == 3
    assert accepted[0].location.start_character == 0
    assert accepted[0].location.end_line is None


def test_validator_degrades_invalid_revision_range_to_start_line(tmp_path: Path) -> None:
    context = _context(tmp_path)
    finding = _finding()
    finding.location = FindingLocation(
        path="fw/train.c",
        start_line=3,
        start_character=0,
        end_line=4,
        end_character=999,
    )

    accepted = FindingValidator(ReviewSettings()).validate(context, [finding])

    assert len(accepted) == 1
    assert accepted[0].location.start_line == 3
    assert accepted[0].location.start_character == 0
    assert accepted[0].location.end_line is None
    assert accepted[0].location.end_character is None


def test_validator_streams_only_needed_lines_from_large_revision_file(tmp_path: Path) -> None:
    relative = "build/tools/register/LPDDR56_PHY.csv"
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    with target.open("wb") as handle:
        for index in range(40_000):
            handle.write(f"REG_{index},0x{index:08x},FIELD_{index}\n".encode())
        # Old read_text().splitlines() decoded the whole file and rejected this unrelated tail.
        # Streaming validation must stop once the finding's requested lines have been read.
        handle.write(b"\xff\xfe\x00\n")

    start_line = 20_001
    context = ReviewContext(
        project="soc/fw",
        change_number=42,
        patchset_number=2,
        revision_sha="a" * 40,
        diff="",
        changed_files=[relative],
        changed_lines=[
            ChangedLine(
                path=relative,
                line=start_line,
                text="REG_20000,0x00004e20,FIELD_20000",
            )
        ],
        policy_text="policy",
        repository_root=str(tmp_path),
    )
    finding = _finding(line=start_line)
    finding.location = FindingLocation(
        path=relative,
        start_line=start_line,
        end_line=start_line + 1,
    )

    accepted = FindingValidator(ReviewSettings()).validate(context, [finding])

    assert len(accepted) == 1
    assert accepted[0].location.end_character == len("REG_20001,0x00004e21,FIELD_20001")


def test_validator_rejects_revision_anchor_past_end_of_file(tmp_path: Path) -> None:
    context = _context(tmp_path)
    prior = _finding(line=999)
    prior.semantic_id = "b" * 32
    context.previous_findings = [prior.model_copy(deep=True)]

    accepted = FindingValidator(ReviewSettings()).validate(context, [prior])

    assert accepted == []


def test_previous_semantic_finding_can_persist_on_untouched_line(tmp_path: Path) -> None:
    context = _context(tmp_path)
    prior = _finding(line=2)
    prior.semantic_id = "a" * 32
    context.previous_findings = [prior.model_copy(deep=True)]

    accepted = FindingValidator(ReviewSettings()).validate(context, [prior])

    assert len(accepted) == 1
    assert accepted[0].semantic_id == "a" * 32


def test_new_finding_on_untouched_line_is_still_rejected(tmp_path: Path) -> None:
    context = _context(tmp_path)
    candidate = _finding(line=2)

    accepted = FindingValidator(ReviewSettings()).validate(context, [candidate])

    assert accepted == []


def test_deleted_line_parent_side_is_valid_without_candidate_file(tmp_path: Path) -> None:
    context = ReviewContext(
        project="soc/fw",
        change_number=42,
        patchset_number=2,
        revision_sha="a" * 40,
        diff="",
        changed_files=["fw/deleted.c"],
        changed_lines=[
            ChangedLine(
                path="fw/deleted.c",
                side=DiffSide.PARENT,
                line=7,
                text="timeout_disable();",
            )
        ],
        policy_text="policy",
        repository_root=str(tmp_path),
    )
    finding = _finding(line=7)
    finding.location = FindingLocation(
        path="fw/deleted.c",
        side=DiffSide.PARENT,
        start_line=7,
        end_line=7,
        end_character=len("timeout_disable();"),
    )

    accepted = FindingValidator(ReviewSettings()).validate(context, [finding])

    assert len(accepted) == 1
    assert accepted[0].location.side == DiffSide.PARENT
