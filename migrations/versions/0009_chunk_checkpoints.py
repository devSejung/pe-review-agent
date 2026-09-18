"""add durable candidate chunk checkpoints

Revision ID: 0009_chunk_checkpoints
Revises: 0008_attempt_tool_trace
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_chunk_checkpoints"
down_revision: str | None = "0008_attempt_tool_trace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_chunk_checkpoints",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("checkpoint_version", sa.String(length=64), nullable=False),
        sa.Column("chunk_key", sa.String(length=64), nullable=False),
        sa.Column("parent_chunk_key", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("paths", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("change_summary", sa.Text(), nullable=False),
        sa.Column("findings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("llm_calls", sa.Integer(), nullable=False),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
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
        sa.CheckConstraint(
            "status IN ('DONE', 'SPLIT', 'RETRY')",
            name="ck_review_chunk_checkpoints_status",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["review_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id",
            "checkpoint_version",
            "chunk_key",
            name="uq_review_chunk_checkpoints_identity",
        ),
    )
    op.create_index(
        "ix_review_chunk_checkpoints_job",
        "review_chunk_checkpoints",
        ["job_id"],
        unique=False,
    )
    op.create_index(
        "ix_review_chunk_checkpoints_retention",
        "review_chunk_checkpoints",
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_review_chunk_checkpoints_retention", table_name="review_chunk_checkpoints")
    op.drop_index("ix_review_chunk_checkpoints_job", table_name="review_chunk_checkpoints")
    op.drop_table("review_chunk_checkpoints")
