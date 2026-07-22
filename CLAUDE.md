# CLAUDE.md — Relay

Relay is an Intercom-class customer messaging platform: Messenger widget → agent inbox →
reply, plus email, help center, billing, and a public API. This file is the always-loaded
contract for working in this repo. The **RFCs in `rfcs/` are the source of truth**; the
ordered build prompts in `build-prompts/` drive the phased delivery.

- `rfcs/RFC-000` — scope, sizing, roadmap
- `rfcs/RFC-001` — system architecture (topology, runtime shapes, flows)
- `rfcs/RFC-002` — data layer (schema, tenancy/RLS, partitioning) — **authoritative for schema**
- `rfcs/RFC-003` — AI subsystem
- `build-prompts/` — milestone prompts (phase 0 → 3); run in order, respect `Depends on`.

If you deviate from an RFC decision, state the section you're overriding and why, and update
the RFC in the same change — docs and code never drift.

---

## Master rules (non-negotiable — every change assumes them)

1. **Tenancy is sacred.** Every tenant table carries `workspace_id`; every request runs its
   DB work inside a transaction that has `SET LOCAL app.ws = <workspace_id>` (via
   `set_config`, done in session middleware). RLS is **enabled + FORCED** on every tenant
   table (RFC-002 §7). Any new tenant table ships with its RLS policy (use
   `relay.core.rls.create_tenant_table`) **and** a cross-tenant leakage test.
2. **Consistency spine.** State changes with downstream effects (fan-out, webhooks, workflow
   triggers, AI turns, billing meters) write an `outbox` row in the **same transaction** as
   the domain write (RFC-001 §6.5). No direct enqueue from request handlers for
   must-not-lose effects.
3. **Idempotency everywhere.** All Celery tasks are safely re-runnable (natural keys or a
   dedupe ledger — at-least-once delivery). Mutating public endpoints accept an
   `Idempotency-Key` header.
4. **Migrations.** Alembic, **expand/contract only**; the `lock_timeout='2s'` /
   `statement_timeout='30s'` wrapper is applied automatically (`migrations/env.py`);
   `CREATE INDEX CONCURRENTLY` on large tables (enforced by `scripts/check_migrations.py`);
   batched, resumable backfills as `housekeeping` tasks. A migration and the code depending
   on it never ship in the same deploy.
5. **Async discipline.** No blocking calls in `async def` paths; every external call has a
   timeout; retries are bounded + jittered + idempotent-only; circuit breakers on providers.
6. **Definition of Done.** `ruff` + `mypy` + `eslint` + `tsc` clean; unit + integration tests
   green (testcontainers Postgres/Redis); new endpoints in the OpenAPI spec; risky work
   behind a feature flag; no secrets in code (AWS Secrets Manager / env, never baked).

---

## Architecture in one screen (RFC-001 §6.1)

A **modular monolith**: one FastAPI codebase, strict internal boundaries, deployed as four
runtime shapes:

| Shape | What | Where |
|---|---|---|
| `app` | All HTTP (public API, agent BFF, widget API, channel webhooks) | `relay.main:app` |
| `workers` | Everything async, on segregated Celery queues (bulkheads) | `relay.worker:celery_app` |
| `beat` | Scheduler + durable timers | `relay.worker:celery_app` (beat) |
| `gateway` | 500k websockets, pub/sub fan-out — **Centrifugo**, separate | P0.4 |

Realtime is bought (Centrifugo), not built. Postgres is the single source of truth; Redis is
cache/coordination only; S3 holds blobs.

### Module map (RFC-001 §6.2) — enforced boundaries
`identity · crm · messaging · channels · tickets · knowledge · ai · automation · outbound ·
reporting · platform · billing`

Each module lives at `apps/api/src/relay/modules/<name>/` and exposes:
`router.py` (HTTP) · `service.py` (the cross-module interface) · `models.py` (its tables) ·
`events.py` (domain events on the outbox).

**Boundary rule:** a module may import another module's `service` or `events`, and may always
import `relay.core`. It may **never** import another module's `models`/`router`/internals.
Enforced by `apps/api/.importlinter` (import-linter, run in CI). To see it bite: add
`from relay.modules.crm import models` inside `relay/modules/messaging/service.py` and run
`make lint-api` — the contract breaks.

---

## Repo layout

```
apps/
  api/        FastAPI modular monolith (Python 3.12, uv, SQLAlchemy 2 async, Alembic, Celery)
    src/relay/
      core/       shared kernel: settings, db (RLS GUC), ids (uuid7/base62), logging, errors
      modules/<name>/  router · service · models · events
      main.py     app factory        worker.py  Celery app        cli.py  `relay openapi`
    migrations/   Alembic (lock_timeout wrapper, create_tenant_table helper)
    tests/        unit · integration (testcontainers) · architecture (import/migration lint)
  web/        Next.js 15 (App Router, TS, Tailwind, shadcn/ui) — marketing SSG + agent app CSR
  widget/     Vite + Preact messenger (loader ≤5 KB gz + iframe app, 50 KB gz budget)
packages/
  shared/     design tokens, Tailwind preset, shared domain types (@relay/shared)
  sdk-ts/     generated API client (@relay/sdk-ts), generated from OpenAPI in CI
infra/        docker-compose dev stack; Postgres init (extensions + roles)
scripts/      check_migrations.py (migration lint)
rfcs/  build-prompts/   source of truth + milestone prompts
```

---

## Dev commands

```bash
make dev          # boot the full stack (Postgres+pgvector, Redis x2, MinIO, Mailpit, API, workers) + migrate
make infra        # backing services only
make migrate      # run Alembic migrations (as the migrator role)
make down         # stop      make clean  # stop + wipe volumes      make logs

make lint         # ruff + mypy + import-linter + migration lint + eslint + tsc
make test-api     # pytest unit + integration (testcontainers spin up PG/Redis)

make web          # Next.js dev server (localhost:3000)
make widget       # Preact widget dev server (localhost:5173)
make sdk          # dump OpenAPI + regenerate packages/sdk-ts
```

Dev URLs: API `:8000` (`/docs`, `/healthz`, `/v0/hello`) · MinIO console `:9001` ·
Mailpit `:8025` · Postgres `:5432` · Redis cache `:6379` · Redis broker `:6380`.

Environment: `make dev` copies `.env.example` → `.env` on first run. This box needs Docker;
the API image is Python 3.12 (the host Python version is irrelevant).

---

## Conventions cheat-sheet (RFC-002 §5.1)

- **Keys:** UUIDv7 PKs, app-generated via `relay.core.ids.uuid7` (time-ordered). Public IDs are
  prefixed base62 (`wrk_`, `adm_`, `cnv_`, …) via `encode_public_id` / `decode_public_id`.
- **Tenant tables:** add `WorkspaceScoped` mixin (gives `workspace_id`), create via
  `create_tenant_table(...)` so RLS is enabled + forced automatically, lead every composite
  index with `workspace_id`, and add a cross-tenant test.
- **DB roles:** `app_rw` (runtime, RLS-forced), `app_ro` (replicas, read-only), `migrator`
  (DDL, BYPASSRLS, migrations/CI only). Read-your-writes uses the writer; replica-tolerant
  reads use `app_ro`.
- **Pagination:** keyset only (no OFFSET) on hot paths; `Page<T>` envelope in `@relay/shared`.
- **Errors:** raise `relay.core.errors.*` (mapped to status + stable `code`); handlers render
  `{"error": {code, message, request_id, details}}`.
- **Logging:** `relay.core.logging.get_logger`; every line carries request/workspace ids.

## RBAC
Roles: `owner / admin / agent / restricted` (+ per-team). Permission checks live in the
**service layer through one choke-point helper** (RFC-001 §10) — never scattered in routers.

## Testing
- Unit: `pytest -m "not integration"`. Integration: `pytest -m integration` (testcontainers).
- Cross-tenant suite proves zero leakage **with RLS on AND with the app filter removed**.
- An unset `app.ws` GUC must return zero rows from tenant tables (fixture asserts this).
