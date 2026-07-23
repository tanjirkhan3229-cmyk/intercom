# Runbook — API 5xx spike / availability burn

**Severity:** SEV-1 if availability error budget burning fast; SEV-2 otherwise.

## Alert name
`API5xxSpike` / `APIAvailabilityBurn`

## SLO
API availability **99.9% monthly** (error budget ≈ 43 min/mo, RFC-001 §3).

## Metric / expression
Counter: `relay_http_requests_total` (`app` shape, labels `method`,`route`,`status`).

```promql
# 5xx ratio over 5m
sum(rate(relay_http_requests_total{status=~"5.."}[5m]))
  /
sum(rate(relay_http_requests_total[5m]))
  > 0.01
```

Use a multi-window burn-rate alert on this ratio against the 0.1% budget (fast 5m/1h page + slow 30m/6h ticket) — symptom-based, not CPU-based (RFC-001 §9).

## Symptom / user impact
Requests fail across the API surface — sends error, inbox fails to load, widget/API/webhook ingest returns 5xx. Directly burns the availability budget.

## Dashboards to open
- **App / errors** — 5xx ratio by `route` and `status` (isolate which routes and 500 vs 502/503).
- **App / traffic + latency** — is it all routes (infra) or one route (code path)?
- **Build info** — `relay_build_info{deploy_sha=...}` overlaid as a deploy marker (RFC-001 §13) — did a release cause it?
- **DB / Redis saturation** — `relay_db_pool_in_use_connections`, DB/Redis reachability.
- **Sentry** — unhandled-exception stream (release-tagged with `deploy_sha`).

## Diagnosis steps
1. Overlay the deploy marker (`relay_build_info` `deploy_sha`). A spike coincident with a new SHA ⇒ **bad deploy** → roll back.
2. All routes vs one route? All routes ⇒ dependency (DB/Redis) or infra; one route ⇒ code path — check Sentry for the exception.
3. Is a dependency down? Postgres failover (`postgres-failover.md`), Redis broker (`redis-broker-down.md`) or pub/sub (`redis-pubsub-down.md`).
4. `relay_db_pool_in_use_connections` pinned at cap ⇒ pool exhaustion (503s from timeouts).
5. Note the `MetricsMiddleware` records a `500` for any unhandled exception propagating to `ServerErrorMiddleware`, so `status="500"` counts include crashes.

## Mitigation
**Immediate**
- **Bad deploy:** auto-rollback should have fired on golden-signal regression during canary (5% app + 1 gateway, 15-min watch, RFC-001 §13). If not, manually repoint to the previous immutable image (seconds; schema is expand/contract so rollback is code-only).
- **Dependency down:** follow the specific dependency runbook.
- **Pool exhaustion:** shed load / recycle app tasks; apply rate limits.

**Follow-up**
- Post-incident: why did canary not catch it? Tighten the golden-signal regression gate.

## Escalation
Page on-call immediately on fast-burn (SEV-1). Backend on-call for code-path 500s; infra/DBA for dependency outages.

## Related RFC / runbooks
- RFC-001 §3, §9, §13 (canary + auto-rollback).
- `postgres-failover.md`, `redis-broker-down.md`, `redis-pubsub-down.md`, `slo-burn-message-send.md`.
