from __future__ import annotations

from dataclasses import dataclass

from prometheus_client import Counter, Gauge, Histogram


@dataclass(frozen=True, slots=True)
class ReviewMetrics:
    jobs_total: Counter
    success_total: Counter
    failed_total: Counter
    duplicate_events_total: Counter
    superseded_total: Counter
    stage_retries_total: Counter
    queue_depth: Gauge
    review_latency_seconds: Histogram
    llm_latency_seconds: Histogram
    gerrit_publish_latency_seconds: Histogram
    llm_input_tokens_total: Counter
    llm_output_tokens_total: Counter
    findings_total: Counter


METRICS = ReviewMetrics(
    jobs_total=Counter("review_jobs_total", "Review jobs created", ["project"]),
    success_total=Counter("review_success_total", "Review jobs completed", ["project"]),
    failed_total=Counter("review_failed_total", "Review jobs failed", ["project", "stage"]),
    duplicate_events_total=Counter(
        "review_duplicate_events_total", "Duplicate Gerrit Patch Set events", ["project"]
    ),
    superseded_total=Counter(
        "review_superseded_jobs_total",
        "Reviews skipped because a newer Patch Set is current",
        ["project"],
    ),
    stage_retries_total=Counter(
        "review_stage_retries_total", "Transient stage retries", ["stage", "reason"]
    ),
    queue_depth=Gauge("review_queue_depth", "Runnable/retry-wait review jobs"),
    review_latency_seconds=Histogram(
        "review_latency_seconds",
        "End-to-end review job latency",
        buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800),
    ),
    llm_latency_seconds=Histogram(
        "review_llm_latency_seconds", "LLM call latency", buckets=(1, 3, 10, 30, 60, 120, 300)
    ),
    gerrit_publish_latency_seconds=Histogram(
        "review_gerrit_publish_latency_seconds",
        "Gerrit REST publish latency",
        buckets=(0.1, 0.5, 1, 2, 5, 10, 30),
    ),
    llm_input_tokens_total=Counter("review_llm_input_tokens_total", "LLM input tokens", ["model"]),
    llm_output_tokens_total=Counter(
        "review_llm_output_tokens_total", "LLM output tokens", ["model"]
    ),
    findings_total=Counter("review_findings_total", "Verified findings", ["severity", "project"]),
)
