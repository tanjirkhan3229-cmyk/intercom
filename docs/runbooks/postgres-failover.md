# Runbook — Postgres writer failover

**Severity:** SEV-2 (brief write unavailability ~30s; recovers automatically).

## Alert name
`PostgresFailover`

## Metric / expression
A writer failover shows as a ~30s cliff of 5xx/timeouts on write routes plus a DB-pool spike, then recovery:

```promql
# Write-path errors during the failover window
sum(rate(relay_http_requests_total{method=~"POST|PUT|PATCH|DELETE", status=~"5.."}[1m])) > 0
```

Corroborate: `/readyz` `checks.database=false` transiently, `relay_db_pool_in_use_connections` spiking then draining, and RDS/Aurora failover events in the provider console.

## Symptom / user impact
For ~30s (RFC-001 §9) writes fail while the replica is promoted. Reads from replicas may continue. Sends/state-changes error transiently. Because mutating public endpoints accept an `Idempotency-Key` (master rule 3), client retries after the blip are safe and non-duplicating.

## Dashboards to open
- **App / errors + latency** — write-route 5xx and p95 during the window.
- **DB / saturation** — `relay_db_pool_in_use_connections`, connection reset counts.
- **Readiness** — `/readyz` `checks.database`.
- **RDS/Aurora** — failover events, replica lag, promoted-writer endpoint.

## Diagnosis steps
1. Confirm a failover actually occurred (provider events) vs a sustained DB outage.
2. Verify the app reconnected to the new writer (DNS/endpoint) after promotion; stale pooled connections should be recycled.
3. Check idempotency: retried writes must not double-apply (natural keys / dedupe ledger). W1/W2/W6 are idempotent by design (RFC-002).
4. Confirm the outbox relay (session-mode DSN, advisory lock) re-acquired its connection — see `outbox-oldest-age.md` if it stalled.

## Mitigation
**Immediate**
- Failover is automatic; primary action is to confirm recovery and that pooled connections recycled to the new writer.
- If the app pinned dead connections, recycle the affected app/worker tasks.
- Communicate the brief write blip if design partners noticed.

**Follow-up**
- Verify RPO ≤ 5 min / RTO ≤ 1 h held (RFC-001 §3, RFC-002 §9); record in the quarterly restore/rollback drill.
- Confirm Multi-AZ automatic failover config and connection-recycling settings.

## Escalation
DBA/infra on-call if the writer does not recover within ~60s or replica promotion fails (RTO risk).

## Related RFC / runbooks
- RFC-001 §3 (RPO/RTO), §9 (Postgres failover row). RFC-002 §9, master rule 3 (idempotency).
- `slo-burn-message-send.md`, `api-5xx-spike.md`, `outbox-oldest-age.md`.
- Game-day drill: `scripts/chaos/pg_failover_sim.sh` (see `docs/gameday-phase0.md`).
