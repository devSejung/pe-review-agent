from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

from pe_review_agent.config import GerritSettings
from pe_review_agent.domain import Finding, GerritPatchsetEvent, ReviewResult
from pe_review_agent.retry import PermanentError, TransientError

from .allowlist import ProjectAllowlist

_XSSI_PREFIX = ")]}'"
_PATCH_SET_PREFIX = re.compile(r"^\s*Patch\s+Set\s+\d+\s*:\s*", re.IGNORECASE)
_REVIEW_DECORATION_PREFIX = re.compile(
    r"^(?:\([^\n]*\bcomments?\)|(?:[A-Za-z][^:\n]*:\s*[+-]?\d+(?:,\s*)?)+)\s*",
    re.IGNORECASE,
)


class SupersededRevisionError(PermanentError):
    def __init__(self, *, project: str, change_number: int, expected: str, actual: str) -> None:
        super().__init__(
            f"Gerrit change {project}~{change_number} moved from revision {expected} to {actual}"
        )
        self.project = project
        self.change_number = change_number
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True, slots=True)
class GerritChange:
    project: str
    change_number: int
    current_revision: str
    patchset_number: int
    ref: str | None
    status: str
    branch: str | None
    change_id: str | None
    subject: str | None
    updated: str | None
    raw: dict[str, Any]


class GerritRestClient:
    def __init__(
        self,
        settings: GerritSettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._allowlist = ProjectAllowlist(settings.projects)
        self._auth: httpx.Auth | None = None
        self._headers: dict[str, str] = {"Accept": "application/json"}

        auth = settings.rest_auth
        if auth.mode == "basic":
            secret = auth.secret()
            if not auth.username or not secret:
                raise PermanentError(
                    "Gerrit basic REST auth requires username and configured password secret"
                )
            self._auth = httpx.BasicAuth(auth.username, secret)
        elif auth.mode == "bearer":
            secret = auth.secret()
            if not secret:
                raise PermanentError("Gerrit bearer REST auth requires configured token secret")
            self._headers["Authorization"] = f"Bearer {secret}"

        self._client = client or httpx.AsyncClient(
            follow_redirects=True,
            verify=str(settings.ca_bundle_path) if settings.ca_bundle_path else True,
        )
        self._owns_client = client is None

        root = settings.rest_url.rstrip("/")
        if auth.mode != "none" and not root.endswith("/a"):
            root = f"{root}/a"
        self._api_root = root

    def replace_projects(self, projects: list[str] | tuple[str, ...]) -> None:
        """Refresh the exact project allowlist from the durable control-plane configuration."""

        self._allowlist = ProjectAllowlist(projects)

    async def server_version(self) -> str:
        payload = await self._request_json("GET", "config/server/version")
        if not isinstance(payload, str) or not payload:
            raise TransientError("Gerrit server version endpoint returned an invalid response")
        return payload

    async def project_head(self, project: str) -> str:
        self._allowlist.require(project)
        payload = await self._request_json("GET", f"projects/{quote(project, safe='')}/HEAD")
        if not isinstance(payload, str) or not payload:
            raise TransientError("Gerrit project HEAD endpoint returned an invalid response")
        return payload

    async def __aenter__(self) -> GerritRestClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_change(self, project: str, change_number: int) -> GerritChange:
        self._allowlist.require(project)
        payload = await self._request_json(
            "GET",
            f"changes/{_change_identifier(project, change_number)}/detail",
            params=[("o", "CURRENT_REVISION")],
        )
        if not isinstance(payload, Mapping):
            raise TransientError("Gerrit Get Change returned a non-object JSON response")
        return _parse_change(payload, expected_project=project, expected_number=change_number)

    async def get_revision_commit(
        self,
        project: str,
        change_number: int,
        revision_sha: str,
    ) -> dict[str, Any]:
        self._allowlist.require(project)
        payload = await self._request_json(
            "GET",
            f"changes/{_change_identifier(project, change_number)}/revisions/"
            f"{quote(revision_sha, safe='')}/commit",
        )
        if not isinstance(payload, Mapping):
            raise TransientError("Gerrit Get Commit returned a non-object JSON response")
        return dict(payload)

    async def ensure_current_revision(
        self,
        project: str,
        change_number: int,
        revision_sha: str,
    ) -> GerritChange:
        change = await self.get_change(project, change_number)
        if change.status != "NEW":
            raise PermanentError(
                f"Gerrit change {project}~{change_number} is not open (status={change.status})"
            )
        if change.current_revision != revision_sha:
            raise SupersededRevisionError(
                project=project,
                change_number=change_number,
                expected=revision_sha,
                actual=change.current_revision,
            )
        return change

    async def is_current_revision(
        self,
        project: str,
        change_number: int,
        revision_sha: str,
    ) -> bool:
        change = await self.get_change(project, change_number)
        return change.status == "NEW" and change.current_revision == revision_sha

    async def publish_review(
        self,
        *,
        project: str,
        change_number: int,
        revision_sha: str,
        review: ReviewResult,
    ) -> dict[str, Any]:
        payload = build_review_input(
            review,
            tag=self._settings.review_tag,
            notify=self._settings.notify,
            max_comment_bytes=self._settings.max_comment_bytes,
        )
        return await self.publish_review_input(
            project=project,
            change_number=change_number,
            revision_sha=revision_sha,
            payload=payload,
        )

    async def publish_review_input(
        self,
        *,
        project: str,
        change_number: int,
        revision_sha: str,
        payload: Mapping[str, Any],
        pre_post_guard: Callable[[], Awaitable[bool]] | None = None,
    ) -> dict[str, Any]:
        """Publish a persisted ReviewInput without rebuilding or mutating its payload."""

        # Keep this check next to the POST even when the payload was durably built much earlier.
        # A newer patch set must supersede the job before any comments leave this process.
        await self.ensure_current_revision(project, change_number, revision_sha)
        if pre_post_guard is not None and not await pre_post_guard():
            raise SupersededRevisionError(
                project=project,
                change_number=change_number,
                expected=revision_sha,
                actual="newer-patchset-known-by-worker",
            )
        response = await self._request_json(
            "POST",
            f"changes/{_change_identifier(project, change_number)}/revisions/"
            f"{quote(revision_sha, safe='')}/review",
            json_body=payload,
        )
        if response is None:
            return {}
        if not isinstance(response, Mapping):
            raise TransientError("Gerrit Set Review returned a non-object JSON response")
        return dict(response)

    async def has_published_review(
        self,
        *,
        project: str,
        change_number: int,
        patchset_number: int,
        summary: str,
        tag: str | None = None,
    ) -> bool:
        """Resolve an ambiguous POST by matching this bot's patch-set change message."""

        self._allowlist.require(project)
        if patchset_number < 1:
            raise ValueError("patchset_number must be positive")
        expected_message = _normalize_review_message(summary)
        expected_tag = tag or self._settings.review_tag
        if not expected_message:
            return False

        payload = await self._request_json(
            "GET",
            f"changes/{_change_identifier(project, change_number)}/messages",
        )
        if not isinstance(payload, list):
            raise TransientError("Gerrit List Change Messages returned a non-array JSON response")

        for item in payload:
            if not isinstance(item, Mapping):
                raise TransientError("Gerrit List Change Messages returned a malformed message")
            if item.get("tag") != expected_tag:
                continue
            if _message_revision_number(item) != patchset_number:
                continue
            message = item.get("message")
            if isinstance(message, str) and _normalize_review_message(message) == expected_message:
                return True
        return False

    async def query_recent_open_changes(
        self,
        *,
        since: datetime | None,
        page_size: int = 100,
    ) -> list[GerritChange]:
        if since is not None and since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        if page_size < 1:
            raise ValueError("page_size must be positive")

        since_utc = since.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S %z") if since else None
        changes: list[GerritChange] = []
        for project in self._allowlist.projects:
            base_query = f"status:open project:{_query_value(project)}"
            if since_utc:
                base_query += f' after:"{since_utc}"'
            before_boundary: str | None = None
            excluded_at_boundary: set[int] = set()
            seen_change_numbers: set[int] = set()
            while True:
                query = base_query
                if before_boundary is not None:
                    query += f' before:"{before_boundary}"'
                    for change_number in sorted(excluded_at_boundary):
                        query += f" -change:{change_number}"
                if len(query.encode("utf-8")) > 64_000:
                    raise TransientError(
                        "Gerrit reconciliation has too many changes sharing one update-second "
                        "boundary to continue safely without offset pagination"
                    )
                payload = await self._request_json(
                    "GET",
                    "changes/",
                    params=[
                        ("q", query),
                        ("n", str(page_size)),
                        ("o", "CURRENT_REVISION"),
                    ],
                )
                if not isinstance(payload, list):
                    raise TransientError("Gerrit Query Changes returned a non-array JSON response")
                if not payload:
                    break

                for item in payload:
                    if not isinstance(item, Mapping):
                        raise TransientError("Gerrit Query Changes returned a malformed change")
                    number = _required_positive_int(item, "_number")
                    parsed = _parse_change(
                        item,
                        expected_project=project,
                        expected_number=number,
                    )
                    if number not in seen_change_numbers:
                        changes.append(parsed)
                        seen_change_numbers.add(number)

                if not bool(payload[-1].get("_more_changes")):
                    break

                oldest_updated = payload[-1].get("updated")
                if not isinstance(oldest_updated, str):
                    raise TransientError(
                        "Gerrit Query Changes pagination requires an updated timestamp"
                    )
                next_boundary = _gerrit_search_boundary(oldest_updated)
                if before_boundary != next_boundary:
                    before_boundary = next_boundary
                    excluded_at_boundary.clear()
                for item in payload:
                    updated = item.get("updated") if isinstance(item, Mapping) else None
                    number = item.get("_number") if isinstance(item, Mapping) else None
                    if (
                        isinstance(updated, str)
                        and isinstance(number, int)
                        and _gerrit_search_boundary(updated) == before_boundary
                    ):
                        excluded_at_boundary.add(number)
        return changes

    async def reconciliation_events(
        self,
        *,
        since: datetime | None,
        page_size: int = 100,
    ) -> list[GerritPatchsetEvent]:
        changes = await self.query_recent_open_changes(since=since, page_size=page_size)
        events: list[GerritPatchsetEvent] = []
        for change in changes:
            revision_info = change.raw.get("revisions", {}).get(change.current_revision, {})
            uploader = revision_info.get("uploader") if isinstance(revision_info, Mapping) else None
            raw = {
                "type": "reconciliation",
                "change": change.raw,
                "patchSet": dict(revision_info) if isinstance(revision_info, Mapping) else {},
            }
            events.append(
                GerritPatchsetEvent(
                    project=change.project,
                    change_number=change.change_number,
                    patchset_number=change.patchset_number,
                    revision_sha=change.current_revision,
                    ref=change.ref,
                    branch=change.branch,
                    change_id=change.change_id,
                    uploader=_account_name(uploader),
                    raw=raw,
                )
            )
        return events

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, str]] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> Any:
        url = f"{self._api_root}/{path.lstrip('/')}"
        try:
            response = await self._client.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=self._headers,
                auth=self._auth,
                timeout=self._settings.request_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise TransientError(f"Gerrit REST {method} timed out") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"Gerrit REST {method} transport failure: {exc}") from exc

        status = response.status_code
        if status == 408 or status == 429 or 500 <= status <= 599:
            raise TransientError(
                f"Gerrit REST {method} returned HTTP {status}",
                retry_after_seconds=_retry_after_seconds(response),
            )
        if status >= 400 or status < 200 or status >= 300:
            raise PermanentError(f"Gerrit REST {method} returned HTTP {status}")
        if status == 204 or not response.content:
            return None

        try:
            return _decode_gerrit_json(response.text)
        except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
            raise TransientError(
                f"Gerrit REST {method} returned invalid JSON (HTTP {status})"
            ) from exc


def build_review_input(
    review: ReviewResult,
    *,
    tag: str = "autogenerated:pe-ai-review",
    notify: str = "OWNER",
    max_comment_bytes: int = 16 * 1024,
) -> dict[str, Any]:
    if max_comment_bytes < 1024:
        raise ValueError("max_comment_bytes must be at least 1024")
    comments: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for finding in review.findings:
        location = finding.location
        comment: dict[str, Any] = {
            "message": _truncate_utf8_comment(_finding_message(finding), max_comment_bytes),
            "side": location.side.value,
        }
        if location.end_line is None:
            comment["line"] = location.start_line
        else:
            comment["range"] = {
                "start_line": location.start_line,
                "start_character": location.start_character,
                "end_line": location.end_line,
                "end_character": location.end_character,
            }
        comments[location.path].append(comment)

    payload: dict[str, Any] = {
        "message": _truncate_utf8_comment(review.summary, max_comment_bytes),
        "tag": tag,
        "notify": notify,
        "omit_duplicate_comments": True,
    }
    if comments:
        payload["comments"] = dict(comments)
    return payload


def _finding_message(finding: Finding) -> str:
    sections = [f"[{finding.severity}] {finding.title}", finding.message]
    if finding.impact:
        sections.append(f"Impact: {finding.impact}")
    if finding.evidence:
        sections.append(f"Evidence: {finding.evidence}")
    if finding.remediation:
        sections.append(f"Suggested fix: {finding.remediation}")
    return "\n\n".join(sections)


def _truncate_utf8_comment(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    marker = "\n\n[truncated by review bot to Gerrit comment size limit]"
    marker_bytes = marker.encode("utf-8")
    keep = max(0, max_bytes - len(marker_bytes))
    prefix = encoded[:keep].decode("utf-8", errors="ignore")
    return prefix + marker


def _normalize_review_message(value: str) -> str:
    # Gerrit commonly prefixes review change messages with "Patch Set N:". The revision number
    # is matched separately, so strip only that presentation prefix and normalize whitespace.
    normalized = _PATCH_SET_PREFIX.sub("", value, count=1).lstrip()
    # Set Review change messages may prepend generated metadata such as "(1 comment)" or label
    # votes before ReviewInput.message. Recovery keys on the exact bot tag + patch set, then strips
    # only these Gerrit-generated decorations before comparing the durable summary.
    while True:
        stripped = _REVIEW_DECORATION_PREFIX.sub("", normalized, count=1).lstrip()
        if stripped == normalized:
            break
        normalized = stripped
    return " ".join(normalized.split())


def _message_revision_number(message: Mapping[str, Any]) -> int | None:
    value = message.get("_revision_number")
    try:
        revision_number = int(value)
    except (TypeError, ValueError):
        return None
    return revision_number if revision_number > 0 else None


def _decode_gerrit_json(text: str) -> Any:
    if text.startswith(_XSSI_PREFIX):
        text = text[len(_XSSI_PREFIX) :]
        if text.startswith("\r\n"):
            text = text[2:]
        elif text.startswith("\n"):
            text = text[1:]
    if not text.strip():
        return None
    return json.loads(text)


def _parse_change(
    payload: Mapping[str, Any],
    *,
    expected_project: str,
    expected_number: int,
) -> GerritChange:
    project = payload.get("project")
    number = payload.get("_number")
    if project != expected_project or number != expected_number:
        raise PermanentError(
            "Gerrit returned change identity inconsistent with the requested project/change"
        )

    current_revision = payload.get("current_revision")
    if not isinstance(current_revision, str) or not current_revision:
        raise TransientError("Gerrit ChangeInfo is missing current_revision")
    revisions = payload.get("revisions")
    if not isinstance(revisions, Mapping):
        raise TransientError("Gerrit ChangeInfo is missing revisions")
    revision = revisions.get(current_revision)
    if not isinstance(revision, Mapping):
        raise TransientError("Gerrit ChangeInfo is missing current revision metadata")

    status = payload.get("status")
    if not isinstance(status, str) or not status:
        raise TransientError("Gerrit ChangeInfo is missing status")

    return GerritChange(
        project=expected_project,
        change_number=expected_number,
        current_revision=current_revision,
        patchset_number=_required_positive_int(revision, "_number"),
        ref=_optional_string(revision.get("ref")),
        status=status,
        branch=_optional_string(payload.get("branch")),
        change_id=_optional_string(payload.get("change_id")),
        subject=_optional_string(payload.get("subject")),
        updated=_optional_string(payload.get("updated")),
        raw=dict(payload),
    )


def _change_identifier(project: str, change_number: int) -> str:
    if change_number < 1:
        raise ValueError("change_number must be positive")
    return quote(f"{project}~{change_number}", safe="~")


def _query_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _gerrit_search_boundary(value: str) -> str:
    """Ceil Gerrit's nanosecond UTC timestamp to a search-safe millisecond boundary."""

    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?", value
    )
    if match is None:
        raise TransientError(f"Gerrit response has invalid updated timestamp {value!r}")
    try:
        base = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise TransientError(f"Gerrit response has invalid updated timestamp {value!r}") from exc
    fraction = (match.group(2) or "").ljust(9, "0")
    nanoseconds = int(fraction or "0")
    milliseconds, remainder = divmod(nanoseconds, 1_000_000)
    if remainder:
        milliseconds += 1
    if milliseconds == 1000:
        base += timedelta(seconds=1)
        milliseconds = 0
    return f"{base.strftime('%Y-%m-%d %H:%M:%S')}.{milliseconds:03d} +0000"


def _gerrit_update_second(value: str) -> str:
    """Normalize Gerrit's UTC Timestamp string to a search-safe whole-second boundary."""

    candidate = value[:19]
    try:
        datetime.strptime(candidate, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise TransientError(f"Gerrit response has invalid updated timestamp {value!r}") from exc
    return candidate


def _required_positive_int(parent: Mapping[str, Any], key: str) -> int:
    value = parent.get(key)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TransientError(f"Gerrit response has invalid integer field {key!r}") from exc
    if parsed < 1:
        raise TransientError(f"Gerrit response has non-positive integer field {key!r}")
    return parsed


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _account_name(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    for key in ("username", "email", "name"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
