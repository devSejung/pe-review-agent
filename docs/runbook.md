# Gerrit AI Reviewer Operations Runbook

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
and creates the recommended directories. If that account already exists with another UID the script
stops rather than creating an unreadable `0600` SSH-key bind mount. It does not grant interactive
login or Gerrit permissions.

## 4. Offline install

Build the release on an internet-capable machine:

```bash
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

## 5. Enable one test project first

Keep `gerrit.projects` to one test repository for the first end-to-end run. Upload a new Patch Set
and verify, in order:

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

Only then expand the project allowlist.

## 6. Health and metrics

The worker exposes localhost-only port 8080 by default:

```text
GET /healthz
GET /readyz
GET /metrics
```

Useful operational signals include queue depth, stage retry counts, review latency, Qwen latency and
token usage, publish latency, verified finding counts, duplicate events, and superseded jobs.

## 7. Incident behavior

Emergency stop: set `service.enabled: false` (or `PE_REVIEW__SERVICE__ENABLED=false`) and restart the
reviewer services. Receiver, worker and reconciler remain running but idle, so they start no new
Gerrit/LLM side effects and preserve all PostgreSQL state. Restore `true` and restart to resume.

To retry a job that is already `FAILED_PERMANENT` after the underlying problem is fixed:

```bash
docker compose run --rm worker requeue --job-id <review-job-uuid>
```

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
