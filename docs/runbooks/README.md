# Runbooks — alert index

One runbook per alert (RFC-001 §9: "runbooks per alert"). Alerts are **symptom-based** (SLO burn, queue depth, oldest-age) rather than CPU/resource alerts. Every metric name below is a real series emitted by `apps/api/src/relay/core/observability/metrics.py` (or a named audit/canary script, or a DB canary function); see `../observability.md` for the full catalog.

| Alert | Primary metric / signal | Runbook | Severity |
|---|---|---|---|
| OutboxQueueDepthHigh | `relay_outbox_pending_rows` | [outbox-queue-depth.md](outbox-queue-depth.md) | SEV-3 (→2 sustained) |
| OutboxOldestAgeHigh | `relay_outbox_oldest_age_seconds` | [outbox-oldest-age.md](outbox-oldest-age.md) | SEV-2 |
| SLOBurnMessageSend | `relay_http_request_duration_seconds` (conversations POST) | [slo-burn-message-send.md](slo-burn-message-send.md) | SEV-2 |
| SLOBurnInboxLoad | `relay_http_request_duration_seconds` (conversations GET) | [slo-burn-inbox-load.md](slo-burn-inbox-load.md) | SEV-2 |
| SLOBurnFanout | round-trip probe + `relay_outbox_oldest_age_seconds` | [slo-burn-fanout.md](slo-burn-fanout.md) | SEV-2 |
| SLOBurnWebhookDelivery | `relay_celery_task_duration_seconds` (webhook queue) | [slo-burn-webhook-delivery.md](slo-burn-webhook-delivery.md) | SEV-3 |
| API5xxSpike / APIAvailabilityBurn | `relay_http_requests_total{status=~"5.."}` | [api-5xx-spike.md](api-5xx-spike.md) | SEV-1/2 |
| RedisBrokerDown | `relay_celery_tasks_total` rate → 0 + `/readyz` redis | [redis-broker-down.md](redis-broker-down.md) | SEV-2 |
| RedisPubSubDown | `relay_outbox_oldest_age_seconds` + round-trip probe | [redis-pubsub-down.md](redis-pubsub-down.md) | SEV-2 |
| PostgresFailover | write-route 5xx + `relay_db_pool_in_use_connections` + `/readyz` database | [postgres-failover.md](postgres-failover.md) | SEV-2 |
| GatewayNodeOOM | Centrifugo node memory / restarts + round-trip probe | [gateway-oom.md](gateway-oom.md) | SEV-2 |
| SESBounceStorm | SES BounceRate + `relay_celery_tasks_total{task=~".*bounce.*"}` | [ses-bounce-storm.md](ses-bounce-storm.md) | SEV-2 |
| WebhookBreakerOpen | webhook breaker state + `relay_celery_tasks_total{queue=~".*webhook.*", status="retry"}` | [webhook-breaker-open.md](webhook-breaker-open.md) | SEV-3 |
| CeleryTaskFailureRate | `relay_celery_tasks_total{status="failure"}` | [celery-task-failure-rate.md](celery-task-failure-rate.md) | SEV-2 |
| RLSAuditFailure | `scripts/audit_rls.py` non-zero exit | [rls-audit-failure.md](rls-audit-failure.md) | **SEV-1 (security)** |
| PartitionMissing | `relay_missing_partitions(parent, n)` SQL canary function (RFC-002 §5.3) | [partition-missing.md](partition-missing.md) | SEV-2 |

## Conventions
- **Severity:** SEV-1 = page now, customer-facing or security; SEV-2 = page, degraded; SEV-3 = ticket/notify, contained.
- Every runbook: Alert name · Metric/expression (real series + PromQL) · Symptom/impact · Dashboards · Diagnosis · Mitigation (immediate + follow-up) · Escalation · Related RFC/runbooks.
- Deploy correlation: overlay `relay_build_info{deploy_sha=...}` as a deploy marker on any incident dashboard (RFC-001 §13, canary + auto-rollback).

## Related
- `../observability.md` — metric catalog, golden signals, trace map, Sentry/logging, dashboards.
- `../gameday-phase0.md` — chaos drills that exercise several of these alerts.
- `../phase0-exit-criteria.md` — Phase-0 gate evidence.
