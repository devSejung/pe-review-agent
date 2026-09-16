from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from pe_review_agent.domain import AttemptStage, JobState, Severity


def _values(enum_type: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum_type)


_RETRY_TARGET_VALUES = ", ".join(
    f"'{state.value}'"
    for state in (
        JobState.FETCHING,
        JobState.REVIEWING,
        JobState.VALIDATING,
        JobState.READY_TO_PUBLISH,
        JobState.PUBLISHING,
    )
)


class PublicationStatus(StrEnum):
    PENDING = "PENDING"
    POSTED = "POSTED"
    AMBIGUOUS = "AMBIGUOUS"
    FAILED = "FAILED"


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "review_jobs"
    __table_args__ = (
        UniqueConstraint(
            "project",
            "change_number",
            "revision_sha",
            "review_policy_version",
            name="uq_review_jobs_identity",
        ),
        CheckConstraint(f"state IN ({_values(JobState)})", name="ck_review_jobs_state"),
        CheckConstraint(
            f"retry_state IS NULL OR retry_state IN ({_RETRY_TARGET_VALUES})",
            name="ck_review_jobs_retry_state",
        ),
        Index(
            "ix_review_jobs_claim",
            "state",
            "next_attempt_at",
            "lease_expires_at",
            "created_at",
        ),
        Index("ix_review_jobs_change", "project", "change_number", "patchset_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project: Mapped[str] = mapped_column(String(512), nullable=False)
    change_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    patchset_number: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    review_policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default=JobState.RECEIVED.value)
    retry_state: Mapped[str | None] = mapped_column(String(32))
    event_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_class: Mapped[str | None] = mapped_column(String(255))
    last_error: Mapped[str | None] = mapped_column(Text)
    superseded_by_job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("review_jobs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    attempts: Mapped[list[Attempt]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    result: Mapped[ReviewResultRow | None] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )
    publications: Mapped[list[Publication]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class Attempt(Base):
    __tablename__ = "review_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_review_attempts_number"),
        CheckConstraint(f"stage IN ({_values(AttemptStage)})", name="ck_review_attempts_stage"),
        Index("ix_review_attempts_job_stage", "job_id", "stage"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_jobs.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(255), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    success: Mapped[bool | None] = mapped_column(Boolean)
    retryable: Mapped[bool | None] = mapped_column(Boolean)
    error_class: Mapped[str | None] = mapped_column(String(255))
    error_message: Mapped[str | None] = mapped_column(Text)

    job: Mapped[Job] = relationship(back_populates="attempts")


class ReviewResultRow(Base):
    __tablename__ = "review_results"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(String(255))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    review_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    job: Mapped[Job] = relationship(back_populates="result")
    findings: Mapped[list[ReviewFinding]] = relationship(
        back_populates="result",
        cascade="all, delete-orphan",
        order_by="ReviewFinding.ordinal",
    )


class ReviewFinding(Base):
    __tablename__ = "review_findings"
    __table_args__ = (
        UniqueConstraint("job_id", "fingerprint", name="uq_review_findings_fingerprint"),
        UniqueConstraint("job_id", "ordinal", name="uq_review_findings_ordinal"),
        CheckConstraint(f"severity IN ({_values(Severity)})", name="ck_review_findings_severity"),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_review_findings_confidence",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_results.job_id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    impact: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    remediation: Mapped[str | None] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    start_character: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    end_line: Mapped[int | None] = mapped_column(Integer)
    end_character: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    result: Mapped[ReviewResultRow] = relationship(back_populates="findings")


class Publication(Base):
    __tablename__ = "review_publications"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_review_publications_job"),
        UniqueConstraint("publication_fingerprint", name="uq_review_publications_fingerprint"),
        CheckConstraint(
            f"status IN ({_values(PublicationStatus)})", name="ck_review_publications_status"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_jobs.id", ondelete="CASCADE"), nullable=False
    )
    publication_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PublicationStatus.PENDING.value
    )
    finding_fingerprints: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    gerrit_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    job: Mapped[Job] = relationship(back_populates="publications")
