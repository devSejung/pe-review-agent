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

## Production quick start

This section is the minimum operator path for enabling the reviewer on a Gerrit 3.8 installation.
For failure handling and acceptance tests, continue with `docs/runbook.md`.

### 1. Create a dedicated Gerrit bot account

Use a service account such as `pe-review-agent`; do not run the production service with a personal
employee account.

Required Gerrit access:

- SSH login/access to Gerrit.
- `Read` on every project listed in `gerrit.projects`.
- Permission to post normal review messages and native inline/range comments through Gerrit REST.
- Global `Stream Events` capability so the receiver can consume `patchset-created`.

The bot does **not** need Gerrit Administrator, Submit, automatic merge authority, or Code-Review
`+2`.

Validate the event-stream capability before deployment:

```bash
timeout 5 ssh -p 29418 pe-review-agent@gerrit.example.internal \
  gerrit stream-events -s patchset-created
```

If Gerrit returns `stream events not permitted`, SSH connectivity is working but the account is
missing the global `Stream Events` capability.

### 2. Determine the exact Gerrit project names

`gerrit.projects` is an explicit service-side allowlist. The Gerrit `Stream Events` capability is
global, but events for projects not listed here are ignored by this service.

From each repository that should be reviewed:

```bash
git remote get-url origin
```

For example, if the remote is:

```text
ssh://developer@gerrit.example.internal:29418/platform/dmc-fw
```

then configure the project as:

```yaml
gerrit:
  # Optional bootstrap list. Leave empty when projects will be managed from the Admin Web.
  projects: []
```

Start production acceptance with **one test project only**. Expand the allowlist after the first
end-to-end Patch Set succeeds.

### 3. Configure Gerrit SSH and REST

Copy `config/config.example.yaml` to the deployment directory as `config.yaml` and set the real
endpoints:

```yaml
gerrit:
  # Used for stream-events and exact Git revision fetches.
  ssh_host: "gerrit.example.internal"
  ssh_port: 29418
  ssh_user: "pe-review-agent"
  ssh_key_path: "/run/secrets/gerrit_ssh_key"
  known_hosts_path: "/run/secrets/gerrit_known_hosts"
  strict_host_key_checking: true

  # Used for current-revision checks, reconciliation, and Set Review publication.
  rest_url: "https://gerrit.example.internal"
  rest_auth:
    mode: "basic"
    username: "pe-review-agent"
    password_env: "PE_REVIEW_GERRIT_HTTP_PASSWORD"

  projects:
    - "platform/dmc-fw"

  review_tag: "autogenerated:pe-ai-review"
  notify: "OWNER"
  max_comment_bytes: 16384
```

REST authentication supports `none`, `basic`, and `bearer`:

```yaml
# Basic auth
rest_auth:
  mode: "basic"
  username: "pe-review-agent"
  password_env: "PE_REVIEW_GERRIT_HTTP_PASSWORD"
```

```yaml
# Bearer token
rest_auth:
  mode: "bearer"
  token_env: "PE_REVIEW_GERRIT_TOKEN"
```

If the corporate Gerrit HTTPS endpoint uses a private CA, mount the PEM into the reviewer containers
and set:

```yaml
gerrit:
  ca_bundle_path: "/run/secrets/corporate_ca.pem"
```

### 4. Configure the internal Qwen/OpenAI-compatible API

The review engine calls an OpenAI-compatible chat-completions endpoint. For an internal vLLM/Qwen
deployment:

```yaml
llm:
  base_url: "https://qwen-api.example.internal/v1"
  model: "Qwen3.6-27B"
  api_key_env: "PE_REVIEW_LLM_API_KEY"
  request_timeout_seconds: 180
  concurrency: 2
  temperature: 0.1
  max_output_tokens: 6000
```

If the internal endpoint uses a private CA, configure `llm.ca_bundle_path` the same way as Gerrit.
If the endpoint does not require an API key, the corresponding environment value may be empty.

### 5. Configure secrets in `.env`

Do not put passwords, tokens, or private keys in `config.yaml` or Git.

Copy `deploy/env.example` to `.env` and set values appropriate for the environment:

```dotenv
POSTGRES_PASSWORD=CHANGE_ME_TO_A_RANDOM_SECRET
REVIEWER_IMAGE=gerrit-ai-reviewer:local
PE_REVIEW_LLM_API_KEY=
PE_REVIEW_GERRIT_HTTP_PASSWORD=
PE_REVIEW_GERRIT_TOKEN=
PE_REVIEW_ADMIN_PASSWORD=CHANGE_ME_TO_ANOTHER_RANDOM_SECRET
ADMIN_BIND_ADDRESS=0.0.0.0
ADMIN_PORT=8080
WORKER_HEALTH_PORT=8081
```

For bearer Gerrit auth, also provide the variable named by `rest_auth.token_env`, for example:

```dotenv
PE_REVIEW_GERRIT_TOKEN=...
```

The supplied Compose file passes the standard `PE_REVIEW_GERRIT_TOKEN` variable through to all
reviewer services. If you choose a different `token_env` name, add that variable to the Compose
review environment as well.

Place SSH material beside the deployment:

```text
secrets/
├── gerrit_ssh_key
└── gerrit_known_hosts
```

`deploy/install.sh` requires these files, normalizes them to UID `10001`, and enforces mode `0600`.
The container intentionally uses strict host-key checking by default.

### 6. PostgreSQL

The supplied Compose topology includes PostgreSQL; a separate database host is not required for the
default deployment. The reviewer connects over the private Compose network:

```yaml
database:
  host: "postgres"
  port: 5432
  username: "pe_review"
  database: "pe_review"
  password_env: "POSTGRES_PASSWORD"
```

PostgreSQL port `5432` is not published to the host. Database migrations run before receiver, worker,
and reconciler startup.

### 7. Validate configuration before enabling work

The settings models reject unknown keys. A typo such as `service.enabld: false` fails validation
instead of silently leaving the service enabled.

Run the configuration check from the release/container environment before first startup:

```bash
pe-review-agent check-config
```

The bootstrap-file emergency kill switch is:

```yaml
service:
  enabled: false
```

or:

```bash
PE_REVIEW__SERVICE__ENABLED=false
```

Restart the reviewer services after changing the YAML/environment kill switch. This bootstrap value
is a **hard** switch: `service.enabled=false` cannot be overridden by a previously stored Admin Web
value. With the bootstrap switch left `true`, **Settings -> AI review service** provides the normal
live pause/resume control without editing files. Durable jobs and review results are retained while
disabled.

### 8. Install and start

For an offline target, build the release on an internet-capable machine:

```bash
./deploy/build-release.sh 0.1.0
```

Transfer and extract the generated release on the corporate Linux host, prepare `config.yaml`,
`.env`, and `secrets/`, then run:

```bash
./install.sh
```

The Compose stack contains:

```text
postgres    durable state
migrate     one-shot schema migration
receiver    Gerrit patchset-created ingestion
worker      repository fetch + Qwen review + Gerrit publication
reconciler missed-event / ambiguous-publication recovery
admin       internal web control plane on port 8080
```

### 9. Admin Web on port 8080

The deployment includes an operations UI at:

```text
http://REVIEW_HOST:8080/
```

It is intentionally an **admin control plane**, not a public end-user application. The default
configuration requires HTTP Basic authentication using `admin.username` from `config.yaml` and the
password stored in `PE_REVIEW_ADMIN_PASSWORD`. The application also enforces CSRF validation,
SameSite cookies, a restrictive Content-Security-Policy, `frame-ancestors 'none'`, and common browser
security headers.

> Basic authentication does not encrypt credentials on plain HTTP. For a shared corporate service,
> terminate HTTPS and preferably company SSO at an internal reverse proxy in front of port 8080.
> Keep direct access to 8080 restricted to the trusted internal network. `admin.auth_mode: none` is
> rejected unless `admin.host` is loopback.

The UI has six operational surfaces:

- **Dashboard** — recent job volume, failures, active queue/state counts, and recent Changes.
- **Projects** — add exact Gerrit project names, test REST Read + Git/SSH fetch access, and
  enable/disable review.
- **Connections** — edit/test Gerrit SSH + REST and the OpenAI-compatible Qwen endpoint.
- **Jobs** — filter durable jobs, inspect state/failure text, open the full audit trail, and requeue
  `FAILED_PERMANENT` jobs. The audit page shows every durable attempt, the model summary and findings,
  file/side/line/confidence/evidence/remediation, the exact persisted Gerrit `ReviewInput`, publication
  status, and Gerrit's response.
- **Logs** — browse receiver/worker/reconciler/admin structured logs, filter by component/level/text,
  auto-refresh every five seconds, and expand the complete JSON/exception traceback instead of a
  truncated one-line error.
- **Settings** — live global pause/resume plus review-policy controls, including the human-facing
  review language. The default is Korean (`ko-KR`); English (`en-US`) can be selected. The prompt
  explicitly keeps code identifiers, paths, macros, register names, literals, and error codes in
  their original form.

Project enable/disable and the global service switch are live DB-backed controls. Disabled projects
are filtered at event ingestion, reconciliation, **and the PostgreSQL claim query**, so queued work is
not claimed while a project is disabled. When re-enabled, durable queued jobs become claimable again
and reconciliation can recover Patch Sets that arrived while disabled.

Gerrit endpoint/auth metadata, LLM endpoint/model, and review-policy edits are persisted in PostgreSQL
but deliberately require restart of `receiver`, `worker`, and `reconciler`. This avoids mutating
long-lived HTTP/SSH/model clients in the middle of an active review. On restart the services overlay
the DB-managed values on top of the bootstrap configuration.

Secrets remain bootstrap-only:

```text
Gerrit SSH private key   -> mounted file
Gerrit REST password     -> environment
Gerrit bearer token      -> environment
LLM API key              -> environment
Admin password           -> environment
```

The web application never writes these secret values to PostgreSQL and never renders them back to
the browser. It only reports whether a required secret/file is configured.

Operational logs are additionally written as rotating JSONL files under `admin.log_root`
(`/var/lib/pe-review-agent/logs` by default). Compose gives each process its own file (`worker.jsonl`,
`receiver.jsonl`, `reconciler.jsonl`, `admin.jsonl`, and `migrate.jsonl`). The default rotation is
10 MiB with five backups per process. Console JSON logging remains enabled as well.

The UI is server-rendered FastAPI with locally vendored **Tabler 1.5.1** assets. No CDN is contacted at
runtime, so the same web console works in the offline corporate deployment. Tabler is MIT licensed;
its license text is kept with the vendored assets under
`src/pe_review_agent/admin/static/vendor/TABLER-LICENSE.txt`.

### 10. First end-to-end acceptance

Keep only one test repository enabled (preferably through the Admin Web; the bootstrap
`gerrit.projects` list is also supported), upload a new Patch Set, and verify in order:

1. `patchset-created` is consumed by the receiver.
2. A unique PostgreSQL review job is created.
3. The exact Patch Set SHA is fetched from Gerrit.
4. Qwen generation and deterministic verification complete.
5. The review result becomes durable before publication.
6. The publisher re-checks Gerrit `current_revision` and the local stale-Patch-Set guard.
7. Gerrit receives one Change-level summary plus native inline/range comments.
8. Replaying the same event does not create duplicate comments.
9. Uploading a newer Patch Set prevents an older pending review from publishing stale findings.
10. A deletion-only finding lands on Gerrit's `PARENT` side.

Also test a merge commit and an intentionally oversized diff. Both should produce explicit safe-skip
summaries rather than unsafe line anchors or silent background failures.

The Admin Web owns host port `8080`. Its health endpoints are:

```text
GET http://REVIEW_HOST:8080/healthz
GET http://REVIEW_HOST:8080/readyz
```

Worker-only health and Prometheus metrics remain bound to localhost on `8081` by the supplied
Compose file:

```text
GET http://127.0.0.1:8081/healthz
GET http://127.0.0.1:8081/readyz
GET http://127.0.0.1:8081/metrics
```

## Why this service owns the Gerrit lifecycle instead of using an OSS reviewer end-to-end

An existing OSS reviewer was investigated rather than ignored. The primary candidate was PR-Agent.
At the pinned upstream commit documented in `docs/oss-engine-notes.md`, it contained a Gerrit provider,
but Gerrit inline-comment publication was explicitly unsupported. More importantly, the production
requirements here extend well beyond model prompting:

- Every Gerrit **Patch Set upload** must automatically trigger review through `stream-events`.
- Summary and exact native Gerrit inline/range comments must be published against the reviewed SHA.
- Deleted-line findings need Gerrit `PARENT`-side anchors.
- Jobs and review results must survive LLM, Gerrit, Git, process, container, and host failures.
- A lost Gerrit POST response must reconcile the exact durable ReviewInput before another POST is
  allowed, preventing duplicate comments.
- New Patch Sets must supersede stale work without allowing an older review to land later.
- Duplicate events must be idempotent across process restarts, not merely within one model run.
- Finding identity must carry across Patch Sets so persisting findings are not re-posted as new.
- The target deployment must support an internal OpenAI-compatible Qwen endpoint and offline release
  installation.
- Operations require a durable PostgreSQL state machine, retry budgets, manual requeue, health and
  metrics, emergency kill switch, and recovery sweeps for missed Gerrit events.

Those are lifecycle, persistence, and Gerrit-correctness requirements, not features that should be
delegated to a generic PR prompt framework. Therefore this project keeps Gerrit ingestion,
persistence, repository workspaces, stale-Patch-Set guards, reconciliation, and ReviewInput
publication service-owned. The model/review engine remains behind a replaceable
`ReviewEngine.review(context, tools) -> ReviewResult` boundary. An OSS review engine can be integrated
there later without changing the safety-critical Gerrit lifecycle.

See `docs/oss-engine-notes.md` for the pinned PR-Agent compatibility finding and licensing/integration
notes.

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
