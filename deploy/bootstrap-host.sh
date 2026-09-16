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
elif [[ "$(id -u "$user")" != "$uid" ]]; then
  echo "$user exists with UID $(id -u "$user"), but containers require UID $uid" >&2
  exit 2
fi

install -d -m 0750 -o "$user" -g "$user" \
  "$home/deploy" "$home/data" "$home/logs" "$home/releases"

echo "host account/layout ready under $home"
