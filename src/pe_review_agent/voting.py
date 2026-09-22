from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta

from pe_review_agent.config import Settings
from pe_review_agent.domain import JobState, ReviewResult
from pe_review_agent.gerrit.client import GerritRestClient, SupersededRevisionError
from pe_review_agent.jobs.store import JobRecord, JobStore
from pe_review_agent.jobs.votes import TERMINAL_VOTE_STATES, VoteStore
from pe_review_agent.retry import PermanentError, TransientError, exponential_backoff
from pe_review_agent.review.project_policy import ProjectReviewPolicy, VoteDecision


def vote_payload(
    job: JobRecord,
    policy: ProjectReviewPolicy,
    decision: VoteDecision,
    review: ReviewResult,
    tag: str,
) -> dict:
    if decision.value is None:
        return {}
    partial = review.review_metadata.get("lineage_complete") is False
    if policy.output_language == "ko-KR":
        explanation = (
            f"AI 리뷰 투표: 유효 finding {decision.finding_count}건 → Code-Review "
            f"{'+' if decision.value else ''}{decision.value}."
        )
        if partial:
            explanation += " 부분 검토 결과에 따른 투표이며 전체 검토 완료를 의미하지 않습니다."
        explanation += " 사람의 코드 리뷰를 대체하지 않습니다."
    else:
        explanation = (
            f"AI review vote: {decision.finding_count} validated finding(s) → Code-Review "
            f"{'+' if decision.value else ''}{decision.value}."
        )
        if partial:
            explanation += " This vote uses a partial review, not a completed full review."
        explanation += " This does not replace human code review."
    return {
        "message": f"{explanation}\n\n[review-vote:{job.id}]",
        "tag": f"{tag}~vote~{job.id}",
        "labels": {"Code-Review": decision.value},
        "notify": "NONE",
    }


class VotePublisher:
    """Bounded, lease-fenced voting, with independent audit and no comment/model replay."""

    def __init__(
        self,
        settings: Settings,
        jobs: JobStore,
        gerrit: GerritRestClient,
    ) -> None:
        self.settings = settings
        self.jobs = jobs
        self.votes = VoteStore(jobs._sessions)
        self.gerrit = gerrit

    async def process(
        self,
        job: JobRecord,
        *,
        worker_id: str,
        guard: Callable[[], Awaitable[bool]],
        allow_vote: bool = True,
    ) -> bool:
        vote = await self.votes.get(job.id)
        if vote is None:
            raise RuntimeError("vote intent must exist before processing")

        async def finish(
            status: str,
            error: str | None = None,
            response: dict | None = None,
            account_id: int | None = None,
        ) -> bool:
            await self.votes.finish(
                job.id,
                worker_id=worker_id,
                status=status,
                error=error,
                response=response,
                account_id=account_id,
            )
            return True

        if vote.status in TERMINAL_VOTE_STATES:
            return await finish(vote.status)
        if not allow_vote:
            return await finish(
                "SKIPPED",
                "Patch Set superseded; no new vote sent. "
                "Any earlier unconfirmed vote request remains in the audit.",
            )
        posted = False
        try:
            account_id = await self.gerrit.authenticated_account_id()
            if vote.account_id is not None and vote.account_id != account_id:
                return await finish("FAILED", "Gerrit bot account changed; vote was not replayed.")
            observation = await self.gerrit.code_review_vote_observation(
                project=job.project,
                change_number=job.change_number,
                revision_sha=job.revision_sha,
                patchset_number=job.patchset_number,
                account_id=account_id,
                tag=vote.request_payload["tag"],
                marker=vote.request_payload["message"],
                target=vote.value,
            )
            if observation.marker_found:
                if observation.label_exists and observation.current_value == vote.value:
                    return await finish(
                        "APPLIED",
                        response={"recovered": True, "labels": {"Code-Review": vote.value}},
                        account_id=account_id,
                    )
                return await finish(
                    "FAILED",
                    "The vote message exists but the current bot label "
                    "differs or was not applied. It was not overwritten.",
                )
            # A matching value without our unique marker may have been copied from an older
            # Patch Set. Publish this review's own decision, including an explicit neutral 0.
            if not observation.can_vote:
                return await finish(
                    "FAILED",
                    "Code-Review label is absent or the bot lacks "
                    "permission for the requested value.",
                    account_id=account_id,
                )
            limit = self.settings.retry.publish_attempts
            if vote.attempts >= limit or vote.retry_count >= limit:
                return await finish(
                    "FAILED",
                    "Vote retry budget exhausted. An earlier request "
                    "may be unconfirmed; review comments remain published.",
                )

            async def pre_post_guard() -> bool:
                return await guard()

            if not await pre_post_guard():
                return await finish("SKIPPED", "A newer Patch Set exists; vote not sent.")
            vote = await self.votes.dispatch(job.id, worker_id=worker_id, account_id=account_id)
            posted = True
            response = await self.gerrit.publish_review_input(
                project=job.project,
                change_number=job.change_number,
                revision_sha=job.revision_sha,
                payload=vote.request_payload,
                pre_post_guard=pre_post_guard,
            )
            labels = response.get("labels")
            if (
                isinstance(labels, Mapping)
                and type(labels.get("Code-Review")) is int
                and labels["Code-Review"] == vote.value
            ):
                return await finish(
                    "APPLIED", response={"labels": dict(labels)}, account_id=account_id
                )
            return await finish(
                "FAILED",
                "Gerrit did not confirm the requested Code-Review value; "
                "the label may have been ignored. Review comments remain published.",
            )
        except SupersededRevisionError:
            return await finish("SKIPPED", "Patch Set superseded; no further vote requests sent.")
        except PermanentError as exc:
            return await finish("FAILED", str(exc))
        except TransientError as exc:
            vote = await self.votes.retry(job.id, worker_id=worker_id, error=str(exc))
            # Permit one read-only recovery after the last ambiguous POST, never another write.
            if vote.retry_count >= self.settings.retry.publish_attempts and not posted:
                return await finish(
                    "FAILED", f"Vote outcome unconfirmed after retry exhaustion: {exc}"
                )
            delay = exc.retry_after_seconds
            if delay is None:
                delay = exponential_backoff(
                    vote.retry_count,
                    base_seconds=self.settings.retry.base_seconds,
                    max_seconds=self.settings.retry.max_seconds,
                    jitter_ratio=self.settings.retry.jitter_ratio,
                )
            await self.jobs.schedule_retry(
                job.id,
                resume_state=JobState.PUBLISHING,
                retry_at=datetime.now(UTC) + timedelta(seconds=max(1.0, delay)),
                error=TransientError(f"Review comments posted; Code-Review vote pending: {exc}"),
                worker_id=worker_id,
            )
            return False
