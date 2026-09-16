# OSS review-engine compatibility notes

Research snapshot: 2026-09-16.

PR-Agent was inspected from upstream commit:

```text
862efa80a10627e7c6fb5ddf1b09c5b265afc0a2
```

The upstream tree contains `pr_agent/git_providers/gerrit_provider.py` and registers a `gerrit`
provider. However, at that commit the Gerrit provider explicitly reports inline-comment capability
as unsupported and both `publish_inline_comments()` and `publish_inline_comment()` raise
`NotImplementedError`.

For that reason this service does not let PR-Agent own Gerrit lifecycle or publishing. The stable
boundary is `ReviewEngine.review(context, tools) -> ReviewResult`; Gerrit event handling, durable
jobs, repository workspaces, supersession checks, and native ReviewInput publication remain service
owned.

The initial production engine is `NativeFirmwareReviewEngine`, which implements the two-pass
high-signal firmware policy directly. PR-Agent can be integrated behind the same interface after a
pinned release/commit is validated in the target offline build, without changing Gerrit lifecycle
or persistence semantics.
