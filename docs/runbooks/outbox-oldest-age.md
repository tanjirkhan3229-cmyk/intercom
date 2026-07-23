# Runbook — Outbox oldest-message age high (relay stalled)

**Severity:** SEV-2 (relay is not draining — durability RPO clock is ticking).

## Alert name
`OutboxOldestAgeHigh`

## Metric / expression
Series: `relay_outbox_oldest_age_seconds` (gauge, `relay` shape — `COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)), 0)` over unpublished rows; `0` when empty).

```promql
relay_outbox_oldest_age_seconds > 120
```

Escalate the threshold toward the durability budget: `> 300` (5 min) breaches RPO expectations (RFC-001 §3 / RFC-002 §9) — the oldest un-fanned-out effect is older than the tolerated window.

## Symptom / user impact
The relay has **stalled** — the oldest row is aging with no publishes. Unlike depth (which can be a healthy burst), a rising oldest-age with flat throughput means nothing is draining. Fan-out, webhooks, workflows, AI turns and billing meters for the oldest events are all stuck. Common root cause: the relay process is dead, or the cache/broker Redis it publishes to is unreachable.

## Dashboards to open
- **Relay / saturation** — `relay_outbox_oldest_age_seconds`, `relay_outbox_pending_rows`.
- **Relay logs** — expect periodic `outbox.relay.published rows=N`; its *absence* is the tell.
- **Redis stream** — `relay:outbox` XADD health, ElastiCache reachability.
- **DB** — advisory-lock holders, `relay_db_pool_in_use_connections`.

## Diagnosis steps
1. Is the relay process running and did it acquire the advisory lock? Look for `outbox.relay.started`; check no stale session holds `RELAY_ADVISORY_LOCK` (0x0075_7462_6F78).
2. Is Redis reachable from the relay host? A publish failure to the `relay:outbox` stream keeps rows in place (correct at-least-once behavior) but stalls oldest-age → see `redis-broker-down.md`.
3. Session-mode connection issue? The relay needs a session-mode DSN (LISTEN + advisory lock, RFC-002 §9). If it landed on a transaction-pooled DSN, LISTEN/advisory-lock break.
4. Is the DB writer reachable (or mid-failover)? See `postgres-failover.md`.
5. Check for a poison row: a payload that repeatedly fails to serialize/XADD would block the head (drain is `ORDER BY aggregate_id, seq, id`).

## Mitigation
**Immediate**
- Restart the relay process. On restart it re-acquires the advisory lock and loops until empty; a crash between publish and delete only redelivers (consumers dedupe on `outbox_id`).
- If Redis is down, restore/point the relay at a healthy Redis; buffered rows drain automatically.
- If a stale advisory lock is held by a dead backend, terminate that backend (`pg_terminate_backend`) so a fresh relay can take the lock.

**Follow-up**
- If a poison row blocked the head, quarantine it and file a bug on the emitter.
- Add a watchdog alert on *absence* of `outbox.relay.published` for N minutes.

## Escalation
Page backend on-call immediately if `relay_outbox_oldest_age_seconds > 300` (RPO risk) or if restart does not resume publishing within 5 min.

## Related RFC / runbooks
- RFC-001 §6.5 (relay, at-least-once), §3/§9 (RPO, oldest-age alarm). RFC-002 §9 (session-mode, durability).
- `outbox-queue-depth.md`, `redis-broker-down.md`, `postgres-failover.md`.
