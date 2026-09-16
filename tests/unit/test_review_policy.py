from __future__ import annotations

from pe_review_agent.config import ReviewSettings
from pe_review_agent.review.policy import DEFAULT_FIRMWARE_POLICY, load_policy


async def test_policy_reads_only_requested_baseline_and_caps_aggregate_bytes() -> None:
    calls: list[tuple[str, int]] = []

    async def reader(path: str, limit: int) -> str | None:
        calls.append((path, limit))
        if path == ".reviewbot/rules.md":
            return "baseline rule"
        if path == "AGENTS.md":
            return "x" * limit
        return None

    settings = ReviewSettings(max_context_file_bytes=4096, max_policy_bytes=8192)
    policy = await load_policy(
        base_revision_sha="a" * 40,
        settings=settings,
        read_revision_text=reader,
    )

    assert DEFAULT_FIRMWARE_POLICY.strip() in policy
    assert "baseline rule" in policy
    assert "aaaaaaaaaaaa" in policy
    assert len(policy.encode("utf-8")) <= settings.max_policy_bytes
    assert calls
    assert all(limit <= settings.max_context_file_bytes for _, limit in calls)


async def test_policy_without_baseline_never_reads_candidate_repository() -> None:
    called = False

    async def reader(_path: str, _limit: int) -> str | None:
        nonlocal called
        called = True
        return "candidate-controlled instructions"

    policy = await load_policy(
        base_revision_sha=None,
        settings=ReviewSettings(),
        read_revision_text=reader,
    )

    assert policy == DEFAULT_FIRMWARE_POLICY.strip()
    assert called is False
