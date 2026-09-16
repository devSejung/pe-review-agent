"""create durable review job state

Revision ID: 0001_durable_review_jobs
Revises:
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_durable_review_jobs"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


JOB_STATES = (
    "RECEIVED",
    "FETCHING",
    "REVIEWING",
    "VALIDATING",
    "READY_TO_PUBLISH",
    "PUBLISHING",
    "RETRY_WAIT",
    "FAILED_TRANSIENT",
    "FAILED_PERMANENT",
    "SUPERSEDED",
    "DONE",
)
ATTEMPT_STAGES = ("FETCH", "REVIEW", "VALIDATE", "PUBLISH", "RECONCILE")
SEVERITIES = ("P0", "P1", "P2")
PUBLICATION_STATUSES = ("PENDING", "POSTED", "AMBIGUOUS", "FAILED")
RETRY_TARGET_STATES = (
    "FETCHING",
    "REVIEWING",
    "VALIDATING",
    "READY_TO_PUBLISH",
    "PUBLISHING",
)


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(repr(value) for value in values)


def upgrade() -> None:
    op.create_table(
        "review_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project", sa.String(length=512), nullable=False),
        sa.Column("change_number", sa.BigInteger(), nullable=False),
        sa.Column("patchset_number", sa.Integer(), nullable=False),
        sa.Column("revision_sha", sa.String(length=64), nullable=False),
        sa.Column("review_policy_version", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("retry_state", sa.String(length=32), nullable=True),
        sa.Column("event_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_class", sa.String(length=255), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("superseded_by_job_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(f"state IN ({_in(JOB_STATES)})", name="ck_review_jobs_state"),
        sa.CheckConstraint(
            f"retry_state IS NULL OR retry_state IN ({_in(RETRY_TARGET_STATES)})",
            name="ck_review_jobs_retry_state",
        ),
        sa.ForeignKeyConstraint(["superseded_by_job_id"], ["review_jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project",
            "change_number",
            "revision_sha",
            "review_policy_version",
            name="uq_review_jobs_identity",
        ),
    )
    op.create_index(
        "ix_review_jobs_claim",
        "review_jobs",
        ["state", "next_attempt_at", "lease_expires_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_review_jobs_change",
        "review_jobs",
        ["project", "change_number", "patchset_number"],
        unique=False,
    )

    op.create_table(
        "review_attempts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("worker_id", sa.String(length=255), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column("error_class", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(f"stage IN ({_in(ATTEMPT_STAGES)})", name="ck_review_attempts_stage"),
        sa.ForeignKeyConstraint(["job_id"], ["review_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "attempt_number", name="uq_review_attempts_number"),
    )
    op.create_index(
        "ix_review_attempts_job_stage",
        "review_attempts",
        ["job_id", "stage"],
        unique=False,
    )

    op.create_table(
        "review_results",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("review_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["review_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id"),
    )

    op.create_table(
        "review_findings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=8), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("impact", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("remediation", sa.Text(), nullable=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("start_character", sa.Integer(), server_default="0", nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=True),
        sa.Column("end_character", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_review_findings_confidence",
        ),
        sa.CheckConstraint(f"severity IN ({_in(SEVERITIES)})", name="ck_review_findings_severity"),
        sa.ForeignKeyConstraint(["job_id"], ["review_results.job_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "fingerprint", name="uq_review_findings_fingerprint"),
        sa.UniqueConstraint("job_id", "ordinal", name="uq_review_findings_ordinal"),
    )

    op.create_table(
        "review_publications",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("publication_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("finding_fingerprints", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("gerrit_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"status IN ({_in(PUBLICATION_STATUSES)})", name="ck_review_publications_status"
        ),
        sa.ForeignKeyConstraint(["job_id"], ["review_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", name="uq_review_publications_job"),
        sa.UniqueConstraint("publication_fingerprint", name="uq_review_publications_fingerprint"),
    )


def downgrade() -> None:
    op.drop_table("review_publications")
    op.drop_table("review_findings")
    op.drop_table("review_results")
    op.drop_index("ix_review_attempts_job_stage", table_name="review_attempts")
    op.drop_table("review_attempts")
    op.drop_index("ix_review_jobs_change", table_name="review_jobs")
    op.drop_index("ix_review_jobs_claim", table_name="review_jobs")
    op.drop_table("review_jobs")
