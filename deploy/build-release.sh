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

docker_build_args=()
build_sha="$(git -C "$root" rev-parse HEAD 2>/dev/null || printf 'unknown')"
if [[ -n "$(git -C "$root" status --porcelain --untracked-files=normal 2>/dev/null)" ]]; then
  build_sha="${build_sha}-dirty"
fi
docker_build_args+=(--build-arg "PE_REVIEW_BUILD_SHA=${build_sha}")
docker_build_args+=(--build-arg "PE_REVIEW_BUILD_VERSION=${version}")
if [[ -n "${PIP_INDEX_URL:-}" ]]; then
  docker_build_args+=(--build-arg "PIP_INDEX_URL=${PIP_INDEX_URL}")
fi
if [[ -n "${PIP_TRUSTED_HOST:-}" ]]; then
  docker_build_args+=(--build-arg "PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST}")
fi
if [[ -n "${APT_DEBIAN_MIRROR_URL:-}" ]]; then
  docker_build_args+=(--build-arg "APT_DEBIAN_MIRROR_URL=${APT_DEBIAN_MIRROR_URL}")
fi
if [[ -n "${APT_DEBIAN_SECURITY_MIRROR_URL:-}" ]]; then
  docker_build_args+=(--build-arg "APT_DEBIAN_SECURITY_MIRROR_URL=${APT_DEBIAN_SECURITY_MIRROR_URL}")
fi

docker build "${docker_build_args[@]}" -t "$image" "$root"
docker pull "$postgres_ref"
docker tag "$postgres_ref" "$postgres_image"
docker save -o "$release/docker-images/gerrit-ai-reviewer.tar" "$image"
docker save -o "$release/docker-images/postgres-16.tar" "$postgres_image"

cp "$root/deploy/docker-compose.yml" "$release/docker-compose.yml"
for script in \
  install.sh bootstrap-host.sh lib.sh manage.sh start.sh stop.sh restart.sh status.sh logs.sh doctor.sh \
  set-admin-port.sh configure.sh configure-corporate-host.sh build-local.sh; do
  cp "$root/deploy/$script" "$release/$script"
done
cp "$root/deploy/env.example" "$release/.env.example"
cp "$root/deploy/corporate.env.example" "$release/corporate.env.example"
cp "$root/config/config.example.yaml" "$release/config.example.yaml"
cp "$root/docs/runbook.md" "$release/RUNBOOK.md"
chmod +x "$release"/*.sh
sed -i "s#^REVIEWER_IMAGE=.*#REVIEWER_IMAGE=${image}#" "$release/.env.example"

(
  cd "$release"
  find . -type f ! -name SHA256SUMS -print0 \
    | sort -z \
    | xargs -0 sha256sum > SHA256SUMS
)

tar -C "$(dirname "$release")" -czf "${release}.tar.gz" "$(basename "$release")"
echo "release: ${release}.tar.gz"
