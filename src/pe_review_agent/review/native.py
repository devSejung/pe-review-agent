from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from pe_review_agent.config import ReviewSettings
from pe_review_agent.domain import Finding, ReviewContext, ReviewResult
from pe_review_agent.llm.client import LlmClient, assistant_message_for_tool_loop
from pe_review_agent.repos.tools import RepositoryToolExecutor
from pe_review_agent.retry import TransientError
from pe_review_agent.review.validator import FindingValidator


class _ModelReview(BaseModel):
    summary: str = ""
    findings: list[Finding] = Field(default_factory=list)


class NativeFirmwareReviewEngine:
    """Two-pass firmware reviewer with repository tool use and strict local validation."""

    def __init__(self, llm: LlmClient, settings: ReviewSettings) -> None:
        self.llm = llm
        self.settings = settings
        self.validator = FindingValidator(settings)

    async def review(self, context: ReviewContext, tools: RepositoryToolExecutor) -> ReviewResult:
        candidate_messages = self._candidate_messages(context)
        candidate, candidate_usage = await self._tool_session(candidate_messages, tools)
        candidates = self._parse_review(candidate.content, stage="candidate")

        verify_messages = self._verification_messages(context, candidates)
        verified_completion, verified_usage = await self._tool_session(verify_messages, tools)
        verified = self._parse_review(verified_completion.content, stage="verification")
        findings = self.validator.validate(context, verified.findings)

        if findings:
            summary = verified.summary.strip() or self._fallback_summary(findings)
        else:
            summary = "No actionable firmware correctness issues were found in this Patch Set."

        return ReviewResult(
            summary=summary,
            findings=findings,
            model=self.llm.settings.model,
            input_tokens=candidate_usage[0] + verified_usage[0],
            output_tokens=candidate_usage[1] + verified_usage[1],
            review_metadata={
                "engine": "native-firmware-v1",
                "candidate_count": len(candidates.findings),
                "verified_model_count": len(verified.findings),
                "published_candidate_count": len(findings),
            },
        )

    async def _tool_session(
        self, messages: list[dict[str, Any]], tools: RepositoryToolExecutor
    ) -> tuple[Any, tuple[int, int]]:
        input_tokens = 0
        output_tokens = 0
        transcript = list(messages)
        for round_index in range(self.settings.max_tool_rounds + 1):
            completion = await self.llm.complete(messages=transcript, tools=tools.tool_schemas)
            input_tokens += completion.input_tokens or 0
            output_tokens += completion.output_tokens or 0
            if not completion.tool_calls:
                return completion, (input_tokens, output_tokens)
            if round_index >= self.settings.max_tool_rounds:
                raise TransientError("review model exceeded configured repository tool-call rounds")
            transcript.append(assistant_message_for_tool_loop(completion))
            for call in completion.tool_calls:
                try:
                    result = await tools.execute(call.name, call.arguments)
                except (ValueError, OSError) as exc:
                    result = json.dumps({"error": str(exc)})
                transcript.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result,
                    }
                )
        raise AssertionError("unreachable")

    def _candidate_messages(self, context: ReviewContext) -> list[dict[str, Any]]:
        changed_line_index = "\n".join(
            f"{item.path}:{item.line}: {item.text}" for item in context.changed_lines[:2000]
        )
        user = f"""\
Review Gerrit change {context.change_number}, Patch Set {context.patchset_number}, revision
{context.revision_sha} in project {context.project}.

Repository policy:
{context.policy_text}

Changed files:
{chr(10).join(context.changed_files)}

Changed-line index (valid inline anchors):
{changed_line_index}

Patch diff:
{context.diff}

Actively search for concrete correctness defects. Use repository tools to inspect definitions,
callers/callees, register macros, headers, and tests when needed. Do not guess. Findings must anchor
their start_line to a changed line from the index above.

Return ONLY a JSON object with this shape:
{{
  "summary": "one short review summary",
  "findings": [
    {{
      "severity": "P0|P1|P2",
      "category": "correctness category",
      "title": "concise defect title",
      "message": "what is wrong and when it triggers",
      "impact": "concrete consequence",
      "evidence": "specific code/contract evidence",
      "remediation": "practical direction or null",
      "location": {{
        "path": "repo/relative/file.c",
        "start_line": 123,
        "start_character": 0,
        "end_line": 123,
        "end_character": 8
      }},
      "confidence": 0.0
    }}
  ]
}}
"""
        return [
            {
                "role": "system",
                "content": (
                    "You are a senior ARM/embedded firmware reviewer. Prioritize real runtime "
                    "defects and high signal. You have read-only repository tools; use them before "
                    "making claims that depend on code outside the diff. Never invent register "
                    "semantics or API contracts. JSON only when done."
                ),
            },
            {"role": "user", "content": user},
        ]

    def _verification_messages(
        self, context: ReviewContext, candidates: _ModelReview
    ) -> list[dict[str, Any]]:
        candidate_json = candidates.model_dump_json(indent=2)
        changed_line_index = "\n".join(
            f"{item.path}:{item.line}: {item.text}" for item in context.changed_lines[:2000]
        )
        return [
            {
                "role": "system",
                "content": (
                    "You are the independent verifier for an automated firmware review. Be "
                    "skeptical. Remove speculative, stylistic, duplicate, pre-existing, or "
                    "unsupported findings. Use repository tools to disprove candidates whenever "
                    "possible. A publishable issue needs a concrete trigger, impact, and code "
                    "evidence. Prefer zero findings over a false positive. JSON only when done."
                ),
            },
            {
                "role": "user",
                "content": f"""\
Policy:
{context.policy_text}

Valid changed-line anchors:
{changed_line_index}

Candidate review:
{candidate_json}

Verify every candidate against the repository and changed code. Return only the same JSON schema as
the candidate review, containing only findings that survive verification. Correct inaccurate line
ranges to a valid changed-line anchor if the defect is real. Set confidence conservatively.
""",
            },
        ]

    def _parse_review(self, content: str, *, stage: str) -> _ModelReview:
        raw = _extract_json_object(content)
        try:
            data = json.loads(raw)
            return _ModelReview.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise TransientError(
                f"{stage} model output was not valid review JSON: {content[:1500]}"
            ) from exc

    @staticmethod
    def _fallback_summary(findings: list[Finding]) -> str:
        counts: dict[str, int] = {}
        for finding in findings:
            counts[finding.severity.value] = counts.get(finding.severity.value, 0) + 1
        detail = ", ".join(f"{key}: {value}" for key, value in sorted(counts.items()))
        return f"Found {len(findings)} actionable correctness issue(s) ({detail})."


def _extract_json_object(content: str) -> str:
    value = content.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        value = fence.group(1).strip()
    if value.startswith("{") and value.endswith("}"):
        return value
    start = value.find("{")
    end = value.rfind("}")
    if start >= 0 and end > start:
        return value[start : end + 1]
    return value
