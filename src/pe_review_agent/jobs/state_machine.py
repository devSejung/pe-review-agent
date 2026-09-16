from __future__ import annotations

from pe_review_agent.domain import TERMINAL_JOB_STATES, JobState


class InvalidJobTransition(ValueError):
    pass


_ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.RECEIVED: frozenset(
        {
            JobState.FETCHING,
            JobState.RETRY_WAIT,
            JobState.FAILED_TRANSIENT,
            JobState.FAILED_PERMANENT,
            JobState.SUPERSEDED,
        }
    ),
    JobState.FETCHING: frozenset(
        {
            JobState.REVIEWING,
            JobState.RETRY_WAIT,
            JobState.FAILED_TRANSIENT,
            JobState.FAILED_PERMANENT,
            JobState.SUPERSEDED,
        }
    ),
    JobState.REVIEWING: frozenset(
        {
            JobState.VALIDATING,
            JobState.RETRY_WAIT,
            JobState.FAILED_TRANSIENT,
            JobState.FAILED_PERMANENT,
            JobState.SUPERSEDED,
        }
    ),
    JobState.VALIDATING: frozenset(
        {
            JobState.READY_TO_PUBLISH,
            JobState.RETRY_WAIT,
            JobState.FAILED_TRANSIENT,
            JobState.FAILED_PERMANENT,
            JobState.SUPERSEDED,
        }
    ),
    JobState.READY_TO_PUBLISH: frozenset(
        {
            JobState.PUBLISHING,
            JobState.RETRY_WAIT,
            JobState.FAILED_TRANSIENT,
            JobState.FAILED_PERMANENT,
            JobState.SUPERSEDED,
        }
    ),
    JobState.PUBLISHING: frozenset(
        {
            JobState.DONE,
            JobState.READY_TO_PUBLISH,
            JobState.RETRY_WAIT,
            JobState.FAILED_TRANSIENT,
            JobState.FAILED_PERMANENT,
            JobState.SUPERSEDED,
        }
    ),
    JobState.RETRY_WAIT: frozenset(
        {
            JobState.FETCHING,
            JobState.REVIEWING,
            JobState.VALIDATING,
            JobState.READY_TO_PUBLISH,
            JobState.PUBLISHING,
            JobState.FAILED_PERMANENT,
            JobState.SUPERSEDED,
        }
    ),
    JobState.FAILED_TRANSIENT: frozenset(
        {
            JobState.RETRY_WAIT,
            JobState.FETCHING,
            JobState.REVIEWING,
            JobState.VALIDATING,
            JobState.READY_TO_PUBLISH,
            JobState.PUBLISHING,
            JobState.FAILED_PERMANENT,
            JobState.SUPERSEDED,
        }
    ),
    JobState.FAILED_PERMANENT: frozenset(),
    JobState.SUPERSEDED: frozenset(),
    JobState.DONE: frozenset(),
}


def can_transition(current: JobState, target: JobState) -> bool:
    if current == target:
        return True
    return target in _ALLOWED_TRANSITIONS[current]


def require_transition(current: JobState, target: JobState) -> None:
    if not can_transition(current, target):
        raise InvalidJobTransition(f"invalid job transition: {current.value} -> {target.value}")


def is_terminal(state: JobState) -> bool:
    return state in TERMINAL_JOB_STATES


def valid_retry_target(state: JobState) -> bool:
    return state in {
        JobState.FETCHING,
        JobState.REVIEWING,
        JobState.VALIDATING,
        JobState.READY_TO_PUBLISH,
        JobState.PUBLISHING,
    }
