#!/usr/bin/env bash
# CHAOS: Redis (Celery broker) down -> the API keeps accepting writes; zero loss (RFC-001 §9).
#
# RFC-001 §9 "Redis broker down": the interactive send/persist path is a single Postgres
# transaction that commits the domain row AND its outbox row together (RFC-001 §6.5), independent
# of the Celery broker. So with the broker stopped the API must keep returning 2xx and persist
# every message — nothing is lost. Broker-dependent async side effects simply queue and recover
# when the broker returns.
#
# NOTE on scope: the outbox stream/relay uses the CACHE Redis, not the broker, so the broker being
# down does not by itself exercise outbox buffering. The outbox buffer/replay + at-least-once +
# consumer-dedupe guarantee is proven separately by the P0.3 relay chaos test
# (apps/api/tests/integration/test_outbox_relay.py, which kills the relay mid-batch). This drill
# proves the complementary RFC-001 §9 claim: a broker outage never loses a committed message.
#
# ROW-COUNT ZERO-LOSS proof: conversation_parts grows by exactly the number of parts sent while the
# broker is down, and stays at that value after the broker is restored.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"

log "Redis-broker-down drill starting"
wait_for_api

N_CONV=5
EXPECTED_PARTS=$((N_CONV * 2)) # each conversation: 1 contact-comment part + 1 reply part

parts_before=$(pg_count conversation_parts)
log "baseline: conversation_parts=${parts_before}"

# Always restore the shared broker however the drill exits — its API/assertion steps below can
# fail() mid-run, and leaving the Celery broker stopped would break the whole stack's async tier.
# `docker compose start` on an already-running service is an idempotent no-op.
broker_up() { "${COMPOSE[@]}" start redis-broker >/dev/null 2>&1 || true; }
trap broker_up EXIT

log "stopping redis-broker (the Celery broker)"
"${COMPOSE[@]}" stop redis-broker >/dev/null

log "driving ${N_CONV} conversations (+replies) through the API with the broker DOWN"
token="$(signup_token)"
contact="$(identify_contact "$token")"
for _ in $(seq 1 "$N_CONV"); do
  conv="$(create_conversation "$token" "$contact")" # must still be 2xx: persists to PG + outbox
  reply_conversation "$token" "$conv"               # must still be 2xx
done
log "all writes returned 2xx while the broker was down (committed to Postgres + outbox)"

parts_during=$(pg_count conversation_parts)
if [ "$parts_during" -ne $((parts_before + EXPECTED_PARTS)) ]; then
  fail "expected conversation_parts=$((parts_before + EXPECTED_PARTS)), got ${parts_during} (writes lost while broker down)"
fi
log "conversation_parts grew to ${parts_during} (+${EXPECTED_PARTS}) with the broker down"

log "restarting redis-broker; letting async consumers reconnect"
"${COMPOSE[@]}" start redis-broker >/dev/null
sleep 5 # brief settle so workers reconnect to the fresh broker

parts_after=$(pg_count conversation_parts)
if [ "$parts_after" -ne "$parts_during" ]; then
  fail "conversation_parts changed after broker restart: ${parts_during} -> ${parts_after} (unexpected loss/dup)"
fi

pass "Redis broker down -> API kept accepting, zero loss. parts ${parts_before} -> ${parts_after} (+${EXPECTED_PARTS}); the broker outage lost no committed message"
