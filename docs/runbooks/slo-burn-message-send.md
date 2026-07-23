# Runbook — SLO burn: message send latency

**Severity:** SEV-2 (interactive-path SLO burn).

## Alert name
`SLOBurnMessageSend`

## SLO
Message send: persist + ack **p95 < 250 ms** (RFC-001 §3).

## Metric / expression
Histogram: `relay_http_request_duration_seconds` (`app` shape, labels `method`,`route`). Send is a POST to the conversations route family (templated route label, never a raw path).

```promql
histogram_quantile(
  0.95,
  sum by (le) (
    rate(relay_http_request_duration_seconds_bucket{route=~".*conversations.*", method="POST"}[5m])
  )
) > 0.25
```

Prefer a multi-window burn-rate alert (fast 5m/1h + slow 30m/6h) against the 99.9% availability budget rather than a raw threshold, per the symptom-based posture (RFC-001 §9).

## Symptom / user impact
Agents and visitors see slow message sends; the send spinner lags. This is workload-1 (the interactive path RFC-001 §1 says must stay fast). W1 is a 3-row txn (message insert + conversation head update + outbox) budgeted at <50 ms DB commit (RFC-002 W1).

## Dashboards to open
- **App / latency** — send p95/p99 by route.
- **App / errors** — `relay_http_requests_total{route=~".*conversations.*", status=~"5.."}` (are slow requests also failing?).
- **DB / saturation** — `relay_db_pool_in_use_connections` (pool exhaustion is the usual cause), Postgres commit latency, lock waits on `conversations` (hot-update bloat, RFC-002 §3 pressure #3).
- **Trace view** — a slow send trace (request → SQLAlchemy spans) pinpoints DB vs app time.

## Diagnosis steps
1. Is it latency-only or latency+errors? Errors ⇒ jump to `api-5xx-spike.md`.
2. `relay_db_pool_in_use_connections` near the pool cap ⇒ writer-pool contention; check for a long-running txn or a noisy-neighbor import (W2 burst).
3. Postgres: check commit latency, autovacuum on `conversations`, lock waits. Hot-update bloat is a named pressure (RFC-002 §3).
4. Is a Postgres failover in progress (~30s writer unavailability)? See `postgres-failover.md`.
5. Traces: is time in DB, in outbox insert, or in app CPU (GC, serialization)?

## Mitigation
**Immediate**
- If a tenant import/bot storm is driving writer contention: apply per-workspace rate limit / abuse kill switch (RFC-001 §9 noisy neighbor).
- If pool-starved: verify no leaked/long-held connections; recycle the app tasks if a connection leak is confirmed.
- If mid-failover: expect ~30s recovery; idempotency keys make client retries safe.

**Follow-up**
- Tune autovacuum/fillfactor on `conversations` if bloat recurs (RFC-002 §3).
- Right-size the writer pool / add read replicas for the read side to relieve the writer.

## Escalation
Backend on-call if p95 stays >250 ms for >10 min after ruling out a transient failover, or if the availability error budget burn-rate alert also fires.

## Related RFC / runbooks
- RFC-001 §1, §3 (SLO). RFC-002 W1, §3 (pressure ranking).
- `api-5xx-spike.md`, `postgres-failover.md`, `slo-burn-inbox-load.md`.
