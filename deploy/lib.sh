#!/usr/bin/env bash

# Shared helpers for deploy scripts. This file is sourced; callers should set
# `set -euo pipefail` themselves.

DEPLOY_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DOCKER=()

log() {
  printf '[pe-review] %s\n' "$*"
}

warn() {
  printf '[pe-review] WARNING: %s\n' "$*" >&2
}

die() {
  printf '[pe-review] ERROR: %s\n' "$*" >&2
  exit 2
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

detect_docker() {
  require_cmd docker
  if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
    return
  fi

  if command -v sudo >/dev/null 2>&1; then
    # Intentionally allow sudo to prompt here. This keeps all lifecycle scripts
    # usable by a normal host account that is not in the docker group.
    sudo docker info >/dev/null 2>&1 || die "Docker daemon is unavailable (docker info failed)"
    DOCKER=(sudo docker)
    return
  fi

  die "Docker daemon is unavailable and sudo is not installed"
}

docker_cmd() {
  if (( ${#DOCKER[@]} == 0 )); then
    detect_docker
  fi
  "${DOCKER[@]}" "$@"
}

compose() {
  docker_cmd compose --project-directory "$DEPLOY_DIR" -f "$DEPLOY_DIR/docker-compose.yml" "$@"
}

env_value() {
  local key="$1"
  local default="${2:-}"
  local file="$DEPLOY_DIR/.env"
  local line value

  if [[ ! -f "$file" ]]; then
    printf '%s\n' "$default"
    return
  fi

  line="$(grep -E "^[[:space:]]*${key}=" "$file" | tail -n 1 || true)"
  if [[ -z "$line" ]]; then
    printf '%s\n' "$default"
    return
  fi

  value="${line#*=}"
  value="${value%$'\r'}"
  # Strip one matching pair of simple quotes. Values used here are ports and
  # bind addresses, not secrets.
  if [[ "$value" == \"*\" && "$value" == *\" ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s\n' "$value"
}

set_env_value() {
  local key="$1"
  local value="$2"
  local file="$DEPLOY_DIR/.env"
  local tmp

  [[ -f "$file" ]] || die "missing $file"
  tmp="$(mktemp)"
  awk -v key="$key" -v value="$value" '
    BEGIN { changed = 0 }
    $0 ~ "^[[:space:]]*" key "=" {
      print key "=" value
      changed = 1
      next
    }
    { print }
    END {
      if (!changed) print key "=" value
    }
  ' "$file" >"$tmp"
  cat "$tmp" >"$file"
  rm -f "$tmp"
  chmod 600 "$file" 2>/dev/null || true
}

service_running() {
  local service="$1"
  local cid running
  cid="$(compose ps -q "$service" 2>/dev/null || true)"
  [[ -n "$cid" ]] || return 1
  running="$(docker_cmd inspect -f '{{.State.Running}}' "$cid" 2>/dev/null || true)"
  [[ "$running" == "true" ]]
}

port_listening() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -H -ltn "sport = :$port" 2>/dev/null | grep -q .
    return
  fi
  if command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | awk -v p=":$port" '$4 ~ p "$" { found=1 } END { exit !found }'
    return
  fi
  return 1
}

show_port_owner() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
      ss -ltnp "sport = :$port" 2>/dev/null || true
    elif command -v sudo >/dev/null 2>&1; then
      sudo ss -ltnp "sport = :$port" 2>/dev/null || ss -ltn "sport = :$port" 2>/dev/null || true
    else
      ss -ltn "sport = :$port" 2>/dev/null || true
    fi
  fi
}

find_free_port() {
  local preferred="$1"
  local candidate
  for candidate in "$preferred" "$((preferred + 10000))" "$((preferred + 20000))" "$((preferred + 30000))" "$((preferred + 40000))"; do
    if (( candidate <= 65535 )) && ! port_listening "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

ensure_port() {
  local service="$1"
  local env_key="$2"
  local default_port="$3"
  local mode="${4:-fail}"
  local port replacement

  port="$(env_value "$env_key" "$default_port")"
  [[ "$port" =~ ^[0-9]+$ ]] || die "$env_key must be an integer; found: $port"

  # A currently running service naturally owns its configured port, so do not
  # treat that as an external conflict.
  if service_running "$service"; then
    return 0
  fi

  if ! port_listening "$port"; then
    return 0
  fi

  warn "$env_key=$port is already in use"
  show_port_owner "$port"

  if [[ "$mode" == "auto" ]]; then
    replacement="$(find_free_port "$port")" || die "could not find a free replacement for port $port"
    set_env_value "$env_key" "$replacement"
    log "changed $env_key from $port to free port $replacement in .env"
    return 0
  fi

  die "port $port is busy; run ./set-admin-port.sh auto (for admin) or edit $DEPLOY_DIR/.env"
}

require_deploy_files() {
  [[ -f "$DEPLOY_DIR/docker-compose.yml" ]] || die "missing deploy/docker-compose.yml"
  [[ -f "$DEPLOY_DIR/config.yaml" ]] || die "missing deploy/config.yaml; run ./configure.sh first"
  [[ -f "$DEPLOY_DIR/.env" ]] || die "missing deploy/.env; run ./configure.sh first"
  [[ -f "$DEPLOY_DIR/secrets/gerrit_ssh_key" ]] || die "missing deploy/secrets/gerrit_ssh_key; run ./configure.sh first"
  [[ -f "$DEPLOY_DIR/secrets/gerrit_known_hosts" ]] || die "missing deploy/secrets/gerrit_known_hosts; run ./configure.sh first"
}

normalize_secret_permissions() {
  local file owner
  for file in "$DEPLOY_DIR/secrets/gerrit_ssh_key" "$DEPLOY_DIR/secrets/gerrit_known_hosts"; do
    owner="$(stat -c '%u' "$file")"
    if [[ "$owner" != "10001" ]]; then
      if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
        chown 10001:10001 "$file"
      elif command -v sudo >/dev/null 2>&1; then
        sudo chown 10001:10001 "$file"
      else
        die "$file must be readable by container UID 10001; sudo is required to fix ownership"
      fi
    fi
    if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
      chmod 600 "$file"
    elif command -v sudo >/dev/null 2>&1; then
      sudo chmod 600 "$file"
    else
      chmod 600 "$file" || die "failed to chmod 600 $file"
    fi
  done
}

reviewer_image() {
  env_value REVIEWER_IMAGE "gerrit-ai-reviewer:local"
}

check_images() {
  local reviewer
  reviewer="$(reviewer_image)"
  docker_cmd image inspect "$reviewer" >/dev/null 2>&1 || die "missing Docker image: $reviewer; run ./build-local.sh"
  docker_cmd image inspect pe-review-postgres:16.15 >/dev/null 2>&1 || die "missing Docker image: pe-review-postgres:16.15; run ./build-local.sh"
}

print_access_url() {
  local bind port host
  bind="$(env_value ADMIN_BIND_ADDRESS "0.0.0.0")"
  port="$(env_value ADMIN_PORT "8080")"
  host="$bind"
  if [[ "$host" == "0.0.0.0" || "$host" == "::" ]]; then
    host="SERVER_IP"
  fi
  log "Admin Web: http://${host}:${port}/"
}
