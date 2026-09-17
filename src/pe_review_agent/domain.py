from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class JobState(StrEnum):
    RECEIVED = "RECEIVED"
    FETCHING = "FETCHING"
    REVIEWING = "REVIEWING"
    VALIDATING = "VALIDATING"
    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    PUBLISHING = "PUBLISHING"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED_TRANSIENT = "FAILED_TRANSIENT"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    SUPERSEDED = "SUPERSEDED"
    SKIPPED_SCOPE = "SKIPPED_SCOPE"
    DONE = "DONE"


TERMINAL_JOB_STATES = {
    JobState.FAILED_PERMANENT,
    JobState.SUPERSEDED,
    JobState.SKIPPED_SCOPE,
    JobState.DONE,
}


class AttemptStage(StrEnum):
    FETCH = "FETCH"
    REVIEW = "REVIEW"
    VALIDATE = "VALIDATE"
    PUBLISH = "PUBLISH"
    RECONCILE = "RECONCILE"


class Severity(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class FindingLineage(StrEnum):
    NEW = "NEW"
    PERSISTING = "PERSISTING"
    REOPENED = "REOPENED"


class DiffSide(StrEnum):
    REVISION = "REVISION"
    PARENT = "PARENT"


class GerritPatchsetEvent(BaseModel):
    project: str
    change_number: int
    patchset_number: int
    revision_sha: str = Field(min_length=7, max_length=64)
    ref: str | None = None
    branch: str | None = None
    change_id: str | None = None
    uploader: str | None = None
    occurred_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class FindingLocation(BaseModel):
    path: str
    side: DiffSide = DiffSide.REVISION
    start_line: int = Field(ge=1)
    start_character: int = Field(default=0, ge=0)
    end_line: int | None = Field(default=None, ge=1)
    end_character: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> FindingLocation:
        if self.end_line is None:
            if self.end_character is not None:
                raise ValueError("end_character requires end_line")
            return self
        if self.end_line < self.start_line:
            raise ValueError("end_line cannot precede start_line")
        if self.end_line == self.start_line and self.end_character is None:
            self.end_character = self.start_character + 1
        if (
            self.end_line == self.start_line
            and self.end_character is not None
            and self.end_character <= self.start_character
        ):
            raise ValueError("end_character cannot precede start_character")
        return self


class Finding(BaseModel):
    severity: Severity
    category: str
    title: str
    message: str
    impact: str
    evidence: str
    remediation: str | None = None
    location: FindingLocation
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    fingerprint: str | None = None
    semantic_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    lineage: FindingLineage | None = None

    def ensure_fingerprint(self, *, project: str) -> Finding:
        if self.fingerprint:
            return self
        payload = {
            "project": project,
            "path": self.location.path,
            "side": self.location.side.value,
            "line": self.location.start_line,
            "category": _normalize_text(self.category),
            "title": _normalize_text(self.title),
            "evidence": _normalize_text(self.evidence),
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.fingerprint = digest[:32]
        return self

    def ensure_semantic_id(self, *, project: str) -> Finding:
        if self.semantic_id:
            return self
        payload = {
            "project": project,
            "path": self.location.path,
            "category": _normalize_text(self.category),
            "title": _normalize_text(self.title),
            "message": _normalize_text(self.message),
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.semantic_id = digest[:32]
        return self


class ReviewResult(BaseModel):
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    model: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    review_metadata: dict[str, Any] = Field(default_factory=dict)


class ChangedLine(BaseModel):
    path: str
    side: DiffSide = DiffSide.REVISION
    line: int = Field(ge=1)
    text: str


class ReviewContext(BaseModel):
    project: str
    change_number: int
    patchset_number: int
    revision_sha: str
    base_revision_sha: str | None = None
    subject: str | None = None
    branch: str | None = None
    commit_message: str | None = None
    diff: str
    changed_files: list[str] = Field(default_factory=list)
    changed_lines: list[ChangedLine] = Field(default_factory=list)
    policy_text: str
    repository_root: str
    previous_findings: list[Finding] = Field(default_factory=list)
    historical_findings: list[Finding] = Field(default_factory=list)
    previous_patchset_number: int | None = Field(default=None, ge=1)
    skip_reason: str | None = None


@dataclass(frozen=True, slots=True)
class JobIdentity:
    project: str
    change_number: int
    revision_sha: str
    review_policy_version: str

    @property
    def key(self) -> str:
        return (
            f"{self.project}:{self.change_number}:{self.revision_sha}:{self.review_policy_version}"
        )


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())
