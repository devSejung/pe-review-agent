#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root" >&2
  exit 2
fi

user="pe-review-agent"
home="/home/${user}"
uid="10001"

if ! id "$user" >/dev/null 2>&1; then
  useradd --uid "$uid" --create-home --home-dir "$home" --shell /usr/sbin/nologin "$user"
else
  # The host UID does not need to match the container UID. install.sh only
  # normalizes deploy/secrets copies to container UID 10001. Never rewrite an
  # existing host account just to match the container.
  echo "$user already exists with host UID $(id -u "$user"); keeping it unchanged"
fi

install -d -m 0750 -o "$user" -g "$user" \
  "$home/deploy" "$home/data" "$home/logs" "$home/releases"

echo "host account/layout ready under $home"
