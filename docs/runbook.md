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
`pe-review-agent` system account and the recommended directories. It does not grant interactive
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
4. Put a pinned Gerrit host key in `secrets/gerrit_known_hosts`.
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

- **Qwen unavailable/slow:** jobs remain durable and retry with backoff; no event is intentionally
  discarded.
- **Gerrit publish failure after review:** keep the stored review result and retry publishing only;
  do not rerun Qwen.
- **Worker crash:** expired job leases are reclaimed and execution resumes from durable state.
- **Newer Patch Set arrives:** old job becomes superseded; the publisher independently re-reads
  Gerrit current revision immediately before POST.
- **Event stream disconnects:** reconnect with backoff; reconciler fills gaps by comparing current
  revisions with persisted jobs.
