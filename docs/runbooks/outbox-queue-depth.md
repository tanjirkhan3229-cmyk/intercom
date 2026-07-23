# Runbook — Outbox queue depth high

**Severity:** SEV-3 (SEV-2 if sustained >15 min or climbing linearly).

## Alert name
`OutboxQueueDepthHigh`

## Metric / expression
Series: `relay_outbox_pending_rows` (gauge, `relay` shape — set by `outbox_relay._record_backlog` via `measure_backlog()`).

```promql
relay_outbox_pending_rows > 5000
  and rate(relay_outbox_pending_rows[5m]) > 0
```

`relay_outbox_pending_rows` counts unpublished `outbox` rows (rows are deleted on successful publish, so pending == everything still present). A high, *rising* depth means the relay is draining slower than the request path is enqueuing.

## Symptom / user impact
The outbox is the consistency spine (RFC-001 §6.5): fan-out, webhooks, workflow triggers, AI turns and billing meters all ride it. A deep backlog does **not** lose data (at-least-once), but it delays every downstream effect — realtime fan-out lags, webhooks land late (risking the p95 <30s SLO), workflows fire late. Depth is a *leading* indicator; `relay_outbox_oldest_age_seconds` is the *lagging* one.

## Dashboards to open
- **Relay / saturation** — `relay_outbox_pending_rows`, `relay_outbox_oldest_age_seconds`.
- **App / traffic** — `rate(relay_http_requests_total{route=~".*conversations.*"}[1m])` (is a write burst driving this?).
- **DB / saturation** — `relay_db_pool_in_use_connections` (is the writer pool starved?).
- **Redis broker/stream** — ElastiCache stream `relay:outbox` length + Redis CPU.

## Diagnosis steps
1. Is depth rising or plateaued? Rising + `oldest_age` also rising ⇒ relay stalled (go to `outbox-oldest-age.md`).
2. Rising depth but `oldest_age` low/flat ⇒ genuine write burst (import loop, campaign, noisy-neighbor tenant) outpacing a healthy relay. Confirm with the app traffic panel.
3. Check the relay process is alive and holds the advisory lock: only one instance drains (`RELAY_ADVISORY_LOCK`). Look for log `outbox.relay.already_running` (a second relay exited) or absence of `outbox.relay.published`.
4. Check the Redis stream target (`relay:outbox`) — XADD failures back the relay up.
5. Check `relay_db_pool_in_use_connections` — if the writer pool is saturated the relay's own fetch/delete slows.

## Mitigation
**Immediate**
- If a single tenant is the source (noisy neighbor, RFC-001 §9): apply the per-tenant send/API pause switch / abuse kill switch to stop the enqueue burst.
- If Redis stream is the bottleneck: verify ElastiCache health; scale the node if CPU-bound.
- The relay loops until empty, so once the source is capped, depth drains on its own within minutes.

**Follow-up**
- Tighten per-workspace rate limits if a legitimate burst crossed the envelope (RFC-000 §4: 1–5k workspaces).
- Consider raising `RELAY_BATCH` (currently 500) only if the DB can sustain larger delete batches.

## Escalation
On-call SRE → backend on-call if the relay itself is stuck (advisory lock held by a dead session, repeated crash-loop). Page if depth continues rising for >15 min after mitigation.

## Related RFC / runbooks
- RFC-001 §6.5 (outbox), §9 (queue-depth + oldest-age alarms, noisy neighbor).
- `outbox-oldest-age.md`, `redis-broker-down.md`, `slo-burn-webhook-delivery.md`, `celery-task-failure-rate.md`.
