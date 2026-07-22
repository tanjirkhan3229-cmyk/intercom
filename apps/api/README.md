# relay-api

The Relay modular monolith (RFC-001 §6.1). One FastAPI codebase deployed as four runtime
shapes: `app` (HTTP), `workers` (Celery, segregated queues), `beat` (scheduler), and the
one-shot `migrate` step. Realtime is a separate gateway (Centrifugo, P0.4).

## Layout
- `src/relay/core/` — shared kernel (settings, db, ids, logging, errors). Any module may
  import it; it must never import a feature module.
- `src/relay/modules/<name>/` — feature modules (`router`, `service`, `models`, `events`).
  Cross-module access is via `service`/`events` only, enforced by `.importlinter`.
- `migrations/` — Alembic (expand/contract, lock_timeout wrapper).

## Common commands (run from repo root)
- `make dev` — boot the whole stack + migrations
- `make test-api` — unit + integration (testcontainers)
- `make lint-api` — ruff + mypy + import-linter + migration lint

See the root `CLAUDE.md` for the master rules.
