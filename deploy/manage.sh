#!/usr/bin/env bash
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/lib.sh
source "$here/lib.sh"
cd "$here"

usage() {
  cat <<'EOF'
Usage: ./manage.sh <command> [args]

Commands:
  install              Validate, auto-remap busy first-install ports, and start
  start                Start the stack (fails rather than changing a busy port)
  stop                 Stop containers without deleting them or volumes
  restart              Recreate the stack so .env/config changes take effect
  status               Show all service/container states
  logs [service]       Show the last 200 log lines (or one service)
  follow [service]     Follow logs
  doctor               Diagnose files, images, Docker, ports, and containers
EOF
}

doctor() {
  local failures=0 reviewer admin_port worker_port
  log "deploy directory: $DEPLOY_DIR"

  for f in docker-compose.yml config.yaml .env secrets/gerrit_ssh_key secrets/gerrit_known_hosts; do
    if [[ -e "$DEPLOY_DIR/$f" ]]; then
      log "OK file: $f"
    else
      warn "MISSING file: $f"
      failures=$((failures + 1))
    fi
  done

  detect_docker
  log "OK Docker daemon"
  docker_cmd compose version

  if [[ -f "$DEPLOY_DIR/.env" ]]; then
    reviewer="$(reviewer_image)"
    if docker_cmd image inspect "$reviewer" >/dev/null 2>&1; then
      log "OK image: $reviewer"
    else
      warn "MISSING image: $reviewer"
      failures=$((failures + 1))
    fi
    if docker_cmd image inspect pe-review-postgres:16.15 >/dev/null 2>&1; then
      log "OK image: pe-review-postgres:16.15"
    else
      warn "MISSING image: pe-review-postgres:16.15"
      failures=$((failures + 1))
    fi

    admin_port="$(env_value ADMIN_PORT 8080)"
    worker_port="$(env_value WORKER_HEALTH_PORT 8081)"
    if ! service_running admin && port_listening "$admin_port"; then
      warn "admin host port $admin_port is occupied by another process"
      show_port_owner "$admin_port"
      failures=$((failures + 1))
    else
      log "OK admin port: $admin_port"
    fi
    if ! service_running worker && port_listening "$worker_port"; then
      warn "worker health host port $worker_port is occupied by another process"
      show_port_owner "$worker_port"
      failures=$((failures + 1))
    else
      log "OK worker health port: $worker_port"
    fi
  fi

  compose ps -a || true
  if (( failures > 0 )); then
    warn "doctor found $failures problem(s)"
    return 2
  fi
  log "doctor: READY"
}

cmd="${1:-help}"
shift || true

case "$cmd" in
  install)
    require_deploy_files
    detect_docker
    normalize_secret_permissions
    check_images
    # First install is allowed to heal common host-port collisions. This is
    # deliberately limited to install; normal start/restart never silently
    # change an established endpoint.
    ensure_port admin ADMIN_PORT 8080 auto
    ensure_port worker WORKER_HEALTH_PORT 8081 auto
    compose up -d
    compose ps -a
    print_access_url
    ;;
  start)
    require_deploy_files
    detect_docker
    normalize_secret_permissions
    check_images
    ensure_port admin ADMIN_PORT 8080 fail
    ensure_port worker WORKER_HEALTH_PORT 8081 fail
    compose up -d
    compose ps -a
    print_access_url
    ;;
  stop)
    detect_docker
    compose stop
    compose ps -a
    ;;
  restart)
    require_deploy_files
    detect_docker
    normalize_secret_permissions
    check_images
    compose down
    ensure_port admin ADMIN_PORT 8080 fail
    ensure_port worker WORKER_HEALTH_PORT 8081 fail
    compose up -d
    compose ps -a
    print_access_url
    ;;
  status)
    detect_docker
    compose ps -a
    print_access_url
    ;;
  logs)
    detect_docker
    if [[ $# -gt 0 ]]; then
      compose logs --tail=200 "$1"
    else
      compose logs --tail=200
    fi
    ;;
  follow)
    detect_docker
    if [[ $# -gt 0 ]]; then
      compose logs -f "$1"
    else
      compose logs -f
    fi
    ;;
  doctor)
    doctor
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage >&2
    die "unknown command: $cmd"
    ;;
esac
