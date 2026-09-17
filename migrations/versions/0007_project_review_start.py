"""add per-project review start scope

Revision ID: 0007_project_review_start
Revises: 0006_admin_web
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_project_review_start"
down_revision: str | None = "0006_admin_web"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing managed projects are intentionally converted to FROM_NOW at migration time. This
    # prevents a newly upgraded deployment from sweeping a very large historical open-Change set.
    op.add_column(
        "review_managed_projects",
        sa.Column(
            "review_start_mode",
            sa.String(length=16),
            server_default="FROM_NOW",
            nullable=False,
        ),
    )
    op.add_column(
        "review_managed_projects",
        sa.Column(
            "review_start_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_review_managed_projects_start_mode",
        "review_managed_projects",
        "review_start_mode IN ('FROM_NOW', 'INCLUDE_OPEN')",
    )
    op.create_check_constraint(
        "ck_review_managed_projects_start_scope",
        "review_managed_projects",
        "(review_start_mode = 'FROM_NOW' AND review_start_at IS NOT NULL) OR "
        "(review_start_mode = 'INCLUDE_OPEN' AND review_start_at IS NULL)",
    )
    op.drop_constraint("ck_review_jobs_state", "review_jobs", type_="check")
    op.create_check_constraint(
        "ck_review_jobs_state",
        "review_jobs",
        "state IN ('RECEIVED', 'FETCHING', 'REVIEWING', 'VALIDATING', "
        "'READY_TO_PUBLISH', 'PUBLISHING', 'RETRY_WAIT', 'FAILED_TRANSIENT', "
        "'FAILED_PERMANENT', 'SUPERSEDED', 'SKIPPED_SCOPE', 'DONE')",
    )
    op.execute(
        """
        UPDATE review_jobs AS job
        SET state = 'SKIPPED_SCOPE',
            retry_state = NULL,
            lease_owner = NULL,
            lease_expires_at = NULL,
            last_error_class = 'ReviewScopeChanged',
            last_error =
                'Skipped during upgrade because project review scope defaults to FROM_NOW.',
            updated_at = now()
        FROM review_managed_projects AS project
        WHERE job.project = project.project
          AND project.enabled IS TRUE
          AND project.review_start_mode = 'FROM_NOW'
          AND job.created_at < project.review_start_at
          AND job.state NOT IN ('FAILED_PERMANENT', 'SUPERSEDED', 'DONE')
          AND (
              job.lease_owner IS NULL
              OR job.lease_expires_at IS NULL
              OR job.lease_expires_at <= now()
          )
          AND NOT EXISTS (
              SELECT 1 FROM review_publications AS publication
              WHERE publication.job_id = job.id
          )
        """
    )


def downgrade() -> None:
    # Preserve rows on rollback while mapping the new non-work terminal state to the closest legacy
    # terminal state accepted by the previous schema.
    op.execute("UPDATE review_jobs SET state = 'SUPERSEDED' WHERE state = 'SKIPPED_SCOPE'")
    op.drop_constraint("ck_review_jobs_state", "review_jobs", type_="check")
    op.create_check_constraint(
        "ck_review_jobs_state",
        "review_jobs",
        "state IN ('RECEIVED', 'FETCHING', 'REVIEWING', 'VALIDATING', "
        "'READY_TO_PUBLISH', 'PUBLISHING', 'RETRY_WAIT', 'FAILED_TRANSIENT', "
        "'FAILED_PERMANENT', 'SUPERSEDED', 'DONE')",
    )
    op.drop_constraint(
        "ck_review_managed_projects_start_scope",
        "review_managed_projects",
        type_="check",
    )
    op.drop_constraint(
        "ck_review_managed_projects_start_mode",
        "review_managed_projects",
        type_="check",
    )
    op.drop_column("review_managed_projects", "review_start_at")
    op.drop_column("review_managed_projects", "review_start_mode")
