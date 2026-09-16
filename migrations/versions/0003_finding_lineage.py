"""track findings across Gerrit patch sets

Revision ID: 0003_finding_lineage
Revises: 0002_service_state
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_finding_lineage"
down_revision: str | None = "0002_service_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_findings", sa.Column("semantic_id", sa.String(length=64), nullable=True))
    op.add_column(
        "review_findings",
        sa.Column(
            "lineage_state",
            sa.String(length=16),
            server_default="NEW",
            nullable=False,
        ),
    )
    op.execute("UPDATE review_findings SET semantic_id = fingerprint WHERE semantic_id IS NULL")
    op.alter_column("review_findings", "semantic_id", nullable=False)
    op.create_check_constraint(
        "ck_review_findings_lineage_state",
        "review_findings",
        "lineage_state IN ('NEW', 'PERSISTING', 'REOPENED')",
    )
    op.create_index(
        "ix_review_findings_semantic_id",
        "review_findings",
        ["semantic_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_review_findings_semantic_id", table_name="review_findings")
    op.drop_constraint(
        "ck_review_findings_lineage_state",
        "review_findings",
        type_="check",
    )
    op.drop_column("review_findings", "lineage_state")
    op.drop_column("review_findings", "semantic_id")
