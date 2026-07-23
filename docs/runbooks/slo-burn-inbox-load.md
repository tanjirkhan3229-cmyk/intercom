# Runbook — SLO burn: inbox view load latency

**Severity:** SEV-2 (interactive-path SLO burn).

## Alert name
`SLOBurnInboxLoad`

## SLO
Inbox view load (50 conversations) **p95 < 300 ms** (RFC-001 §3). DB share of that budget is p95 < 100 ms (RFC-002 R1).

## Metric / expression
Histogram: `relay_http_request_duration_seconds` on the inbox list route (`app` shape).

```promql
histogram_quantile(
  0.95,
  sum by (le) (
    rate(relay_http_request_duration_seconds_bucket{route=~".*conversations.*", method="GET"}[5m])
  )
) > 0.30
```

Narrow the `route` matcher to the exact templated inbox-list path once confirmed in the exposition (e.g. `/v0/conversations`). Reads dominate the interactive path (~10:1, RFC-001 §5.1).

## Symptom / user impact
The agent inbox is slow to load/refresh. This is R1 (multi-predicate range scan, LIMIT 50, keyset). ~5k concurrent agents refresh every ~10s ⇒ ~500 read QPS (RFC-001 §5.1) — the highest-QPS interactive read.

## Dashboards to open
- **App / latency** — inbox-list p95/p99.
- **DB / saturation** — replica lag (R1 is read-your-writes for the acting agent but heavy views hit replicas), `relay_db_pool_in_use_connections`, index scan health on the inbox index.
- **Redis cache** — cached inbox counts hit rate (per-workspace cached counts, RFC-001 §5.2).

## Diagnosis steps
1. Confirm the keyset path is used (no OFFSET regression — hot paths are keyset-only per conventions).
2. Check the leading composite index is `workspace_id`-first and matches the "open convos by team/assignee ordered by waiting-since" predicate (R1).
3. Replica lag pushing replica-tolerant reads stale/slow? Check replication lag.
4. Cache: are per-workspace inbox counts being recomputed on every request (cache miss storm)?
5. `relay_db_pool_in_use_connections` — pool contention with the write path.

## Mitigation
**Immediate**
- If a bad query plan (missing/again index), and a recent deploy changed the query, roll back the deploy (`postgres`/deploy runbooks).
- If replica lag is the cause, route the acting-agent reads to the writer temporarily.

**Follow-up**
- Add/repair the R1 index; verify keyset pagination.
- Cache inbox counts per workspace with a short TTL to shed recompute load.

## Escalation
Backend on-call if p95 >300 ms persists >10 min or correlates with a deploy (candidate rollback).

## Related RFC / runbooks
- RFC-001 §3, §5.1–5.2. RFC-002 R1, §5.3 (keyset, indexes).
- `slo-burn-message-send.md`, `api-5xx-spike.md`.
