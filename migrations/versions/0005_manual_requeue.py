"""add retry epoch for safe manual requeue

Revision ID: 0005_manual_requeue
Revises: 0004_finding_side
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_manual_requeue"
down_revision: str | None = "0004_finding_side"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_jobs",
        sa.Column(
            "retry_epoch_start_attempt",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_review_jobs_retry_epoch",
        "review_jobs",
        "retry_epoch_start_attempt >= 0 AND retry_epoch_start_attempt <= attempt_count",
    )


def downgrade() -> None:
    op.drop_constraint("ck_review_jobs_retry_epoch", "review_jobs", type_="check")
    op.drop_column("review_jobs", "retry_epoch_start_attempt")
