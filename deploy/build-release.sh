#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
version="${1:-0.1.0}"
if [[ ! "$version" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  echo "invalid release version: use only alphanumerics, dot, underscore, and dash" >&2
  exit 2
fi
image="gerrit-ai-reviewer:${version}"
release="$root/release/gerrit-ai-reviewer-${version}"
postgres_ref="postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94"
postgres_image="pe-review-postgres:16.15"

rm -rf "$release"
mkdir -p "$release/docker-images" "$release/secrets"

docker build -t "$image" "$root"
docker pull "$postgres_ref"
docker tag "$postgres_ref" "$postgres_image"
docker save -o "$release/docker-images/gerrit-ai-reviewer.tar" "$image"
docker save -o "$release/docker-images/postgres-16.tar" "$postgres_image"

cp "$root/deploy/docker-compose.yml" "$release/docker-compose.yml"
cp "$root/deploy/install.sh" "$release/install.sh"
cp "$root/deploy/bootstrap-host.sh" "$release/bootstrap-host.sh"
cp "$root/deploy/env.example" "$release/.env.example"
cp "$root/config/config.example.yaml" "$release/config.example.yaml"
cp "$root/docs/runbook.md" "$release/RUNBOOK.md"
chmod +x "$release/install.sh" "$release/bootstrap-host.sh"
sed -i "s#^REVIEWER_IMAGE=.*#REVIEWER_IMAGE=${image}#" "$release/.env.example"

(
  cd "$release"
  find . -type f ! -name SHA256SUMS -print0 \
    | sort -z \
    | xargs -0 sha256sum > SHA256SUMS
)

tar -C "$(dirname "$release")" -czf "${release}.tar.gz" "$(basename "$release")"
echo "release: ${release}.tar.gz"
