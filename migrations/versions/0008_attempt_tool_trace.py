"""add durable repository tool trace to attempts

Revision ID: 0008_attempt_tool_trace
Revises: 0007_project_review_start
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_attempt_tool_trace"
down_revision: str | None = "0007_project_review_start"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_attempts",
        sa.Column(
            "tool_events",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("review_attempts", "tool_events")
