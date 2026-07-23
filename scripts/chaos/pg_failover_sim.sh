#!/usr/bin/env bash
# CHAOS: Postgres failover -> idempotency absorbs client retries (RFC-001 §9, RFC-002 §7).
#
# Models the failover window: a client sends a mutating request, the DB flaps
# (restart = connection drop, brief unavailability), the client retries the SAME request with
# the SAME Idempotency-Key. The idempotency ledger must dedupe so exactly ONE row is created,
# no matter how many times the retry lands.
#
# ROW-COUNT proof: conversation_parts increases by exactly 1 (one contact-comment part for the
# one conversation), even though the create request was sent twice with the same key.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"

log "Postgres-failover / idempotency drill starting"
wait_for_api

token="$(signup_token)"
contact="$(identify_contact "$token")"
IDEM_KEY="chaos-failover-$RANDOM-$RANDOM-$(date +%s)"

parts_before=$(pg_count conversation_parts)
conv_before=$(pg_count conversations)
log "baseline: conversations=${conv_before} conversation_parts=${parts_before}, idempotency-key=${IDEM_KEY}"

body="{\"contact_id\":\"${contact}\",\"body\":\"failover idempotent msg\"}"

log "1st create (before failover) with fixed Idempotency-Key"
first="$(post_json "${BASE_URL}/v0/conversations" "$body" \
  -H "Authorization: Bearer ${token}" -H "Idempotency-Key: ${IDEM_KEY}")"
conv_id="$(printf '%s' "$first" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')"
log "created conversation ${conv_id}"

log "restarting postgres (failover window)"
"${COMPOSE[@]}" restart postgres >/dev/null

log "waiting for pg_isready"
for i in $(seq 1 60); do
  if "${COMPOSE[@]}" exec -T postgres pg_isready -U "$PG_SUPER" -d "$PG_DB" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
"${COMPOSE[@]}" exec -T postgres pg_isready -U "$PG_SUPER" -d "$PG_DB" >/dev/null 2>&1 \
  || fail "postgres did not become ready after restart"
# API pool needs a moment to re-establish connections after the DB bounce.
wait_for_api

log "RETRY the SAME create with the SAME Idempotency-Key (post-failover)"
retry="$(post_json "${BASE_URL}/v0/conversations" "$body" \
  -H "Authorization: Bearer ${token}" -H "Idempotency-Key: ${IDEM_KEY}")"
retry_id="$(printf '%s' "$retry" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')"
log "retry returned conversation ${retry_id}"

if [ "$conv_id" != "$retry_id" ]; then
  fail "idempotency replay returned a different id (${conv_id} vs ${retry_id}) — duplicate created"
fi

conv_after=$(pg_count conversations)
parts_after=$(pg_count conversation_parts)
log "final: conversations=${conv_after} conversation_parts=${parts_after}"

if [ "$conv_after" -ne $(( conv_before + 1 )) ]; then
  fail "expected exactly 1 new conversation (idempotent), got $(( conv_after - conv_before ))"
fi
if [ "$parts_after" -ne $(( parts_before + 1 )) ]; then
  fail "expected exactly 1 new part (idempotent), got $(( parts_after - parts_before ))"
fi

pass "Postgres failover -> idempotency deduped the retry. conversations +1, parts +1 (same id ${conv_id} both times)"
