"""Add round-robin review progress and invocation audit, preserving v1 checkpoint history."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_review_progress"
down_revision: str | None = "0009_chunk_checkpoints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_progress",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("input_key", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["job_id"], ["review_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id", "input_key"),
    )
    op.create_table(
        "review_invocations",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.BigInteger(), nullable=False),
        sa.Column("input_key", sa.String(64), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("work_key", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["job_id"], ["review_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["attempt_id"], ["review_attempts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "phase IN ('candidate', 'verification')", name="ck_review_invocation_phase"
        ),
        sa.CheckConstraint("kind IN ('llm', 'tool')", name="ck_review_invocation_kind"),
        sa.CheckConstraint(
            "status IN ('started', 'completed', 'failed')", name="ck_review_invocation_status"
        ),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0", name="ck_review_invocation_input"
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0", name="ck_review_invocation_output"
        ),
    )
    op.create_index("ix_review_invocations_job", "review_invocations", ["job_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_review_invocations_job", table_name="review_invocations")
    op.drop_table("review_invocations")
    op.drop_table("review_progress")
