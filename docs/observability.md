# Observability

Golden signals per runtime shape, distributed tracing, error tracking, and structured logging for Relay — implementing the RFC-001 §9 production-readiness posture: *golden signals on every unit; structured logs with request/workspace correlation IDs; symptom-based (SLO-burn) alerts, not CPU alerts; queue-depth + oldest-age alarms; runbooks per alert.*

Source of truth: `apps/api/src/relay/core/observability/{metrics,tracing,sentry,scrub}.py`, `apps/api/src/relay/health.py`, and the settings block in `apps/api/src/relay/settings.py`.

---

## 1. Metric catalog

All series are prefixed `relay_`. Labels are deliberately **low-cardinality** — route *templates* (never raw paths with ids), task names, queue names; **never** a `workspace_id` or raw id — so the series count stays bounded at the RFC-000 §4 envelope (1–5k workspaces).

> Naming note: `prometheus_client` appends `_total` to Counter sample names. The code defines the base name (`relay_http_requests`), which is exposed as `relay_http_requests_total`. Histograms expose `_bucket`, `_sum`, `_count` suffixes.

| Series (exposition name) | Type | Labels | Meaning | Emitted by shape |
|---|---|---|---|---|
| `relay_http_requests_total` | Counter | `method`, `route`, `status` | HTTP requests handled (traffic + errors) | `app` |
| `relay_http_request_duration_seconds` | Histogram | `method`, `route` | HTTP request latency (latency) | `app` |
| `relay_celery_tasks_total` | Counter | `task`, `queue`, `status` (`success`/`failure`/`retry`) | Celery task attempts by disjoint outcome (traffic + errors) | `workers`, `beat` |
| `relay_celery_task_duration_seconds` | Histogram | `task`, `queue` | Celery task execution time (latency) | `workers`, `beat` |
| `relay_outbox_pending_rows` | Gauge | — | Unpublished outbox rows = **queue depth** (saturation) | `relay` |
| `relay_outbox_oldest_age_seconds` | Gauge | — | Age of oldest unpublished outbox row, 0 when empty (saturation / RPO signal) | `relay` |
| `relay_db_pool_in_use_connections` | Gauge | — | Checked-out writer-pool connections (saturation) | `app`, `workers` |
| `relay_build_info` | Gauge (=1) | `version`, `deploy_sha`, `environment` | Build/deploy marker; `deploy_sha` changes per release (RFC-001 §13) | all |

Notes:
- The outbox gauges are set by `outbox_relay._record_backlog()` calling `measure_backlog(conn)` on each relay loop (before and after a drain).
- `relay_db_pool_in_use_connections` and `relay_build_info` are refreshed on every `/metrics` scrape by `refresh_runtime_gauges()`.
- The `route` label is the Starlette matched route template (`/v0/contacts/{contact_id}`); unmatched requests (404) collapse to `__unmatched__`. An unhandled exception is recorded as `status="500"` by `MetricsMiddleware` before re-raising.
- Celery `status` values are **disjoint**: `success` (task state `SUCCESS`), `failure` (a terminal, non-retried failure), and `retry` (a retried attempt, emitted by `task_retry`). `_on_task_postrun` increments `success`/`failure` but does **not** increment the counter when the task state is `RETRY` — a retried attempt increments only `status="retry"`, never `status="failure"`. So a failure-ratio alert `rate(relay_celery_tasks_total{status="failure"}) / rate(relay_celery_tasks_total)` counts only genuinely-failed (non-retried) attempts.

---

## 2. Golden signals per shape

The four golden signals mapped onto each of the four runtime shapes (RFC-001 §6.1):

| Shape | Latency | Traffic | Errors | Saturation |
|---|---|---|---|---|
| `app` (HTTP) | `relay_http_request_duration_seconds` | `relay_http_requests_total` | `relay_http_requests_total{status=~"5.."}` | `relay_db_pool_in_use_connections` |
| `workers` (Celery) | `relay_celery_task_duration_seconds` | `relay_celery_tasks_total` | `relay_celery_tasks_total{status="failure"\|"retry"}` | queue depth (broker) + `relay_db_pool_in_use_connections` |
| `beat` (scheduler) | task duration (its jobs) | `relay_celery_tasks_total` | failed scheduled jobs | schedule drift |
| `relay` (outbox) | drain latency (log `outbox.relay.published`) | rows published | publish failures (oldest-age climb) | `relay_outbox_pending_rows`, `relay_outbox_oldest_age_seconds` |
| `gateway` (Centrifugo, separate) | fan-out round-trip probe | connection count / handshake rate | node restarts / OOM | node memory (4 GB/node envelope) |

Alerts derive from these signals; see `runbooks/README.md` for the alert→runbook index.

---

## 3. Exposition — how metrics are served

- **`app` shape:** `GET /metrics` (`relay.health.metrics_endpoint`) renders Prometheus exposition. Excluded from OpenAPI (`include_in_schema=False`) so it never affects the generated SDK. It calls `refresh_runtime_gauges()` (build info + DB pool) before rendering.
- **Non-HTTP shapes (`workers`/`beat`/`relay`/fanout):** call `start_metrics_server()` to expose a scrape server on `METRICS_PORT` (default **9100**). Idempotent and best-effort — a bind failure is logged, never fatal.
- **Multiprocess (prod prefork/uvicorn):** set `PROMETHEUS_MULTIPROC_DIR`; a fresh multiprocess `CollectorRegistry` is assembled per scrape so prefork children share one series set. The outbox/pool gauges use `multiprocess_mode="livemostrecent"`.

### Run locally
```bash
make dev                      # boots API + workers + relay + backing services
curl -s localhost:8000/metrics        # app-shape exposition
curl -s localhost:9100/metrics        # a non-HTTP shape's scrape server (METRICS_PORT)
curl -s localhost:8000/healthz        # liveness  → {status, version}
curl -s localhost:8000/readyz         # readiness → checks: {database, redis}
```
`metrics_enabled` (default `true`) gates all of the above; `/readyz` now checks **database + redis** and returns `degraded` if either is down (used by orchestration before routing traffic).

---

## 4. Trace map (request → outbox → worker)

OpenTelemetry, **no-op unless an OTLP endpoint is configured** (`OTEL_EXPORTER_OTLP_ENDPOINT` → `settings.otel_enabled`). When enabled, `configure_tracing()` instruments FastAPI, SQLAlchemy, httpx, Redis, and Celery.

The one hop OTel cannot see natively is the custom **outbox → Redis stream → fan-out** path, so W3C trace context is propagated by hand:

```
[client request]
  └─ FastAPI server span (FastAPIInstrumentor)
       ├─ SQLAlchemy spans (domain write + outbox INSERT, same txn)
       └─ emit() → inject_trace_context() writes the W3C carrier into the
                   outbox payload under the reserved key "_trace" (TRACE_CARRIER_KEY)
                   (no-op with no active span, so payloads are untouched when tracing is off)
                                  │
                                  ▼
[outbox relay]  (outbox_relay._publish_to_stream)
  └─ strips "_trace" from the payload BEFORE XADD  ← never leaks to consumers/clients
       └─ "outbox.publish {topic}" span, parented to the request via extract_context(carrier)
            └─ XADD → relay:outbox stream (each entry carries outbox_id for dedupe)
                                  │
                                  ▼
[Celery worker]
  └─ Celery task span (CeleryInstrumentor handles request→task propagation on its own headers)
```

Key facts:
- `TRACE_CARRIER_KEY = "_trace"` — reserved payload key; **stripped on publish** in `_publish_to_stream`, so it never reaches downstream consumers or clients.
- Tracing failures never break delivery: `_publish_span` and the inject/extract helpers swallow errors and fall through.
- Resource attributes: `service.name` (`OTEL_SERVICE_NAME`, default `relay`), `deployment.environment`, `service.version` = `deploy_sha`. Sampler = `TraceIdRatioBased(OTEL_TRACES_SAMPLER_RATIO)`.

---

## 5. Error tracking (Sentry)

`configure_sentry()` — **no-op unless `SENTRY_DSN` is set.** When on:
- Integrations: Starlette, FastAPI, Celery.
- `release = deploy_sha` (ties an error to a specific canary deploy), `environment` from settings.
- `send_default_pii=False`; **every event runs through `sentry_before_send` → `scrub()`** so PII/secrets are redacted before leaving the process.
- Request + workspace context comes from our contextvars (the same correlation ids in the logs).

---

## 6. Logging

- structlog JSON via `relay.core.logging.get_logger`; **every line carries request + workspace correlation ids** (RFC-001 §9).
- **PII scrub** (`observability/scrub.py`) is a structlog processor (`scrub_processor`) reused by Sentry (`sentry_before_send`) — one redaction pass:
  - Sensitive keys dropped wholesale (substring match): `authorization`, `cookie`, `password`, `secret`, `token`, `api_key`/`apikey`/`api-key`, `signing_key`, `encryption_key`, `hmac`, `credential`, `private_key`, `session`, `user_hash`, `key_hash`, … (`REDACTED = "***"`).
  - Emails embedded in free-text string values are masked via regex.
  - Recurses through dicts/lists/tuples. Deliberately over-redacts rather than risk a leak.

---

## 7. Recommended Grafana dashboards

Four golden-signal dashboards, one per shape (RFC-001 §9). Each panel below uses the real series.

**App (HTTP)**
- Latency: `histogram_quantile(0.95, sum by (le,route) (rate(relay_http_request_duration_seconds_bucket[5m])))`
- Traffic: `sum by (route) (rate(relay_http_requests_total[1m]))`
- Errors: `sum(rate(relay_http_requests_total{status=~"5.."}[5m])) / sum(rate(relay_http_requests_total[5m]))`
- Saturation: `relay_db_pool_in_use_connections`
- Deploy markers: annotate from `relay_build_info` `deploy_sha` changes.

**Worker (Celery)**
- Latency: `histogram_quantile(0.95, sum by (le,task) (rate(relay_celery_task_duration_seconds_bucket[5m])))`
- Traffic: `sum by (task,queue) (rate(relay_celery_tasks_total[1m]))`
- Errors: `sum by (task) (rate(relay_celery_tasks_total{status="failure"}[5m]))` and `status="retry"`
- Saturation: broker queue depth + `relay_db_pool_in_use_connections`

**Relay (outbox)**
- Saturation (primary): `relay_outbox_pending_rows`, `relay_outbox_oldest_age_seconds`
- Traffic: rows published (from `outbox.relay.published` log-based metric)
- Errors: oldest-age climb with flat throughput = stalled relay

**Gateway (Centrifugo)**
- Latency: fan-out round-trip probe p95 (sender→subscriber)
- Traffic: connection count, handshake rate (reconnect storm)
- Errors: node restarts / OOM count
- Saturation: per-node memory vs 4 GB envelope

---

## 8. Alert → runbook index

Every alert has a runbook (`docs/runbooks/`). Full table with metrics + severity: [`runbooks/README.md`](runbooks/README.md).

Quick map: outbox depth/age → `outbox-*`; SLO burns → `slo-burn-*`; availability → `api-5xx-spike`; dependency outages → `redis-broker-down` / `redis-pubsub-down` / `postgres-failover` / `gateway-oom`; async failures → `celery-task-failure-rate` / `webhook-breaker-open` / `ses-bounce-storm`; security/canary → `rls-audit-failure` / `partition-missing`.

---

## 9. Settings reference (observability block)

From `apps/api/src/relay/settings.py`:

| Setting | Default | Effect |
|---|---|---|
| `METRICS_ENABLED` | `true` | Gate `/metrics` + `start_metrics_server()` |
| `METRICS_PORT` | `9100` | Scrape port for non-HTTP shapes |
| `PROMETHEUS_MULTIPROC_DIR` | unset | Enables multiprocess registry (prod) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | Enables tracing (`otel_enabled` when set) |
| `OTEL_SERVICE_NAME` | `relay` | Trace `service.name` |
| `OTEL_TRACES_SAMPLER_RATIO` | `1.0` | Trace sampling ratio |
| `SENTRY_DSN` | unset | Enables Sentry |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.0` | Sentry perf tracing sample rate |
| `DEPLOY_SHA` | `unknown` | `relay_build_info` label + Sentry release + OTel `service.version` |

No secrets are baked in — all secrets come from env / AWS Secrets Manager (RFC-001 §13).
