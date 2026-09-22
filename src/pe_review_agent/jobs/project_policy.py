from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pe_review_agent.domain import AttemptStage
from pe_review_agent.jobs.models import Attempt, Job, ManagedProject
from pe_review_agent.jobs.store import _require_live_lease
from pe_review_agent.review.project_policy import ProjectReviewPolicy


async def bind_project_review_policy(
    sessions: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    *,
    worker_id: str,
    default_language: str,
) -> ProjectReviewPolicy:
    """Bind once immediately before review, surviving retries and settings changes.

    Already-started pre-feature jobs preserve the old global language and stay comment-only.
    No project/global settings are mutated. Language remains part of the engine cache identity.
    """

    async with sessions.begin() as session:
        job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None:
            raise KeyError(job_id)
        _require_live_lease(job, worker_id)
        if job.project_review_policy is not None:
            return ProjectReviewPolicy.model_validate(job.project_review_policy)
        legacy = bool(
            await session.scalar(
                select(
                    exists().where(
                        Attempt.job_id == job_id, Attempt.stage == AttemptStage.REVIEW.value
                    )
                )
            )
        )
        project = await session.get(ManagedProject, job.project)
        language = project.review_language if project and not legacy else "INHERIT"
        policy = ProjectReviewPolicy(
            review_language=language,
            output_language=default_language if language == "INHERIT" else language,
            auto_code_review=bool(project and project.auto_code_review and not legacy),
            generation=project.policy_generation if project else 0,
            source="legacy" if legacy else "project" if project else "default",
            bound_at=datetime.now(UTC),
        )
        job.project_review_policy = policy.model_dump(mode="json")
        return policy
