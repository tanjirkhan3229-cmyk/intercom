# Runbook — SLO burn: webhook delivery latency

**Severity:** SEV-3 (external integration lag; at-least-once, no data loss).

## Alert name
`SLOBurnWebhookDelivery`

## SLO
Webhook delivery **p95 < 30 s** from event, at-least-once (RFC-001 §3, §7 §201).

## Metric / expression
Webhook delivery runs as a Celery task off the outbox. Latency = outbox age + task queue wait + delivery attempt.

```promql
histogram_quantile(
  0.95,
  sum by (le) (
    rate(relay_celery_task_duration_seconds_bucket{queue=~".*webhook.*"}[5m])
  )
) > 30
```

Pair with the outbox lag (`relay_outbox_oldest_age_seconds`) since a stalled relay delays delivery before the task even runs. For the true event→delivered SLI, add a delivery-log-based measurement (30-day delivery log, RFC-001 §7).

## Symptom / user impact
Customer webhook endpoints receive `conversation.*` events late. At-least-once means no loss; retries use exponential backoff + jitter up to 72 h (RFC-001 §7).

## Dashboards to open
- **Worker / latency + traffic** — `relay_celery_task_duration_seconds` and `rate(relay_celery_tasks_total{queue=~".*webhook.*"}[1m])` by status.
- **Worker / errors** — `relay_celery_tasks_total{queue=~".*webhook.*", status="failure"}` and `status="retry"`.
- **Relay / saturation** — `relay_outbox_oldest_age_seconds` (upstream delay).
- **Per-endpoint breaker state** — see `webhook-breaker-open.md`.

## Diagnosis steps
1. Is the delay upstream (relay stalled) or in the worker? Check `relay_outbox_oldest_age_seconds`.
2. High `status="retry"` rate ⇒ customer endpoints are slow/5xx-ing; backoff inflates delivery time (expected, self-healing).
3. Worker queue backlog? The webhook queue is a bulkhead (segregated Celery queue). Check queue depth and worker concurrency.
4. One endpoint dominating retries? Its circuit breaker should be tripping → `webhook-breaker-open.md`.
5. Egress/SSRF guard blocking legitimate targets? Check `webhook_allow_private_targets` semantics (prod requires https + public IP).

## Mitigation
**Immediate**
- If a single slow endpoint is congesting the queue, confirm its breaker opens (auto-disable after sustained failure) to protect other tenants.
- If worker-starved, scale webhook-queue worker concurrency (bulkhead keeps other queues unaffected).

**Follow-up**
- Verify per-endpoint circuit breaker thresholds and auto-disable + tenant notification are working.
- Confirm backoff schedule (up to 72 h) and 30-day delivery log retention.

## Escalation
Backend on-call if the webhook queue backlog grows unbounded or p95 stays >30 s excluding customer-endpoint-induced backoff.

## Related RFC / runbooks
- RFC-001 §3, §7 (webhook delivery, breaker), §6.4 (bulkheads).
- `webhook-breaker-open.md`, `celery-task-failure-rate.md`, `outbox-oldest-age.md`.
