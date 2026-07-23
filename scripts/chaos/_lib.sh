#!/usr/bin/env bash
# Shared helpers for the P0.12 chaos drills (RFC-001 §9, RFC-002 §9).
# Sourced by each drill; not executed directly.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE=(docker compose -f "${REPO_ROOT}/infra/docker-compose.yml")
# Pass the env file when present so `${VAR}` interpolation doesn't spam "variable not set"
# warnings (exec/stop/start don't need the values, but the parser still interpolates).
[ -f "${REPO_ROOT}/.env" ] && COMPOSE+=(--env-file "${REPO_ROOT}/.env")
BASE_URL="${BASE_URL:-http://localhost:8000}"
PG_DB="${POSTGRES_DB:-relay}"
PG_SUPER="${POSTGRES_SUPERUSER:-postgres}"

log()  { printf '\033[36m[chaos]\033[0m %s\n' "$*"; }
pass() { printf '\033[32mPASS\033[0m %s\n' "$*"; }
# fail writes to stderr so the message survives command substitution ($(pg_count ...)).
fail() { printf '\033[31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

# RLS-bypassing row count via the postgres SUPERUSER (app_rw is RLS-forced and returns 0
# without an app.ws GUC). Emits a bare integer; a non-integer/empty read is a hard FAIL so a
# broken measurement can never silently bypass a downstream `[ "$x" -ne N ]` guard.
pg_count() {
  local table="$1" out
  out="$("${COMPOSE[@]}" exec -T postgres \
    psql -U "$PG_SUPER" -d "$PG_DB" -t -A -c "SELECT count(*) FROM ${table}" | tr -d '[:space:]')"
  [[ "$out" =~ ^[0-9]+$ ]] || fail "could not read integer row count for '${table}' (got '${out}')"
  printf '%s' "$out"
}

# Scalar query -> single value. Must be non-empty (else the drill's comparison is meaningless).
pg_scalar() {
  local sql="$1" out
  out="$("${COMPOSE[@]}" exec -T postgres \
    psql -U "$PG_SUPER" -d "$PG_DB" -t -A -c "$sql" | tr -d '[:space:]')"
  [[ -n "$out" ]] || fail "empty scalar result for query: ${sql}"
  printf '%s' "$out"
}

# Wait for the API /healthz to return 200 (poll up to ~60s).
wait_for_api() {
  local i
  for i in $(seq 1 60); do
    if curl -fsS -o /dev/null "${BASE_URL}/healthz" 2>/dev/null; then return 0; fi
    sleep 1
  done
  fail "API did not become healthy at ${BASE_URL}/healthz"
}

# curl JSON POST helper -> stdout body. Args: url json [extra curl args...]
post_json() {
  local url="$1"; local body="$2"; shift 2
  curl -fsS -X POST "$url" -H 'Content-Type: application/json' -d "$body" "$@"
}

# Sign up a fresh workspace, echo the access token.
signup_token() {
  local suffix="$RANDOM-$RANDOM-$(date +%s)"
  post_json "${BASE_URL}/v0/auth/signup" \
    "{\"workspace_name\":\"chaos-${suffix}\",\"email\":\"chaos-${suffix}@example.com\",\"password\":\"password123\",\"name\":\"Chaos\"}" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])'
}

# Identify a contact, echo its id. Args: token
identify_contact() {
  local token="$1"; local suffix="$RANDOM-$RANDOM"
  post_json "${BASE_URL}/v0/contacts/identify" "{\"external_id\":\"chaos-${suffix}\"}" \
    -H "Authorization: Bearer ${token}" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])'
}

# Create a conversation, echo its id. Args: token contact_id
create_conversation() {
  local token="$1"; local contact="$2"
  post_json "${BASE_URL}/v0/conversations" "{\"contact_id\":\"${contact}\",\"body\":\"chaos msg\"}" \
    -H "Authorization: Bearer ${token}" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])'
}

# Reply on a conversation. Args: token conversation_id
reply_conversation() {
  local token="$1"; local conv="$2"
  post_json "${BASE_URL}/v0/conversations/${conv}/reply" "{\"body\":\"chaos reply\"}" \
    -H "Authorization: Bearer ${token}" >/dev/null
}
