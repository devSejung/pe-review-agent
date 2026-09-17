#!/usr/bin/env bash
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
config="${CORPORATE_CONFIG:-$here/corporate.env}"

[[ -f "$config" ]] || {
  echo "missing $config" >&2
  echo "copy corporate.env.example to corporate.env and fill mirror/CA paths" >&2
  exit 2
}

source "$config"
: "${DOCKER_REGISTRY_MIRROR:?set DOCKER_REGISTRY_MIRROR in corporate.env}"
: "${CORPORATE_CA_FILES:?set CORPORATE_CA_FILES in corporate.env}"

for cmd in python3 openssl docker update-ca-certificates; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "required command not found: $cmd" >&2; exit 2; }
done

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || { echo "run as root (sudo is not installed)" >&2; exit 2; }
  exec sudo CORPORATE_CONFIG="$config" "$here/configure-corporate-host.sh" "$@"
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

IFS=':' read -r -a ca_files <<<"$CORPORATE_CA_FILES"
(( ${#ca_files[@]} > 0 )) || { echo "no CA files configured" >&2; exit 2; }

normalized=()
idx=0
for input in "${ca_files[@]}"; do
  [[ -f "$input" ]] || { echo "CA file not found: $input" >&2; exit 2; }
  idx=$((idx + 1))
  out="$tmp/company-ca-$(printf '%02d' "$idx").crt"
  if openssl x509 -in "$input" -noout >/dev/null 2>&1; then
    openssl x509 -in "$input" -out "$out"
  elif openssl x509 -inform DER -in "$input" -noout >/dev/null 2>&1; then
    openssl x509 -inform DER -in "$input" -out "$out"
  else
    echo "not a readable X.509 certificate: $input" >&2
    exit 2
  fi
  normalized+=("$out")
done

mkdir -p /usr/local/share/ca-certificates
for cert in "${normalized[@]}"; do
  cp "$cert" "/usr/local/share/ca-certificates/pe-review-$(basename "$cert")"
done
update-ca-certificates

mirror_host="$(python3 - "$DOCKER_REGISTRY_MIRROR" <<'PY'
import sys
from urllib.parse import urlparse
u = urlparse(sys.argv[1])
if not u.netloc:
    raise SystemExit("registry mirror must be an absolute http(s) URL")
print(u.netloc)
PY
)"

mkdir -p "/etc/docker/certs.d/$mirror_host"
idx=0
for cert in "${normalized[@]}"; do
  idx=$((idx + 1))
  cp "$cert" "/etc/docker/certs.d/$mirror_host/ca-$(printf '%02d' "$idx").crt"
done

mkdir -p /etc/docker
daemon_json=/etc/docker/daemon.json
if [[ -f "$daemon_json" ]]; then
  cp "$daemon_json" "$daemon_json.bak.$(date +%Y%m%d%H%M%S)"
fi
python3 - "$daemon_json" "$DOCKER_REGISTRY_MIRROR" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
mirror = sys.argv[2]
if path.exists() and path.read_text(encoding="utf-8").strip():
    data = json.loads(path.read_text(encoding="utf-8"))
else:
    data = {}
mirrors = data.get("registry-mirrors") or []
if mirror not in mirrors:
    mirrors.insert(0, mirror)
data["registry-mirrors"] = mirrors
tmp = path.with_suffix(".json.tmp")
tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
os.replace(tmp, path)
PY

systemctl restart docker
systemctl is-active --quiet docker || { echo "Docker failed to restart" >&2; exit 2; }

mkdir -p "$here/secrets"
cat "${normalized[@]}" >"$here/secrets/corporate_ca.pem"
chmod 600 "$here/secrets/corporate_ca.pem"

echo "corporate host setup complete"
echo "Docker registry mirror: $DOCKER_REGISTRY_MIRROR"
docker info | sed -n '/Registry Mirrors:/,/Live Restore Enabled:/p'
echo "next: $here/build-local.sh"
