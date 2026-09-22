"""Per-project review language, snapshotted job policy, and optional vote audit."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_project_review_policy"
down_revision: str | None = "0011_service_heartbeats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_managed_projects",
        sa.Column("review_language", sa.String(16), nullable=False, server_default="INHERIT"),
    )
    op.add_column(
        "review_managed_projects",
        sa.Column(
            "auto_code_review", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "review_managed_projects",
        sa.Column("policy_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_review_managed_projects_language",
        "review_managed_projects",
        "review_language IN ('INHERIT', 'ko-KR', 'en-US')",
    )
    op.create_check_constraint(
        "ck_review_managed_projects_policy_gen", "review_managed_projects", "policy_generation >= 0"
    )
    op.add_column("review_jobs", sa.Column("project_review_policy", postgresql.JSONB()))
    op.create_table(
        "review_votes",
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("review_jobs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("value", sa.Integer()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("finding_count", sa.Integer()),
        sa.Column("account_id", sa.BigInteger()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_payload", postgresql.JSONB(), nullable=False),
        sa.Column("response", postgresql.JSONB()),
        sa.Column("last_error", sa.Text()),
        sa.Column("events", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("value IS NULL OR value IN (0, 1)", name="ck_review_votes_value"),
        sa.CheckConstraint(
            "status IN ('PENDING','AMBIGUOUS','APPLIED','SKIPPED','FAILED')",
            name="ck_review_votes_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_review_votes_attempts"),
    )


def downgrade() -> None:
    # Downgrade is an explicit operator action after draining/stopping every worker.
    op.drop_table("review_votes")
    op.drop_column("review_jobs", "project_review_policy")
    op.drop_constraint("ck_review_managed_projects_policy_gen", "review_managed_projects")
    op.drop_constraint("ck_review_managed_projects_language", "review_managed_projects")
    op.drop_column("review_managed_projects", "policy_generation")
    op.drop_column("review_managed_projects", "auto_code_review")
    op.drop_column("review_managed_projects", "review_language")
