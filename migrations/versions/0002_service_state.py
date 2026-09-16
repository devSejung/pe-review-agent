"""persist service reconciliation state

Revision ID: 0002_service_state
Revises: 0001_durable_review_jobs
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_service_state"
down_revision: str | None = "0001_durable_review_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_service_state",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("timestamp_value", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "json_value",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("review_service_state")
