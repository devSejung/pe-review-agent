# OSS review-engine compatibility notes

Research snapshot: 2026-09-16.

Upstream documentation was rechecked on 2026-09-17. The current PR-Agent usage guide lists its
supported providers as GitHub, GitLab, Bitbucket, Azure, CodeCommit, local, and Gitea; Gerrit is not
listed as a supported provider. This does not change the service boundary decision below.

Current upstream reference:

```text
https://github.com/The-PR-Agent/pr-agent/blob/main/docs/docs/usage-guide/automations_and_usage.md
```

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

This was not a NIH/rewrite-for-rewrite's-sake decision. The service requirements include durable
Patch Set event ingestion, exactly identified revisions, native Gerrit PARENT-side deletion comments,
post-response-loss reconciliation, stale-Patch-Set suppression, duplicate-event idempotency across
restarts, finding lineage across Patch Sets, internal Qwen/OpenAI-compatible APIs, and offline
deployment. Those concerns are intentionally kept outside the replaceable review-engine boundary.

The initial production engine is `NativeFirmwareReviewEngine`, which implements the two-pass
high-signal firmware policy directly. PR-Agent can be integrated behind the same interface after a
pinned release/commit is validated in the target offline build, without changing Gerrit lifecycle
or persistence semantics.

The Patch-Set finding lifecycle deliberately adopts the conservative principle used by PR-Agent's
MIT-licensed `review_finding_state.py` at the pinned commit above: absence from a partial/failed pass
is not evidence that a finding is resolved. In this service, every finding from the previous
successfully published Patch Set is injected into the independent verification pass even when the
candidate-generation pass misses it. Only a complete successful verification can therefore move a
previous finding to the resolved summary. Older resolved findings are retained as bounded context so
the model can preserve the original semantic identity when a root cause reappears.
