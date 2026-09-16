"""persist Gerrit comment side for deletion findings

Revision ID: 0004_finding_side
Revises: 0003_finding_lineage
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_finding_side"
down_revision: str | None = "0003_finding_lineage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_findings",
        sa.Column("side", sa.String(length=16), server_default="REVISION", nullable=False),
    )
    op.create_check_constraint(
        "ck_review_findings_side",
        "review_findings",
        "side IN ('REVISION', 'PARENT')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_review_findings_side", "review_findings", type_="check")
    op.drop_column("review_findings", "side")
