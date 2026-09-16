from __future__ import annotations

from typing import Protocol

from pe_review_agent.domain import ReviewContext, ReviewResult
from pe_review_agent.repos.tools import RepositoryToolExecutor


class ReviewEngine(Protocol):
    async def review(
        self, context: ReviewContext, tools: RepositoryToolExecutor
    ) -> ReviewResult: ...
