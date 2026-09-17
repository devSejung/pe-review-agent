#!/usr/bin/env bash
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/lib.sh
source "$here/lib.sh"

value="${1:-auto}"
[[ -f "$here/.env" ]] || die "missing deploy/.env; run ./configure.sh first"

if [[ "$value" == "auto" ]]; then
  current="$(env_value ADMIN_PORT 8080)"
  value="$(find_free_port "$current")" || die "could not find a free admin port"
else
  [[ "$value" =~ ^[0-9]+$ ]] || die "port must be an integer or 'auto'"
  (( value >= 1 && value <= 65535 )) || die "port must be between 1 and 65535"
  port_listening "$value" && die "port $value is already in use"
fi

set_env_value ADMIN_PORT "$value"
log "ADMIN_PORT=$value"
log "restart with ./restart.sh if the stack is already running"
