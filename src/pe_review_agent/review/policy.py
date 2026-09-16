from __future__ import annotations

from pathlib import Path

from pe_review_agent.config import ReviewSettings

DEFAULT_FIRMWARE_POLICY = """\
# Firmware correctness review policy

The review bot exists to find concrete defects, not to perform style review.

Prioritize defects in these areas:
- Error propagation: ignored/overwritten errors, success returned after failure, partially
  initialized state used after an error.
- Timeout and polling: unbounded loops, incorrect timeout units/wraparound, timeout treated as
  success, poll condition inversion, missing delay/barrier where required.
- MMIO/register semantics: missing volatile access semantics, unsafe read-modify-write, W1C/W1S
  misuse, reserved-bit corruption, wrong mask/shift, wrong register width, repeated side-effect
  reads.
- Integer correctness: width truncation, signed/unsigned conversion, overflow, invalid shift width,
  sign extension, pointer/integer width assumptions.
- ARM/concurrency: missing ordering/barrier requirements, races on shared state, unsafe atomicity
  assumptions, cache/coherency assumptions, interrupt-context hazards.
- Memory safety: NULL, bounds, lifetime/use-after-free, alignment, aliasing, size/count confusion.
- Initialization and cleanup: ordering dependencies, use before init, double cleanup, leaks on
  failure, resource/clock/lock/IRQ lifetime imbalance.
- API contracts: unchecked return values, caller/callee contract mismatch, unit/endianness mismatch.
- Compiler/optimization hazards: undefined behavior or logic that only works at a particular
  optimization level/layout.

When reviewing register code, inspect related masks/macros/register definitions before claiming a
bug. When reviewing a changed call, inspect the callee/caller if the behavior depends on its return
contract. Use repository tools rather than guessing.

Do not report:
- naming/style preferences,
- requests to add comments/documentation,
- speculative issues without a concrete execution condition and code evidence,
- pre-existing problems unrelated to the changed lines,
- duplicate variants of the same root cause.

Every publishable finding must state the trigger, impact, evidence, and a useful remediation
direction. Prefer zero findings over weak findings.
"""


def load_policy(root: str | Path, settings: ReviewSettings) -> str:
    repo_root = Path(root)
    sections = [DEFAULT_FIRMWARE_POLICY.strip()]
    for relative in (
        Path(".reviewbot/rules.md"),
        Path(".reviewbot/architecture.md"),
        Path(".reviewbot/critical_paths.md"),
        Path("AGENTS.md"),
    ):
        target = repo_root / relative
        if not target.is_file():
            continue
        if target.stat().st_size > settings.max_context_file_bytes:
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        sections.append(f"# Repository policy: {relative.as_posix()}\n{text.strip()}")
    return "\n\n".join(sections)
