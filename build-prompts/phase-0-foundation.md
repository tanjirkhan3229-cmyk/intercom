# Phase 0 — Foundation & Core Loop (months 0–3)

Goal: a visitor messages in the widget → an agent answers in the inbox → history persists — plus email, help center, billing, and a public API skeleton. Exit criteria: RFC-000 §5 Phase 0.

Run in order. ∥ marks prompts that can run in parallel worktrees once their dependency is met.

---

### P0.0 — Monorepo scaffold, CI, and CLAUDE.md
**Depends on:** nothing · **Read first:** `rfcs/RFC-001-system-architecture.md` §6.1–6.2, §13

> Scaffold the Relay monorepo exactly to RFC-001's runtime shapes:
>
> - `apps/api` — FastAPI (Python 3.12, uv, SQLAlchemy 2 async + asyncpg, Pydantic v2, Alembic, Celery). Modular-monolith layout: `src/modules/{identity,crm,messaging,channels,tickets,knowledge,ai,automation,outbound,reporting,platform,billing}` each exposing `router.py`, `service.py`, `models.py`, `events.py`. Add **import-linter** contracts forbidding cross-module imports except via `service` interfaces or the shared `core` package.
> - `apps/web` — Next.js 15 (App Router, TypeScript, Tailwind, shadcn/ui): agent app shell (CSR behind auth) + marketing placeholder (SSG).
> - `apps/widget` — Vite + Preact (bundle-size budget 50 KB gz): loader snippet + iframe app placeholder.
> - `packages/` — `sdk-ts` (generated API client), `shared` (design tokens, types).
> - `infra/` — docker-compose for dev: Postgres 16 (with pgvector), Redis ×2 (cache/broker), MinIO (S3), Mailpit (SMTP sink). One-command bootstrap: `make dev`.
> - CI (GitHub Actions): cheapest-first jobs — ruff+mypy / eslint+tsc → unit tests → integration tests (testcontainers Postgres+Redis) → build images (build-once, tagged by SHA). Path-filtered so widget changes don't rebuild the API.
> - Alembic wired with a migration wrapper that sets `lock_timeout='2s'`, `statement_timeout='30s'`, and forbids non-`CONCURRENTLY` index creation on tables > 1M rows (lint rule).
> - Write `CLAUDE.md` at repo root encoding the Master Rules from `build-prompts/README.md`, the module map, the layout above, and the dev commands.
>
> **Acceptance:** `make dev` boots the stack; CI green on a hello-world endpoint + page; import-linter fails a deliberate cross-module import in a test PR; CLAUDE.md exists and is accurate.

### P0.1 — Identity, tenancy & the RLS regime
**Depends on:** P0.0 · **Read first:** RFC-002 §5.1, §7, §10; RFC-001 §10

> Build the `identity` module: `workspaces`, `admins`, `memberships` (roles: owner/admin/agent/restricted), `teams`, `team_memberships`, `api_keys` (hashed). UUIDv7 PKs via a shared `uuid7()` helper; public IDs as prefixed base62 (`wrk_`, `adm_`…).
>
> Auth: email+password (argon2) and Google OIDC; short-lived access JWT (15 min) + rotating refresh tokens (httpOnly); session middleware that opens every request's DB transaction with `SET LOCAL app.ws = :workspace_id` derived from the authenticated principal — **no query path may touch a tenant table without it** (add a pytest fixture that asserts this by running with an unset GUC and expecting zero rows).
>
> Enable + FORCE row-level security on every tenant table created from now on; write the `ws_isolation` policy template and a `create_tenant_table()` Alembic helper that applies it automatically. Roles per RFC-002 §10: `app_rw`, `app_ro`, `migrator` (BYPASSRLS, migrations only).
>
> **Acceptance:** cross-tenant test suite (two workspaces, every endpoint) proves zero leakage with RLS on AND with the app-layer filter deliberately removed (RLS catches it); auth flows tested incl. refresh rotation + revocation; role checks enforced in service layer with one choke-point helper.

### P0.2 — CRM core: contacts, companies, attributes, events
**Depends on:** P0.1 · **Read first:** RFC-002 §5.4; RFC-001 §5.3

> Build the `crm` module per RFC-002 §5.4 DDL: `contacts` (users/leads, citext email, partial unique indexes exactly as specced), `companies`, `contact_companies`, `attribute_definitions` (typed: string/number/boolean/date/list), JSONB `custom` validated against definitions at write time (422 on type mismatch), trigram index for name typeahead.
>
> APIs: `POST /contacts/identify` (idempotent upsert per RFC-002 W2, merging rules documented), `POST /events/track` (accepts batches; buffers to Redis list; a Celery `analytics` task drains via COPY into the monthly-partitioned `events` table per RFC-002 §5.4), basic CRUD + list with keyset pagination.
>
> Include the partition automation: `housekeeping` task pre-creates events partitions T+2 months; alert hook if missing.
>
> **Acceptance:** identify called twice with same external_id = one contact; 10k-event batch lands via COPY in one txn per chunk; type-mismatched custom attribute rejected; `EXPLAIN` test proves typeahead uses the trigram index.

### P0.3 — Messaging core: conversations, parts, outbox
**Depends on:** P0.2 · **Read first:** RFC-002 §5.3, §5.6 (outbox, idempotency_keys); RFC-001 §6.3, §6.5

> Build the `messaging` module exactly to RFC-002 §5.3: `conversations` (state machine open/snoozed/closed enforced by CHECK + service layer; fillfactor 85), monthly-partitioned `conversation_parts` with the specced PK `(created_at, id)` and indexes, `attachments` metadata (S3 refs), `conversation_tags`, `saved_replies`.
>
> Implement W1 as one transaction: insert part → update conversation head (`last_part_at`, `waiting_since` rules: set on contact part, cleared on admin comment) → insert `outbox` row(s). Implement the `outbox` table + relay worker (LISTEN/NOTIFY-woken, poll fallback, per-aggregate ordering by `(aggregate_id, seq)`, aggressive cleanup of published rows) and the `idempotency_keys` table honored by all mutating endpoints via an `Idempotency-Key` header decorator.
>
> Part types this phase: comment, note, assignment, state_change, rating. Assignment ops: manual + round-robin per team (atomic claim via `UPDATE … WHERE assignee_id IS NULL RETURNING`).
>
> **Acceptance:** duplicate send with same idempotency key returns the original part, exactly one row; outbox relay chaos test (kill relay mid-batch, restart) delivers at-least-once with consumer dedupe proven; partial-index `EXPLAIN` test for the R1 inbox query shows Index Scan, no Sort; state-machine violations rejected at both service and DB layer.

### P0.4 — Realtime tier
**Depends on:** P0.3 · **Read first:** RFC-001 §5.2, §6.1 (gateway row), §6.3, §9 (Redis pub/sub row)

> Integrate **Centrifugo** as the realtime gateway (docker-compose service + Terraform stub): Redis engine; channel scheme `conv:{conversation_id}` and `inbox:{workspace_id}:{team_id}`; per-connection JWTs and per-channel subscribe tokens minted by the API (authz: agents → their workspace channels; widget contacts → only their own `conv:` channels).
>
> API publishes on outbox consumption (not inline) — build the `realtime-fanout` consumer. Typing indicators + presence: Redis-only with TTL (RFC-002 §2 note), relayed through Centrifugo, never persisted. Client fallback: long-poll endpoint `GET /conversations/:id/parts?after=` behind the `realtime_fallback` Unleash flag; both web and widget clients must auto-downgrade on websocket failure.
>
> **Acceptance:** two browser sessions see each other's messages < 1 s locally; a widget token cannot subscribe to another conversation's channel (test); killing Centrifugo flips clients to polling without message loss (outbox replay covers the gap); reconnect uses jittered backoff.

### P0.5 — Inbox app v1 ∥ (with P0.6)
**Depends on:** P0.4 · **Read first:** RFC-001 §3 (SLO table), §6.1 (`web` row); RFC-000 §2.2

> Build the agent Inbox in `apps/web`: three-pane layout (views sidebar / conversation list / thread + contact side panel). Views this phase: You, Unassigned, Team inboxes, All open, Snoozed, Closed. Conversation list ordered by `waiting_since` with realtime updates and unread indicators (Redis-backed counts). Thread: parts timeline with keyset infinite scroll, composer (reply vs note toggle, ⌘Enter send, attachment upload via presigned S3), macros picker (`/` trigger, variable interpolation), tags, assignment menu, snooze presets, close/reopen. Contact side panel: profile, custom attributes, recent conversations, recent events. Keyboard shortcuts: j/k navigate, a assign, s snooze, e close, r reply, n note.
>
> State: TanStack Query + the realtime subscription updating the cache; optimistic sends reconciled by part id. All list endpoints keyset-paginated. Empty/loading/error states designed, not defaulted.
>
> **Acceptance:** Playwright e2e: visitor message (simulated via API) appears in Unassigned < 1 s, agent assigns→replies→closes entirely by keyboard; list virtualizes at 1k conversations without jank; refresh restores exact view state.

### P0.6 — Messenger widget v1 ∥ (with P0.5)
**Depends on:** P0.4 · **Read first:** RFC-000 §2.1; RFC-001 §6.3, §9 (widget bundle row), §10 (identity verification)

> Build `apps/widget`: a ≤5 KB loader snippet (`relay('boot', {app_id, user, user_hash})`) that injects an iframe app. Boot API: verify `user_hash` = HMAC-SHA256(secret, external_id) when identity verification is enabled; otherwise create/resume a cookie-scoped lead. Widget UI: launcher bubble (position/color themable from workspace settings), conversation list + thread, composer with attachments, typing indicators, delivery/read states, conversation rating prompt on close, unread badge, office-hours/expected-reply-time header stub (schedule model lands in P1.7 — read from settings if present).
>
> Build pipeline: versioned immutable bundles (`widget/v{semver}/relay.js`) to S3+CloudFront with a rollout pointer file per RFC-001 §9 (cohort-staged, instant rollback); CSP-safe (no inline eval), i18n-ready strings.
>
> **Acceptance:** demo host page boots widget in < 300 ms on cable-profile throttling; HMAC mismatch rejected (test); a lead's cookie session survives reload; bundle-size CI budget enforced; rollback pointer flip verified on staging CDN.

### P0.7 — Email channel v1
**Depends on:** P0.3 · **Read first:** RFC-001 §6.6 (email), §9 (SES row); RFC-002 §5.6 (channel tables)

> Build the `channels` module's email adapter. **Inbound:** SES receipt → S3 raw MIME → SNS → `ingest` task: parse (python `email` + html→text), thread via `In-Reply-To`/`References` + plus-addressed reply tokens (`reply+{conv_token}@…`), dedupe on Message-ID (unique index), create/append conversation with `channel='email'`, store attachments to S3. Malformed MIME goes to DLQ with alert, never poisons the queue. **Outbound:** agent replies render through a minimal HTML template, sent via SES with per-workspace verified domains (DKIM/SPF setup flow with DNS-record UI + verification poller), configuration-set event webhooks → `suppressions` (hard bounce/complaint permanent, tenant-visible list, sends to suppressed addresses blocked at service layer).
>
> **Acceptance:** round-trip e2e on staging (send → reply → threads correctly); duplicate SNS delivery creates no duplicate part; bounce marks suppression and blocks the next send with a clear API error; 50 MB attachment rejected politely.

### P0.8 — Help Center v1 ∥ (with P0.5–P0.7)
**Depends on:** P0.2 · **Read first:** RFC-000 §2.5; RFC-002 §5.5 (articles only — chunks/embeddings land in P1.1); RFC-001 §6.1 (`web` ISR row)

> Build `knowledge` module v1: `collections`, `articles` (draft/published, block-based body JSON, per-article SEO fields), `article_translations` (schema only; UI later). Editor in the agent app: block editor (headings, lists, images via S3, callouts, code), autosave drafts, publish flow.
>
> Hosted site: Next.js ISR multi-tenant routes `{workspace-slug}.relayhc.com` — collection index, article page, search (Postgres FTS over title+body with `websearch_to_tsquery`), workspace theming (logo, colors), sitemap + meta tags. Custom domains deferred to phase 2 (schema field exists). Widget integration: search + article viewer tab calling the same API.
>
> **Acceptance:** publish → live on ISR site ≤ 60 s (revalidate hook on publish); FTS returns title matches above body matches (rank test); unpublished articles 404 publicly but preview for logged-in admins; Lighthouse ≥ 95 on article page.

### P0.9 — Reporting v0 + queue monitor
**Depends on:** P0.3 · **Read first:** RFC-002 §5.6 (reporting tables), §2 R4/R9; RFC-000 §2.9

> Build `reporting` v0: `conversation_metrics` upserted by an outbox consumer on close/reopen (first_response_s, resolution_s, replies count, rating); `daily_rollups` computed by an `analytics` task (idempotent re-runs by day window). Endpoints + agent-app pages: volume over time, responsiveness (median/p90 first response), CSAT summary — filterable by team/date. Realtime queue monitor: open/unassigned counts, longest wait, agents online (Redis presence) — served from cached counts (R4), refreshed ≤ 10 s.
>
> **Acceptance:** metrics reconcile against a fixture set (hand-computed expected values); rollup task re-run produces identical rows (idempotent); queue monitor updates without page reload; no reporting query touches `conversation_parts` raw (assert via pg_stat_statements in the integration run).

### P0.10 — Billing v1 ∥
**Depends on:** P0.1 · **Read first:** RFC-002 §5.6 (billing tables); RFC-000 §8 (pricing open question — implement seats now, meters-ready)

> Build `billing`: Stripe Checkout + customer portal integration; `plans`, `subscriptions`, seat counting (active memberships) synced to Stripe subscription quantity daily + on change; `usage_records` table per RFC-002 W8 (append-only, negative-row corrections) with a generic meter interface — Aide resolutions plug in during P1.3. Trial logic (14 days), plan gates as a `Entitlements` service consulted by feature flags, dunning webhooks → workspace banner state.
>
> **Acceptance:** Stripe test-clock run: trial → subscribe → seat add → payment fail → recovery, all states reflected in-app; usage_records survive a duplicate webhook (idempotent by event id); no Stripe call inside a request-path transaction (async via outbox).

### P0.11 — Public API v0 + webhooks
**Depends on:** P0.3 · **Read first:** RFC-000 §2.10; RFC-001 §6.7 (webhook delivery), §10 (platform security)

> Expose the public REST API (`/v0`): auth via API keys (scoped read/write), per-workspace token-bucket rate limits (Redis) with standard headers, resources: contacts (CRUD/identify), conversations (list/get/create/reply), articles (read), events (track). OpenAPI spec published; generate `packages/sdk-ts` from it in CI.
>
> Webhooks: `webhook_subscriptions` (topics: conversation.created, conversation.part.created, contact.created/updated), deliveries fed from outbox via the `webhooks` queue — HMAC signature + timestamp header, 10 s timeout, exponential backoff + jitter to 72 h, per-endpoint circuit breaker, auto-disable after sustained failure with notification, 30-day partitioned `webhook_deliveries` log + redelivery endpoint.
>
> **Acceptance:** contract tests from the OpenAPI spec pass; rate limit returns 429 + Retry-After; a hanging consumer (test server that sleeps) trips the breaker without delaying other tenants' deliveries; signature verification snippet in docs verified by test.

### P0.12 — Phase 0 gate: hardening, observability, load & chaos
**Depends on:** all above · **Read first:** RFC-001 §9 (full table), §13; RFC-000 §5 Phase 0 exit criteria

> Ship the production-readiness layer and run the gate:
>
> - Observability: OTel tracing (request → outbox → worker correlation), Prometheus metrics + Grafana dashboards for the four golden signals per runtime shape, Sentry, structured JSON logs with workspace/request IDs, queue-depth + oldest-message-age alerts, deploy markers.
> - Terraform for staging+prod (ECS Fargate per RFC-001 topology, RDS Postgres single-AZ→multi-AZ toggle, ElastiCache ×2, S3/CloudFront, Secrets Manager); canary deploy (5% task weight, 15 min) + auto-rollback on SLO burn; Unleash server.
> - Load test (k6): message path at 2× phase-0 target (define target = 10 msg/s, so 20 msg/s) and inbox reads; connection storm test on Centrifugo (staged 20k reconnects).
> - Chaos drills from RFC-001 §9: kill Redis broker (outbox buffers, drains on recovery — prove zero loss), kill a gateway node, Postgres failover (idempotency absorbs retries), full restore-from-backup rehearsal with row-count checksums.
> - Security pass: RLS audit script over information_schema (every tenant table has forced policy), dependency audit, secret scan, PII log scrub verification.
>
> **Acceptance:** RFC-000 Phase 0 exit criteria all check; runbook per alert exists; a "game day" doc records the chaos results and fixes.
