# Runbook — Webhook circuit breaker open / endpoint auto-disabled

**Severity:** SEV-3 (single tenant endpoint; other tenants protected).

## Alert name
`WebhookBreakerOpen`

## Metric / expression
A per-endpoint circuit breaker trips after sustained delivery failure; the endpoint is auto-disabled and the tenant notified (RFC-001 §7). Surfaced via retry/failure task metrics for the webhook queue:

```promql
sum by (task) (rate(relay_celery_tasks_total{queue=~".*webhook.*", status="retry"}[5m])) > <baseline>
```

The authoritative breaker state lives in the webhook subsystem (per-endpoint state + 30-day delivery log). Alert on the breaker-open event/gauge it emits, backed by the retry-rate above.

## Symptom / user impact
A customer's webhook endpoint has been failing (429/5xx or timeouts) long enough that the breaker opened and the endpoint was auto-disabled. That tenant stops receiving `conversation.*` events until re-enabled; other tenants are unaffected (per-endpoint isolation prevents one bad endpoint from congesting the shared queue).

## Dashboards to open
- **Per-endpoint breaker state** — open/half-open/closed, last-success timestamp.
- **Worker / errors** — `relay_celery_tasks_total{queue=~".*webhook.*", status="retry"|"failure"}`.
- **Delivery log** — 30-day per-endpoint attempt/response log.
- **Webhook delivery latency** — cross-check `slo-burn-webhook-delivery.md`.

## Diagnosis steps
1. Which endpoint(s) tripped? Identify the workspace and endpoint URL from the delivery log.
2. Failure class: 4xx (client rejecting — auth/schema), 5xx (their server erroring), timeout (their server slow), or DNS/egress (SSRF guard blocked a now-private target).
3. Was the tenant notified (auto-disable notification)?
4. Is this a one-off (endpoint maintenance) or chronic (misconfigured endpoint)?

## Mitigation
**Immediate**
- The breaker is working as designed — protecting the shared queue and the endpoint from hammering. No action needed to protect the platform.
- Backoff continues up to 72 h (RFC-001 §7); if the endpoint recovers, the breaker half-opens and re-tests.
- If the tenant fixed their endpoint, re-enable it (manual re-enable) and let backlog redeliver (at-least-once, deduped by receiver).

**Follow-up**
- Reach out to the design partner if chronic; confirm their endpoint expectations (HMAC signature verification, timestamp tolerance `webhook_signature_tolerance_seconds`).
- Verify breaker thresholds are neither too twitchy nor too slow.

## Escalation
Rarely pages. Escalate to backend on-call only if breakers open across *many* endpoints simultaneously (points to a delivery-worker bug, not customer endpoints).

## Related RFC / runbooks
- RFC-001 §7 (breaker, auto-disable, 72 h backoff, 30-day log), §6.4 (bulkheads).
- `slo-burn-webhook-delivery.md`, `celery-task-failure-rate.md`.
