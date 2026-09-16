# Gerrit AI Reviewer

Production-oriented Gerrit 3.8 review service for firmware repositories. It listens to
`patchset-created`, persists every review job in PostgreSQL, reviews the exact revision with an
OpenAI-compatible model, verifies findings against repository context, and publishes a Gerrit
summary plus native inline/range comments.

## Non-negotiable correctness properties

- Durable job identity: `(project, change_number, revision_sha, review_policy_version)`.
- Duplicate Gerrit events do not create duplicate jobs or comments.
- The worker refuses publication when either Gerrit `current_revision` or the durable job store
  knows a newer Patch Set. The final Gerrit GET/local-guard/POST window is minimized; an absolute
  atomic guarantee against a Patch Set created inside that last network race requires a Gerrit-side
  compare-and-post extension (see the runbook).
- Review results are persisted before publication, so Gerrit publication retries never rerun the
  LLM unnecessarily.
- Deleted-code findings are represented explicitly on Gerrit's `PARENT` side, so deletion-only
  Patch Sets remain reviewable and can receive native inline comments.
- Optional repository review guidance is loaded from the accepted baseline Git revision as bounded
  blobs, never by following files/symlinks from the untrusted candidate worktree.
- Merge commits are currently safe-skipped with a visible Gerrit summary rather than reviewed
  against an incorrect first-parent diff. Gerrit 3.8 uses its auto-merge base for merge diffs; a
  future merge-review path must ingest that Gerrit DiffInfo before native inline comments are safe.
- Patch Sets that exceed `repos.max_diff_bytes` are likewise safe-skipped with a visible summary
  instead of disappearing into a permanent background failure.
- Gerrit reconciliation avoids live `S/start` offset pagination. It walks the update-time order with
  `before:` boundaries and explicit same-second tie exclusion so concurrent Change updates cannot
  shift an unseen Change past an offset. Incremental passes are supplemented by periodic full
  open-Change sweeps so temporary secondary-index lag cannot permanently age out a missed event.
- Event-stream reconnects are supplemented by reconciliation; polling is not the primary trigger.
- Project allowlists are enforced in the service even though Gerrit's `Stream Events` is a global
  capability.
- Bot is comment-only by default: no Submit and no Code-Review vote.
- A newer Patch Set waits while an older Patch Set has an unresolved/ambiguous publication side
  effect, preserving finding lineage and preventing the same published finding from being reposted
  as new.
- Once a Gerrit POST becomes ambiguous, reconciliation retries are intentionally not terminalized by
  the ordinary retry budget: neither a transient GET failure nor a later read/auth failure can prove
  whether that external side effect committed. Retries use the configured capped backoff and keep
  newer Patch Sets ordered behind the unresolved publication until Gerrit can answer or an operator
  intervenes.
- Individual summary/inline messages are UTF-8-byte capped by `gerrit.max_comment_bytes` (16 KiB by
  default) so one verbose model finding cannot make Gerrit reject the entire Set Review request.
- `service.enabled=false` is an emergency global kill switch. After restart, receiver, worker, and
  reconciler stay alive but idle; durable jobs/results remain intact for later resume.
- `FAILED_PERMANENT` jobs can be explicitly retried with
  `pe-review-agent requeue --job-id <uuid>`. Requeue preserves prior attempt history, starts a new
  retry-budget epoch, refuses stale Patch Sets, and resumes from a durable review/publication intent
  when one exists instead of rerunning Qwen.

## Runtime topology

```text
Gerrit SSH stream-events -> receiver -> PostgreSQL jobs -> worker
                                                  |-> repo cache/worktree
                                                  |-> Qwen/OpenAI API
                                                  `-> Gerrit REST review
```

The target environment can be offline from the public internet. Deployment artifacts are intended
to be built on an internet-capable machine and transferred as Docker image tarballs plus config.

## Local development

```bash
python -m venv .venv
pip install -e ".[dev]"
pytest
```

Copy `config/config.example.yaml` to `config/local.yaml` and provide secrets only through the
environment or mounted secret files.

## Gerrit permissions

The dedicated bot account needs SSH access, Read on target projects, permission to comment through
REST, and the global `Stream Events` capability. It should not receive Submit or Code-Review +2.

The currently observed personal-account failure `stream events not permitted` is an ACL/capability
issue, not a transport failure.
