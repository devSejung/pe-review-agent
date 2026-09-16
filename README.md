# Gerrit AI Reviewer

Production-oriented Gerrit 3.8 review service for firmware repositories. It listens to
`patchset-created`, persists every review job in PostgreSQL, reviews the exact revision with an
OpenAI-compatible model, verifies findings against repository context, and publishes a Gerrit
summary plus native inline/range comments.

## Non-negotiable correctness properties

- Durable job identity: `(project, change_number, revision_sha, review_policy_version)`.
- Duplicate Gerrit events do not create duplicate jobs or comments.
- A review generated for an older Patch Set is never published after a newer Patch Set becomes
  current.
- Review results are persisted before publication, so Gerrit publication retries never rerun the
  LLM unnecessarily.
- Event-stream reconnects are supplemented by reconciliation; polling is not the primary trigger.
- Project allowlists are enforced in the service even though Gerrit's `Stream Events` is a global
  capability.
- Bot is comment-only by default: no Submit and no Code-Review vote.

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
