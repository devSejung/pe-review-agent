from __future__ import annotations

from dataclasses import dataclass

from pe_review_agent.domain import Finding

CANDIDATE_CHECKPOINT_VERSION = "candidate-v1"


@dataclass(frozen=True, slots=True)
class CandidateChunkCheckpoint:
    chunk_key: str
    parent_chunk_key: str | None
    status: str
    paths: tuple[str, ...]
    change_summary: str
    findings: tuple[Finding, ...]
    input_tokens: int
    output_tokens: int
    llm_calls: int
    tool_calls: int
