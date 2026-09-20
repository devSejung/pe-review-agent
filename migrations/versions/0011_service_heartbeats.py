"""Add durable process heartbeat and loaded-config audit state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_service_heartbeats"
down_revision: str | None = "0010_review_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_service_heartbeats",
        sa.Column("component", sa.String(32), nullable=False),
        sa.Column("instance_id", sa.String(64), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("config_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "applied_config_generation",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "details",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("component", "instance_id"),
        sa.CheckConstraint(
            "component IN ('receiver', 'worker', 'reconciler', 'admin')",
            name="ck_review_service_heartbeats_component",
        ),
        sa.CheckConstraint(
            "applied_config_generation >= 0",
            name="ck_review_service_heartbeats_generation",
        ),
    )
    op.create_index(
        "ix_review_service_heartbeats_component_seen",
        "review_service_heartbeats",
        ["component", "last_seen_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_review_service_heartbeats_component_seen",
        table_name="review_service_heartbeats",
    )
    op.drop_table("review_service_heartbeats")
