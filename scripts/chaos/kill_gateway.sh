#!/usr/bin/env bash
# CHAOS: realtime gateway (Centrifugo) OOM/down -> API unaffected, zero message loss
# (RFC-001 §9 gateway row).
#
# Proves: killing Centrifugo does not touch the source of truth. The API keeps serving
# (/healthz 200) and messages still persist, because realtime fan-out is a downstream
# effect buffered on the outbox/Redis stream (RFC-001 §6.3/§6.5), not part of the write
# transaction. Real clients reconnect with jittered backoff (see load/k6/connection_storm.js);
# server-side there is nothing to lose.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"

log "Gateway-down drill starting"
wait_for_api

CENTRIFUGO_URL="${CENTRIFUGO_URL:-http://localhost:8001}"
N_CONV=5
EXPECTED_PARTS=$(( N_CONV * 2 ))

# Centrifugo health is enabled in infra/centrifugo/config.json ("health": true) -> /health.
assert_centrifugo_health() {
  local i
  for i in $(seq 1 30); do
    if curl -fsS -o /dev/null "${CENTRIFUGO_URL}/health" 2>/dev/null; then return 0; fi
    sleep 1
  done
  return 1
}

log "asserting Centrifugo /health OK"
assert_centrifugo_health || fail "Centrifugo /health not OK before drill"
log "Centrifugo healthy"

parts_before=$(pg_count conversation_parts)
log "baseline: conversation_parts=${parts_before}"

# Always restore the gateway however the drill exits — the API/assertion steps below can fail()
# mid-run, and leaving centrifugo stopped would degrade realtime for any later run/test.
gateway_up() { "${COMPOSE[@]}" start centrifugo >/dev/null 2>&1 || true; }
trap gateway_up EXIT

log "stopping centrifugo"
"${COMPOSE[@]}" stop centrifugo >/dev/null

log "asserting API still serves with the gateway DOWN"
curl -fsS -o /dev/null "${BASE_URL}/healthz" || fail "API /healthz not 200 with gateway down"

log "driving ${N_CONV} conversations (+replies) with the gateway DOWN"
token="$(signup_token)"
contact="$(identify_contact "$token")"
for _ in $(seq 1 "$N_CONV"); do
  conv="$(create_conversation "$token" "$contact")"
  reply_conversation "$token" "$conv"
done

parts_gateway_down=$(pg_count conversation_parts)
if [ "$parts_gateway_down" -ne $(( parts_before + EXPECTED_PARTS )) ]; then
  fail "expected conversation_parts=$(( parts_before + EXPECTED_PARTS )), got ${parts_gateway_down} (writes lost with gateway down)"
fi
log "messages persisted with the gateway down (fan-out buffered on the outbox)"

log "starting centrifugo; polling health back to OK"
"${COMPOSE[@]}" start centrifugo >/dev/null
assert_centrifugo_health || fail "Centrifugo /health did not recover after restart"

parts_final=$(pg_count conversation_parts)
log "final: conversation_parts=${parts_final} (baseline ${parts_before})"
if [ "$parts_final" -ne $(( parts_before + EXPECTED_PARTS )) ]; then
  fail "conversation_parts mismatch after gateway recovery: ${parts_final} != $(( parts_before + EXPECTED_PARTS ))"
fi

pass "Gateway down -> API served, zero loss. parts ${parts_before} -> ${parts_final} (+${EXPECTED_PARTS}); clients reconnect with jittered backoff"
