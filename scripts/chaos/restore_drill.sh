#!/usr/bin/env bash
# CHAOS: backup restore rehearsal + row-count checksum (RFC-002 §9).
#
# Proves the backup/restore path is exercisable and lossless: pg_dump the live `relay` DB,
# restore it into a scratch `relay_restore` DB, and compare row counts of the key tables
# between source and restore. Equal counts = ROW-COUNT CHECKSUM pass. Scratch DB is dropped
# at the end (even on failure).
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"

RESTORE_DB="relay_restore"
# NB: `outbox` is deliberately EXCLUDED. It is an actively-drained queue: the outbox-relay DELETEs
# rows continuously, so between the pg_dump snapshot (T0) and the live source count (T1) its count
# moves — comparing a frozen snapshot against a moving target yields spurious mismatches. The
# checksum uses only tables with no background mutation during the drill window.
KEY_TABLES=(conversations conversation_parts contacts)

cleanup() {
  "${COMPOSE[@]}" exec -T postgres \
    psql -U "$PG_SUPER" -d postgres -c "DROP DATABASE IF EXISTS ${RESTORE_DB}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

log "Restore-drill starting"
wait_for_api

# Seed a little data so the checksum is meaningful even on a fresh volume.
log "seeding a conversation + reply so the checksum is non-trivial"
token="$(signup_token)"
contact="$(identify_contact "$token")"
conv="$(create_conversation "$token" "$contact")"
reply_conversation "$token" "$conv"

log "dumping the live '${PG_DB}' database"
"${COMPOSE[@]}" exec -T postgres \
  pg_dump -U "$PG_SUPER" -d "$PG_DB" -Fc -f /tmp/relay_chaos.dump

log "(re)creating scratch database '${RESTORE_DB}'"
cleanup
"${COMPOSE[@]}" exec -T postgres \
  psql -U "$PG_SUPER" -d postgres -c "CREATE DATABASE ${RESTORE_DB}" >/dev/null

log "restoring the dump into '${RESTORE_DB}'"
# pg_restore exits non-zero on benign role/ownership notices; --no-owner keeps it clean, and
# we verify success by the row-count checksum below regardless.
"${COMPOSE[@]}" exec -T postgres \
  pg_restore -U "$PG_SUPER" -d "$RESTORE_DB" --no-owner --no-privileges /tmp/relay_chaos.dump \
  >/dev/null 2>&1 || log "pg_restore emitted notices (non-fatal); verifying by checksum"

log "comparing row counts (ROW-COUNT CHECKSUM)"
ok=1
for t in "${KEY_TABLES[@]}"; do
  src=$("${COMPOSE[@]}" exec -T postgres psql -U "$PG_SUPER" -d "$PG_DB" -t -A \
        -c "SELECT count(*) FROM ${t}" | tr -d '[:space:]')
  dst=$("${COMPOSE[@]}" exec -T postgres psql -U "$PG_SUPER" -d "$RESTORE_DB" -t -A \
        -c "SELECT count(*) FROM ${t}" | tr -d '[:space:]')
  # A garbled/empty read must FAIL, never pass as ""="" (else the checksum is meaningless).
  [[ "$src" =~ ^[0-9]+$ && "$dst" =~ ^[0-9]+$ ]] || fail "non-integer count for ${t} (src='${src}' dst='${dst}')"
  if [ "$src" = "$dst" ]; then
    log "  ${t}: source=${src} restore=${dst}  OK"
  else
    log "  ${t}: source=${src} restore=${dst}  MISMATCH"
    ok=0
  fi
done

# tidy the dump file inside the container
"${COMPOSE[@]}" exec -T postgres rm -f /tmp/relay_chaos.dump >/dev/null 2>&1 || true

[ "$ok" -eq 1 ] || fail "row-count checksum mismatch between source and restore"
pass "Restore rehearsal -> row-count checksum matched for: ${KEY_TABLES[*]}"
