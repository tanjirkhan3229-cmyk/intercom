# Runbook — Celery task failure rate high

**Severity:** SEV-2 (async effects failing; scope depends on which task/queue).

## Alert name
`CeleryTaskFailureRate`

## Metric / expression
Counter: `relay_celery_tasks_total` (`workers`/`beat` shapes, labels `task`,`queue`,`status`; `status` ∈ `success|failure|retry`, **disjoint**). Emitted via Celery `task_postrun`/`task_retry` signals. The statuses are mutually exclusive: `success` (state `SUCCESS`), `failure` (a terminal, non-retried failure), and `retry` (a retried attempt). `_on_task_postrun` does **not** increment the counter when the task state is `RETRY`, so a retried attempt increments only `status="retry"`, never `status="failure"`. The companion latency histogram is `relay_celery_task_duration_seconds` (labels `task`,`queue`).

```promql
# Failure ratio per task over 5m — numerator counts only genuinely-failed
# (non-retried) attempts, since RETRY attempts increment status="retry" only.
sum by (task, queue) (rate(relay_celery_tasks_total{status="failure"}[5m]))
  /
sum by (task, queue) (rate(relay_celery_tasks_total[5m]))
  > 0.05
```

Also watch a rising `status="retry"` rate as an early warning before failures accumulate — a retried attempt is counted only under `status="retry"`, so it does not inflate the failure ratio.

## Symptom / user impact
A task type is failing repeatedly. Impact depends on the task: webhook delivery (external integrations lag), fan-out enqueue (realtime lag), workflow eval (automations misfire), AI turn (Aide degraded), housekeeping (partitions/backfills). Bulkheads (segregated queues, RFC-001 §6.4) contain the blast radius to one queue.

## Dashboards to open
- **Worker / errors** — `relay_celery_tasks_total{status="failure"|"retry"}` broken out by `task` and `queue`.
- **Worker / latency** — `relay_celery_task_duration_seconds` for the failing task (timeouts?).
- **Sentry** — the task's exception, release-tagged with `deploy_sha`.
- **Dependency dashboards** — DB / Redis / provider health for whatever the task calls.

## Diagnosis steps
1. Which `task` + `queue`? That localizes the subsystem and points to the specific runbook.
2. Deterministic failure (every run) or intermittent (retries eventually succeed)? Deterministic + coincident with a `deploy_sha` change ⇒ bad deploy → roll back.
3. Downstream dependency down? (DB failover, Redis, LLM provider, SES, customer webhook endpoint.)
4. Idempotency: repeated failures must not have half-applied side effects — tasks are at-least-once + idempotent (master rule 3). Verify no partial state.
5. Poison message stuck in retry loop? Check the retry count / dead-letter handling.

## Mitigation
**Immediate**
- Bad deploy: roll back to the previous immutable image.
- Dependency down: follow that dependency's runbook; retries with jittered backoff self-heal once it recovers.
- Poison task: quarantine/dead-letter it so it stops consuming retry budget.

**Follow-up**
- Fix the root cause; add a regression test.
- If a whole queue is affected, confirm bulkhead isolation held (other queues healthy).

## Escalation
Backend on-call. Page if a must-not-lose task class (billing meters W8, webhook delivery) fails persistently, or if failures span multiple queues (systemic).

## Related RFC / runbooks
- RFC-001 §6.4 (bulkheads), §9 (retries/breakers), §13 (deploy markers). Master rule 3 (idempotency).
- `webhook-breaker-open.md`, `slo-burn-webhook-delivery.md`, `redis-broker-down.md`, `postgres-failover.md`.
