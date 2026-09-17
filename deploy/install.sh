#!/usr/bin/env bash
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"
source "$here/lib.sh"

if [[ -f SHA256SUMS ]]; then
  sha256sum -c SHA256SUMS
fi

detect_docker
if [[ -f docker-images/gerrit-ai-reviewer.tar ]]; then
  docker_cmd load -i docker-images/gerrit-ai-reviewer.tar
fi
if [[ -f docker-images/postgres-16.tar ]]; then
  docker_cmd load -i docker-images/postgres-16.tar
fi

exec "$here/manage.sh" install
