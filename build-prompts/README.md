# Relay — Build Prompts

Ordered, copy-paste prompts that drive a coding agent (Claude Code or similar) through the Relay build, phase by phase. Each prompt is one milestone sized for a focused agent session. The RFCs in `../rfcs/` are the source of truth; the prompts point the agent at the exact sections to read before coding.

| File | Phase | Outcome |
|---|---|---|
| `phase-0-foundation.md` | Months 0–3 | Core loop: widget → inbox → reply; email; help center; billing; API |
| `phase-1-ai-automation.md` | Months 4–6 | Sellable MVP: Aide (AI agent), workflows, SLAs, outbound v1 |
| `phase-2-omnichannel-platform.md` | Months 7–12 | WhatsApp/Meta/SMS, tickets, Series, Copilot, Actions, apps, SSO |
| `phase-3-scale-enterprise.md` | Months 13–18 | Voice, procedures/evals, EU cell, conditional scale graduations |
| `team-split.md` | Phases 1–3 | Two-developer parallel assignment: tracks, sync points, interface contracts |

## How to use

1. Run prompts **in order** within a phase; respect the `Depends on` line. Prompts marked ∥ can run in parallel sessions/worktrees.
2. Start every session by letting the agent read `CLAUDE.md` (created by P0.0) plus the `Read first` RFC sections listed on the prompt.
3. Paste the prompt block verbatim, then add anything you've learned since ("we renamed X", review feedback, etc.).
4. Don't accept a milestone until its **Acceptance** list passes. Each phase ends with a gate prompt — run it before starting the next phase.
5. Commit per milestone; keep PRs reviewable. If the agent proposes deviating from an RFC decision, require it to state the RFC section it's overriding and why — then update the RFC in the same PR (docs and code never drift).

## Master rules (baked into CLAUDE.md by P0.0 — every prompt assumes them)

- **Tenancy is sacred:** every tenant table carries `workspace_id`; every query runs under `SET LOCAL app.ws`; RLS enabled + forced (RFC-002 §7). Any new table ships with its RLS policy and a cross-tenant test.
- **Consistency spine:** state changes with downstream effects write `outbox` rows in the same transaction (RFC-001 §6.5). No direct enqueue from request handlers for must-not-lose effects.
- **Idempotency everywhere:** all Celery tasks safely re-runnable; mutating public endpoints accept idempotency keys.
- **Migrations:** Alembic, expand/contract only, `lock_timeout` wrapper, `CREATE INDEX CONCURRENTLY`, batched backfills (RFC-002 §9). Never bundle destructive DDL with dependent code.
- **Async discipline:** no blocking calls in `async def` paths; external calls always have timeouts; retries = bounded + jittered + idempotent-only.
- **Definition of Done:** ruff + mypy + eslint + tsc clean; unit + integration tests green (testcontainers Postgres/Redis); new endpoints in the OpenAPI spec; feature-flagged if risky; no secrets in code.
