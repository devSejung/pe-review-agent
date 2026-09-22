# Gerrit AI Reviewer Operations Runbook

한국어 상세 운영 가이드는 [`guide-ko.md`](guide-ko.md)를 참고하세요.

## 1. Gerrit bot account

Prefer a dedicated Gerrit account named `pe-review-agent` (or the local naming convention) rather
than a personal employee account.

Request only what the service needs:

- SSH login/access to Gerrit.
- `Read` on the explicitly configured team projects.
- Ability to post normal review messages and inline comments through the REST API.
- Global **Stream Events** capability.
- REST authentication appropriate for the corporate Gerrit deployment.

Do not request Gerrit Administrator, Submit, automatic merge authority, or Code-Review +2.

For repositories explicitly opted into automatic Code-Review voting, additionally grant the bot
Code-Review +1 on the applicable branches. Default remains OFF. Zero validated findings, even from
a budget-limited partial review, get +1; nonzero findings get 0. Failed/skipped reviews do not vote.
Confirm that this policy is acceptable to local submit requirements and external automation.
No proactive reset occurs on a new Patch Set; Gerrit may copy an older label until the new review.
Comment publication and voting have separate durable audit results; a vote failure does not discard
comments. See the repository-policy section in README and the Korean guide for retry/upgrade details.

An authenticated probe can be run with:

```bash
timeout 5 ssh -p 29418 review-user@gerrit.example.internal \
  gerrit stream-events -s patchset-created
```

If it returns:

```text
stream events not permitted
```

that response means the SSH command reached Gerrit but the account lacks the `Stream Events`
global capability.

## 2. Confirm endpoints before production enablement

From an existing team repository, record:

```bash
git remote get-url origin
```

Open the Gerrit server's `/ssh_info` endpoint and confirm the same SSH host/port before enabling the
service.

Confirm HTTPS REST reachability from the review host and determine the supported authentication
method. Do not put a personal password or private key into source control.

## 3. Host layout

A recommended host layout is:

```text
/home/pe-review-agent/
├── deploy/
├── data/
├── logs/
└── releases/
```

The containers themselves run as unprivileged UID 10001. PostgreSQL is only on the Compose network;
port 5432 is not published to the host.

When provisioning the Linux host as root, `bootstrap-host.sh` creates the dedicated
`pe-review-agent` system account with UID **10001**, matching the unprivileged container runtime,
and creates the recommended directories. If that account already exists with another host UID, the
script keeps the existing account unchanged; host UID equality is not required. `install.sh`
normalizes only the deployment copies of the SSH files to container UID 10001. It does not grant
interactive login or Gerrit permissions.

## 3.1 Lifecycle and diagnostics scripts

Run these from the deployment directory:

```bash
./start.sh
./stop.sh
./restart.sh
./status.sh
./logs.sh worker
./doctor.sh
```

`stop.sh` preserves containers and named volumes. `restart.sh` recreates the Compose stack so `.env`
and configuration changes are re-read while preserving PostgreSQL/repository named volumes.
`install.sh` is the first-install path: it validates files/images, fixes deployment-secret ownership,
and automatically remaps a host port if 8080/8081 is already owned by an unrelated process. Later
start/restart operations fail instead of silently moving an established endpoint.

For a new source checkout, use `configure.sh` to create `.env`, `config.yaml`, random local service
passwords, and SSH deployment copies. For corporate network setup, copy `corporate.env.example` to
the gitignored `corporate.env`, fill the company mirror/CA values once, then run:

```bash
./configure-corporate-host.sh
./build-local.sh
```

The corporate-host script accepts PEM or DER X.509 files, installs normalized CA certificates,
backs up and merges `/etc/docker/daemon.json`, restarts Docker, and leaves unrelated daemon keys
untouched. The local image builder forwards the configured PyPI and Debian mirror values into the
Docker build.

## 4. Offline install

Build the release on an internet-capable machine:

```bash
./deploy/build-release.sh 0.1.0
```

If the build machine must use an internal PyPI mirror, the release builder forwards the standard
pip build settings into the Docker build:

```bash
PIP_INDEX_URL="https://pypi-mirror.example.internal/simple" \
PIP_TRUSTED_HOST="pypi-mirror.example.internal" \
./deploy/build-release.sh 0.1.0
```

Omit these variables on hosts that can use the normal public PyPI index.

If the build machine also cannot reach the public Debian repositories, pass Debian mirror URLs as
well. These are Docker-image Debian sources, not the Ubuntu host's sources.list entries:

```bash
PIP_INDEX_URL="https://pypi-mirror.example.internal/simple" \
PIP_TRUSTED_HOST="pypi-mirror.example.internal" \
APT_DEBIAN_MIRROR_URL="http://debian-mirror.example.internal/debian" \
APT_DEBIAN_SECURITY_MIRROR_URL="http://debian-security-mirror.example.internal/debian-security" \
./deploy/build-release.sh 0.1.0
```

Transfer the resulting `release/gerrit-ai-reviewer-0.1.0.tar.gz` to the corporate Linux host.
Extract it under `/home/pe-review-agent/releases/`, then:

1. Copy `config.example.yaml` to `config.yaml` and fill only non-secret settings.
2. Copy `.env.example` to `.env`; set the PostgreSQL password and API secret environment values.
3. Put the Gerrit SSH private key at `secrets/gerrit_ssh_key` with mode `0600`.
4. Put a pinned Gerrit host key in `secrets/gerrit_known_hosts`. `install.sh` normalizes both the
   private key and known-hosts file to UID 10001 and mode `0600`, matching the unprivileged reviewer
   container.
5. Run `./install.sh`.

The release contains Docker image tarballs, so the target host does not need public registry or PyPI
access.

## 5. Admin Web and first project

After `install.sh` starts the stack, open:

```text
http://REVIEW_HOST:8080/
```

Authenticate with `admin.username` from `config.yaml` and `PE_REVIEW_ADMIN_PASSWORD` from `.env`.
The default deployment binds port 8080 to the host. This is intended for an internal trusted network;
for shared use, put an internal HTTPS/SSO reverse proxy in front of it because Basic credentials are
not encrypted by plain HTTP.

The web UI manages projects, connection metadata, durable jobs, service enable/disable, review
audit, structured logs, review policy, process liveness, and the **Needs attention** incident queue.
It never stores Gerrit/LLM/admin secrets in PostgreSQL. Project enable/disable and global
pause/resume take effect live. Gerrit endpoint/auth metadata, LLM endpoint/model, and review-policy
changes are saved durably but require restarting receiver/worker/reconciler so active clients are not
mutated mid-review.

Restart-required changes advance a durable configuration generation. Receiver, worker, reconciler,
and admin report a PostgreSQL heartbeat with their process instance, version, Git SHA, last-seen time,
loaded generation, and a non-secret settings fingerprint. The global warning remains until all three
runtime services have applied the current generation. A mixed Git SHA or settings fingerprint is
shown as an operational error instead of being left for later log archaeology.

The fingerprint is the loaded settings snapshot, not a file watcher. A manual `config.yaml` edit is
visible as drift only after at least one process restarts. Admin Web saves are different: they advance
the durable generation immediately, so pending processes are visible before restart.

Admin Web stores only fields that an operator explicitly overrides. On **Connections**, the source
banner shows whether Gerrit/LLM values currently come from `config.yaml` or saved Admin overrides.
**Use config.yaml values** clears the Gerrit/LLM overrides; restart the long-lived services afterward.
The page reports REST secrets as `not required`, `configured`, or `missing` and shows the expected
environment-variable name, never the secret value itself.

For model browsing loops, keep `review.max_tool_rounds` near the normal operating value (8-16 is a
reasonable starting range; the validated ceiling is 64) rather than using a very large number as the
primary fix. The reviewer suppresses identical read-only tool calls within a model tool session and,
when the round budget is exhausted, disables repository tools for one final JSON response instead of
discarding the whole review. The Job Audit page records the bounded tool trace and forced-finalization
reason for diagnosis.

`search_text` includes ±12 lines of context for only the top six matches and leaves later matches as
locations. `batch_read` can fetch at most six explicit file/line ranges in one model-visible tool call;
each range is capped at 200 lines and the combined output is capped. A batch counts as one charged tool
call, while the trace records how many range operations it attempted. Requests above six are truncated
to the first six with a hint to request the remainder separately.

Upgrade note: older releases stored a full runtime snapshot. At startup, a legacy section that is
identical to the current `config.yaml` is removed automatically. A differing full snapshot is retained
to avoid deleting a potentially intentional Admin edit; Connections or Settings shows a warning for
the corresponding section. After verifying the deployment file, use **Use config.yaml values** when
the YAML should win.

Stop old receiver/worker/reconciler/admin processes before migrating and starting this version. Older
processes do not publish heartbeat/config-generation state, and an old Admin must not remain writable
during a mixed-version upgrade.

**Settings -> Review language** selects the human-facing language injected into both the candidate
review and independent-verifier prompts. The default is `ko-KR`; `en-US` is also available. Function
names, variables, macros, register names, paths, commands, literals, and error codes are instructed to
remain verbatim rather than being translated.

Use **Projects -> Add Gerrit project** for the first test repository. The project name must be the
exact Gerrit project path (for example `platform/dmc-fw`). Use **Test** to verify REST Read access
and Git/SSH fetch access before enabling it.

New projects use **From now on** by default. The activation timestamp is stored per project and the
reconciler applies it in the Gerrit query itself, so a repository with tens of thousands of existing
open Changes is not scanned or enqueued by default. Selecting **Include current open Changes** opts
that project into backfill. Switching a `FROM_NOW` project off and back on resets its cutoff to the
re-enable time, so Patch Sets uploaded while it was disabled are not replayed later. Switching from
backfill back to **From now on** terminalizes queued, unleased pre-cutoff work as `SKIPPED_SCOPE`.
Jobs that already have a Gerrit publication intent are never discarded and continue through the
normal publication-reconciliation safety path.

### Enable one test project first

Keep only one test repository enabled for the first end-to-end run. Upload a new Patch Set and
verify, in order:

1. receiver consumes `patchset-created`;
2. the unique durable job exists;
3. exact revision fetch succeeds;
4. Qwen review and verification complete;
5. result is stored as ready-to-publish;
6. publisher re-checks Gerrit `current_revision`;
7. summary and native inline/range comments appear on that revision;
8. a duplicate event does not add duplicate comments;
9. uploading another Patch Set prevents the old job from publishing stale findings.

Deletion-only changes should be included in acceptance: verify that a finding on removed code is
posted as a native `PARENT`-side inline comment. A merge-commit Patch Set should produce the explicit
safe-skip summary and no AI findings; first-parent line anchors are intentionally not guessed because
Gerrit 3.8 presents merge diffs against its auto-merge base.

Also exercise an intentionally oversized Patch Set with a low test `repos.max_diff_bytes`; it should
post a summary explaining that automated review was skipped, not end as a silent permanent failure.

Only then add/enable more projects in the Admin Web.

## 6. Health and metrics

Admin Web health is exposed on host port 8080:

```text
GET /healthz
GET /readyz
```

The worker exposes localhost-only port 8081 by default:

```text
GET /healthz
GET /readyz
GET /metrics
```

Useful operational signals include queue depth, stage retry counts, review latency, Qwen latency and
token usage, publish latency, verified finding counts, duplicate events, and superseded jobs.

The Dashboard also derives process and durable-job health:

- heartbeat older than 60 seconds: component is **stale**;
- `FAILED_PERMANENT`: always listed under **Needs attention**;
- retry overdue by more than two minutes;
- FETCHING/REVIEWING/VALIDATING/PUBLISHING with an expired or missing lease after a grace period;
- RECEIVED or READY_TO_PUBLISH waiting beyond their queue grace;
- Gerrit publication remaining `AMBIGUOUS` for more than ten minutes;
- runtime configuration not yet applied by every service, or mixed Git revisions.

Intentional global pause and disabled projects suppress ordinary queue/lease warnings. Permanent
failures and unresolved external publication outcomes remain visible.

## 7. Incident behavior

Emergency stop for new work: use **Settings -> AI review service** in the Admin Web. The DB-backed
switch is checked by receiver, worker claim loops, and reconciliation. It prevents new ingest/claims
while preserving PostgreSQL state; an already in-flight review may finish its current operation.
Restore the switch to resume. The bootstrap alternative is `service.enabled: false` (or
`PE_REVIEW__SERVICE__ENABLED=false`) followed by service restart. The bootstrap value is the hard
kill switch and takes precedence over the DB-backed web value; the web UI cannot re-enable work until
the bootstrap value is restored to `true` and services are restarted.

To retry a job that is already `FAILED_PERMANENT` after the underlying problem is fixed:

```bash
docker compose run --rm worker requeue --job-id <review-job-uuid>
```

The same operation is available as **Jobs -> Requeue** in the Admin Web.

For incident triage, begin with **Dashboard -> Needs attention**, then open **Jobs -> Audit** on the
affected Change. That page contains the complete
durable attempt timeline, full stored failure text, model summary/findings, and the exact Gerrit
`ReviewInput`/response. Use **Logs** for process-level details and exception tracebacks. Logs are
persisted in the shared reviewer volume as rotating JSONL files and can be filtered by service,
severity, or free text; the page auto-refreshes every five seconds.

This is an administrative retry, not a state reset. Previous attempt rows remain as audit history,
while a new retry-budget epoch starts at the current attempt number. A durable review resumes at
`READY_TO_PUBLISH`; an existing publication intent resumes at `PUBLISHING` and is reconciled before
another Gerrit POST; a stale Patch Set with a newer known Patch Set is refused.

- **Qwen unavailable/slow:** jobs remain durable and retry with backoff; no event is intentionally
  discarded.
- **Gerrit publish failure after review:** keep the stored review result and retry publishing only;
  do not rerun Qwen.
- **Worker crash:** expired job leases are reclaimed and execution resumes from durable state.
  The worker owns the repository cache volume with a process lock and clears abandoned worktrees on
  startup. Structurally invalid or crash-locked cache mirrors are rebuilt from Gerrit because they
  are cache-only state.
- **Newer Patch Set arrives:** old job becomes superseded; the publisher independently re-reads
  Gerrit current revision immediately before POST. If an older publication is ambiguous, the newer
  Patch Set waits for that side effect to reconcile before review so finding history cannot race.
- **Gerrit POST response is lost / ambiguous:** the durable ReviewInput is retained and the worker
  repeatedly checks for the tagged Patch Set message before deciding whether another POST is safe.
  This reconciliation path deliberately continues past the normal retry budget with capped backoff,
  because transport failures, later 403/404 reads, or other observation failures cannot by
  themselves prove whether the original external side effect committed.
- **Reconciliation event-stream safety net:** Query Changes is walked by update-time `before:`
  boundaries rather than mutable `S/start` offsets. Same-second ties are explicitly excluded by
  change number as they are consumed; a pass that cannot advance safely fails without advancing the
  durable watermark. Every `service.reconcile_full_sweep_interval_seconds` (one hour by default),
  the reconciler also scans all open allowlisted Changes without the watermark so a temporarily
  stale secondary index cannot permanently age out a missed event.
- **Event stream disconnects:** reconnect with backoff; reconciler fills gaps by comparing current
  revisions with persisted jobs. The reconciler keeps a PostgreSQL watermark and overlap; after a
  long outage it resumes from the last successful pass, while first boot scans all current open
  allowlisted changes.

## 8. Stale-Patch-Set atomicity boundary

Immediately before a review POST, the service checks Gerrit `current_revision`, then re-checks the
durable job store after that Gerrit response. This prevents publishing whenever a newer Patch Set is
known to either source and closes the normal event-delivery race.

Gerrit's Set Review REST operation is nevertheless addressed to a concrete revision. A Patch Set can
be created in the very small interval after the final checks and before Gerrit commits that POST; an
external worker has no compare-and-set precondition that makes those two operations atomic. If the
team requires a mathematical guarantee that *no* comment can ever land on an immediately superseded
revision, add a minimal Gerrit-side atomic guard/endpoint. The AI workload, durable jobs, retries and
review engine remain in this independent service; the server-side piece need only compare the current
SHA and apply the supplied ReviewInput atomically.
