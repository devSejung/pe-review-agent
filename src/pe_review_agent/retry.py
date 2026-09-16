from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryDecision:
    retryable: bool
    reason: str
    retry_after_seconds: float | None = None


class TransientError(RuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class PermanentError(RuntimeError):
    pass


def exponential_backoff(
    attempt: int,
    *,
    base_seconds: float,
    max_seconds: float,
    jitter_ratio: float,
) -> float:
    raw = min(max_seconds, base_seconds * (2 ** max(0, attempt - 1)))
    if jitter_ratio <= 0:
        return raw
    delta = raw * jitter_ratio
    return max(0.0, random.uniform(raw - delta, raw + delta))
