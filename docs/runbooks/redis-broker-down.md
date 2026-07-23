# Runbook — Redis broker down (Celery broker outage)

**Severity:** SEV-2 (async work stalls; outbox buffers, no loss).

## Alert name
`RedisBrokerDown`

## Metric / expression
The Celery broker is the Redis instance on port 6380 (dev). Symptoms surface as a task throughput collapse plus outbox backlog growth:

```promql
# Task throughput drops to ~0 while the app keeps taking traffic
sum(rate(relay_celery_tasks_total[5m])) == 0
  and sum(rate(relay_http_requests_total[5m])) > 0
```

Corroborate with `relay_outbox_pending_rows` climbing and `/readyz` `checks.redis=false`.

## Symptom / user impact
Workers can't fetch tasks; async effects (fan-out enqueues, webhooks, workflow eval, AI turns) stop being processed. **No data is lost** — must-not-lose effects were written to the `outbox` in the same txn as the domain write (RFC-001 §6.5, master rule 2), so they buffer in Postgres and drain once the broker is back. Interactive send/persist still works (that path is a DB txn, not a broker enqueue).

## Dashboards to open
- **Worker / traffic** — `rate(relay_celery_tasks_total[1m])` (flatlined).
- **Relay / saturation** — `relay_outbox_pending_rows`, `relay_outbox_oldest_age_seconds` (buffering).
- **Readiness** — `/readyz` `checks.redis`.
- **ElastiCache (broker)** — node health, failover events, CPU/memory.

## Diagnosis steps
1. Confirm broker Redis reachability (not the cache/pub-sub Redis — they are separate instances; broker is 6380).
2. Check ElastiCache for a node failure / failover in progress.
3. Confirm the app itself is healthy (it should be — only async is affected). If `/readyz` reports `redis=false`, orchestration will pull the task from rotation; that is the readiness gate working.
4. Verify outbox is buffering, not losing: `relay_outbox_pending_rows` rising is expected and correct.

## Mitigation
**Immediate**
- Restore/replace the broker Redis node (ElastiCache failover to replica).
- Once reachable, workers reconnect and drain; the relay continues buffering to the `relay:outbox` stream regardless (that is the cache/stream Redis).
- Do **not** manually replay — at-least-once + idempotent tasks (master rule 3) handle redelivery.

**Follow-up**
- Confirm Multi-AZ / automatic failover is enabled on the broker cache.
- Verify worker reconnect/backoff config avoids a thundering herd on recovery.

## Escalation
Page infra/on-call for the ElastiCache outage. Backend on-call if workers do not reconnect after the broker recovers.

## Related RFC / runbooks
- RFC-001 §6.5 (outbox buffers), §9 (Redis broker down row).
- `outbox-queue-depth.md`, `outbox-oldest-age.md`, `celery-task-failure-rate.md`.
- Game-day drill: `scripts/chaos/kill_redis_broker.sh` (see `docs/gameday-phase0.md`).
