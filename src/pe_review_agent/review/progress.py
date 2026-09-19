from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from pe_review_agent.domain import Finding

PROGRESS_VERSION = "review-v2"
Phase = Literal["candidate", "verification"]


class Usage(BaseModel):
    """Operational charges; tokens are informational and never gate execution."""

    llm_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)

    def add(self, other: Usage) -> None:
        for name in type(self).model_fields:
            setattr(self, name, getattr(self, name) + getattr(other, name))


class WorkCheckpoint(BaseModel):
    phase: Phase
    key: str
    parent_key: str | None = None
    status: Literal["DONE", "SPLIT", "STOPPED"]
    paths: list[str] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)
    usage: Usage = Field(default_factory=Usage)
    limitations: list[str] = Field(default_factory=list)


class ReviewProgress(BaseModel):
    """Compact results/decisions only: never persist diff or tool/reasoning transcripts."""

    version: Literal["review-v2"] = PROGRESS_VERSION
    input_key: str
    phase: Literal["candidate", "verification", "complete"] = "candidate"
    checkpoints: dict[str, WorkCheckpoint] = Field(default_factory=dict)
    frozen_candidates: list[Finding] = Field(default_factory=list)
    change_summary: str = ""
    coverage: dict[str, Any] = Field(default_factory=dict)
    candidate_usage: Usage = Field(default_factory=Usage)
    candidate_limitations: list[str] = Field(default_factory=list)
    candidate_stats: dict[str, int] = Field(default_factory=dict)
    legacy_usage: Usage = Field(default_factory=Usage)
    legacy_present: bool = False
    result: dict[str, Any] | None = None


class Invocation(BaseModel):
    id: str
    input_key: str
    phase: Phase
    work_key: str
    kind: Literal["llm", "tool"]
    status: Literal["started", "completed", "failed"] = "started"
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    error: str | None = None


class ProgressBackend(Protocol):
    async def load(self, input_key: str) -> ReviewProgress | None: ...

    async def save(self, progress: ReviewProgress) -> None: ...

    async def record_invocation(self, invocation: Invocation) -> None: ...

    async def totals(self) -> dict[str, int]: ...

    async def legacy(self) -> tuple[bool, Usage]: ...


class MemoryProgressBackend:
    """Standalone/replay backend; production injects the lease-guarded PostgreSQL backend."""

    def __init__(self) -> None:
        self.progress: dict[str, ReviewProgress] = {}
        self.invocations: dict[str, Invocation] = {}

    async def load(self, input_key: str) -> ReviewProgress | None:
        value = self.progress.get(input_key)
        return value.model_copy(deep=True) if value else None

    async def save(self, progress: ReviewProgress) -> None:
        self.progress[progress.input_key] = progress.model_copy(deep=True)

    async def record_invocation(self, invocation: Invocation) -> None:
        self.invocations[invocation.id] = invocation.model_copy(deep=True)

    async def totals(self) -> dict[str, int]:
        values = list(self.invocations.values())
        return {
            "llm_calls": sum(item.kind == "llm" for item in values),
            "tool_calls": sum(item.kind == "tool" for item in values),
            "input_tokens": sum(item.input_tokens or 0 for item in values),
            "output_tokens": sum(item.output_tokens or 0 for item in values),
            "failed_calls": sum(item.status == "failed" for item in values),
            "unconfirmed_calls": sum(item.status == "started" for item in values),
            "llm_usage_unknown": sum(
                item.kind == "llm" and item.input_tokens is None for item in values
            ),
        }

    async def legacy(self) -> tuple[bool, Usage]:
        return False, Usage()
