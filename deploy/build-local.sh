#!/usr/bin/env bash
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd -- "$here/.." && pwd)"
config="${CORPORATE_CONFIG:-$here/corporate.env}"

[[ -f "$root/Dockerfile" ]] || {
  echo "build-local.sh requires a source checkout with Dockerfile at $root/Dockerfile" >&2
  echo "offline release archives already contain prebuilt image tar files; use ./install.sh there" >&2
  exit 2
}

if [[ -f "$config" ]]; then
  source "$config"
fi

source "$here/lib.sh"
detect_docker

reviewer_image="${REVIEWER_IMAGE:-gerrit-ai-reviewer:local}"
postgres_ref="${POSTGRES_REF:-postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94}"
postgres_image="${POSTGRES_IMAGE:-pe-review-postgres:16.15}"

args=()
build_sha="$(git -C "$root" rev-parse HEAD 2>/dev/null || printf 'unknown')"
if [[ -n "$(git -C "$root" status --porcelain --untracked-files=normal 2>/dev/null)" ]]; then
  build_sha="${build_sha}-dirty"
fi
build_version="$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$root/pyproject.toml" | head -n 1)"
build_version="${build_version:-0.1.0}"
args+=(--build-arg "PE_REVIEW_BUILD_SHA=$build_sha")
args+=(--build-arg "PE_REVIEW_BUILD_VERSION=$build_version")
[[ -n "${PIP_INDEX_URL:-}" ]] && args+=(--build-arg "PIP_INDEX_URL=$PIP_INDEX_URL")
[[ -n "${PIP_TRUSTED_HOST:-}" ]] && args+=(--build-arg "PIP_TRUSTED_HOST=$PIP_TRUSTED_HOST")
[[ -n "${APT_DEBIAN_MIRROR_URL:-}" ]] && args+=(--build-arg "APT_DEBIAN_MIRROR_URL=$APT_DEBIAN_MIRROR_URL")
[[ -n "${APT_DEBIAN_SECURITY_MIRROR_URL:-}" ]] && args+=(--build-arg "APT_DEBIAN_SECURITY_MIRROR_URL=$APT_DEBIAN_SECURITY_MIRROR_URL")

log "building $reviewer_image"
docker_cmd build "${args[@]}" -t "$reviewer_image" "$root"

log "preparing PostgreSQL image"
docker_cmd pull "$postgres_ref"
docker_cmd tag "$postgres_ref" "$postgres_image"

docker_cmd image inspect "$reviewer_image" >/dev/null
docker_cmd image inspect "$postgres_image" >/dev/null
log "images ready: $reviewer_image, $postgres_image"
log "next: ./configure.sh (first install) or ./install.sh"
