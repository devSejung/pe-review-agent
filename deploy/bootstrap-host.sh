#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root" >&2
  exit 2
fi

user="pe-review-agent"
home="/home/${user}"

if ! id "$user" >/dev/null 2>&1; then
  useradd --create-home --home-dir "$home" --shell /usr/sbin/nologin "$user"
fi

install -d -m 0750 -o "$user" -g "$user" \
  "$home/deploy" "$home/data" "$home/logs" "$home/releases"

echo "host account/layout ready under $home"
