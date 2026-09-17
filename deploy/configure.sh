#!/usr/bin/env bash
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$here/lib.sh"
cd "$here"

force=0
ssh_key="${GERRIT_SSH_KEY_PATH:-$HOME/.ssh/id_ed25519_gerrit}"
known_hosts="${GERRIT_KNOWN_HOSTS_PATH:-$HOME/.ssh/known_hosts}"
gerrit_host="${GERRIT_SSH_HOST:-}"
gerrit_port="${GERRIT_SSH_PORT:-29418}"
gerrit_user="${GERRIT_SSH_USER:-}"
gerrit_rest_url="${GERRIT_REST_URL:-}"
rest_auth="${GERRIT_REST_AUTH_MODE:-none}"
llm_url="${LLM_BASE_URL:-}"
llm_model="${LLM_MODEL:-Qwen3.6-27B}"

usage() {
  cat <<'EOF'
Usage: ./configure.sh [options]

Creates deploy/.env, config.yaml, and deploy/secrets without modifying the
original SSH files in ~/.ssh.

Options:
  --gerrit-host HOST
  --gerrit-port PORT                 default: 29418
  --gerrit-user USER
  --gerrit-rest-url URL
  --rest-auth none|basic|bearer      default: none
  --llm-url URL
  --llm-model MODEL                  default: Qwen3.6-27B
  --ssh-key PATH                     default: ~/.ssh/id_ed25519_gerrit
  --known-hosts PATH                 default: ~/.ssh/known_hosts
  --force                            overwrite existing config.yaml
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --gerrit-host) gerrit_host="$2"; shift 2 ;;
    --gerrit-port) gerrit_port="$2"; shift 2 ;;
    --gerrit-user) gerrit_user="$2"; shift 2 ;;
    --gerrit-rest-url) gerrit_rest_url="$2"; shift 2 ;;
    --rest-auth) rest_auth="$2"; shift 2 ;;
    --llm-url) llm_url="$2"; shift 2 ;;
    --llm-model) llm_model="$2"; shift 2 ;;
    --ssh-key) ssh_key="$2"; shift 2 ;;
    --known-hosts) known_hosts="$2"; shift 2 ;;
    --force) force=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

prompt_required() {
  local var_name="$1"
  local prompt="$2"
  local current="${!var_name}"
  if [[ -n "$current" ]]; then
    return
  fi
  if [[ -t 0 ]]; then
    read -r -p "$prompt: " current
    [[ -n "$current" ]] || die "$prompt is required"
    printf -v "$var_name" '%s' "$current"
  else
    die "$prompt is required; pass the corresponding option"
  fi
}

yaml_quote() {
  python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"
}

random_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    python3 -c 'import secrets; print(secrets.token_hex(24))'
  fi
}

require_cmd python3
prompt_required gerrit_host "Gerrit SSH host"
prompt_required gerrit_user "Gerrit SSH user"
prompt_required gerrit_rest_url "Gerrit REST base URL"
prompt_required llm_url "LLM OpenAI-compatible base URL"

[[ "$gerrit_port" =~ ^[0-9]+$ ]] || die "Gerrit SSH port must be an integer"
case "$rest_auth" in
  none|basic|bearer) ;;
  *) die "--rest-auth must be none, basic, or bearer" ;;
esac

[[ -f "$ssh_key" ]] || die "SSH private key not found: $ssh_key"
[[ -f "$known_hosts" ]] || die "known_hosts not found: $known_hosts"

if [[ ! -f .env ]]; then
  env_template="$here/env.example"
  [[ -f "$env_template" ]] || env_template="$here/.env.example"
  [[ -f "$env_template" ]] || die "env example not found"
  cp "$env_template" .env
fi
chmod 600 .env

postgres_password="$(env_value POSTGRES_PASSWORD CHANGE_ME)"
if [[ -z "$postgres_password" || "$postgres_password" == "CHANGE_ME" ]]; then
  set_env_value POSTGRES_PASSWORD "$(random_secret)"
fi

admin_password="$(env_value PE_REVIEW_ADMIN_PASSWORD CHANGE_ME)"
if [[ -z "$admin_password" || "$admin_password" == "CHANGE_ME" ]]; then
  set_env_value PE_REVIEW_ADMIN_PASSWORD "$(random_secret)"
fi

if [[ "$rest_auth" == "basic" && -t 0 ]]; then
  read -r -s -p "Gerrit REST password/HTTP credential (blank keeps current .env): " rest_secret
  printf '\n'
  [[ -z "$rest_secret" ]] || set_env_value PE_REVIEW_GERRIT_HTTP_PASSWORD "$rest_secret"
elif [[ "$rest_auth" == "bearer" && -t 0 ]]; then
  read -r -s -p "Gerrit REST bearer token (blank keeps current .env): " rest_secret
  printf '\n'
  [[ -z "$rest_secret" ]] || set_env_value PE_REVIEW_GERRIT_TOKEN "$rest_secret"
fi

if [[ -t 0 ]]; then
  read -r -s -p "LLM API key (blank if not required): " llm_secret
  printf '\n'
  [[ -z "$llm_secret" ]] || set_env_value PE_REVIEW_LLM_API_KEY "$llm_secret"
fi

mkdir -p secrets
cp "$ssh_key" secrets/gerrit_ssh_key
cp "$known_hosts" secrets/gerrit_known_hosts
chmod 600 secrets/gerrit_ssh_key secrets/gerrit_known_hosts

if [[ -f config.yaml && "$force" -ne 1 ]]; then
  warn "config.yaml already exists; leaving it unchanged (use --force to regenerate)"
else
  auth_yaml="    mode: $(yaml_quote "$rest_auth")"
  if [[ "$rest_auth" == "basic" ]]; then
    auth_yaml+=$'\n'"    username: $(yaml_quote "$gerrit_user")"
    auth_yaml+=$'\n'"    password_env: \"PE_REVIEW_GERRIT_HTTP_PASSWORD\""
  elif [[ "$rest_auth" == "bearer" ]]; then
    auth_yaml+=$'\n'"    token_env: \"PE_REVIEW_GERRIT_TOKEN\""
  fi

  cat >config.yaml <<EOF
gerrit:
  ssh_host: $(yaml_quote "$gerrit_host")
  ssh_port: $gerrit_port
  ssh_user: $(yaml_quote "$gerrit_user")
  ssh_key_path: "/run/secrets/gerrit_ssh_key"
  known_hosts_path: "/run/secrets/gerrit_known_hosts"
  strict_host_key_checking: true
  rest_url: $(yaml_quote "$gerrit_rest_url")
  rest_auth:
$auth_yaml
  projects: []

llm:
  base_url: $(yaml_quote "$llm_url")
  model: $(yaml_quote "$llm_model")
  api_key_env: "PE_REVIEW_LLM_API_KEY"

database:
  host: "postgres"
  port: 5432
  username: "pe_review"
  database: "pe_review"
  password_env: "POSTGRES_PASSWORD"

review:
  output_language: "ko-KR"

service:
  enabled: true

admin:
  host: "0.0.0.0"
  port: 8080
  auth_mode: "basic"
  username: "admin"
  password_env: "PE_REVIEW_ADMIN_PASSWORD"
EOF
fi

detect_docker
ensure_port admin ADMIN_PORT 8080 auto
ensure_port worker WORKER_HEALTH_PORT 8081 auto

log "configuration files are ready"
log "SSH originals were not modified; deploy/secrets contains copies"
log "Admin password is stored in $here/.env (PE_REVIEW_ADMIN_PASSWORD)"
log "next: ./build-local.sh (if images are not built), then ./install.sh"
print_access_url
