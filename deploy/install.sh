#!/usr/bin/env bash
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

if [[ -f SHA256SUMS ]]; then
  sha256sum -c SHA256SUMS
fi

if [[ -f docker-images/gerrit-ai-reviewer.tar ]]; then
  docker load -i docker-images/gerrit-ai-reviewer.tar
fi
if [[ -f docker-images/postgres-16.tar ]]; then
  docker load -i docker-images/postgres-16.tar
fi

test -f config.yaml || { echo "missing deploy/config.yaml" >&2; exit 2; }
test -f .env || { echo "missing deploy/.env" >&2; exit 2; }
test -f secrets/gerrit_ssh_key || { echo "missing Gerrit SSH key" >&2; exit 2; }
test -f secrets/gerrit_known_hosts || { echo "missing Gerrit known_hosts" >&2; exit 2; }

chmod 600 secrets/gerrit_ssh_key
docker compose up -d
