# Phase-0 exit criteria — evidence checklist

Maps each RFC-000 §5 Phase-0 exit criterion to the concrete evidence in this repo, current status, and residual gap. This is the Phase-0 gate acceptance record.

> RFC-000 §5, Phase 0 exit criteria (verbatim): *"10 design partners live; message p95 persist→inbox-render < 1s; zero cross-tenant leakage under RLS test suite; SOC 2 controls started."*

Status legend: ✅ met · 🟡 in place, needs a live run/fill · ⬜ process/organizational (not a code artifact).

| # | Exit criterion | Evidence in repo | Status | Residual gap |
|---|---|---|---|---|
| 1 | **10 design partners live** | Organizational milestone — tracked via workspace provisioning (each partner = a live workspace, RFC-000 §4 envelope) + billing/seat metering (Stripe, RFC-000 §2). No single code marker; verified by counting live workspaces + partner sign-off list. | ⬜ | Not a code deliverable. Confirm 10 workspaces live + partner sign-off before gate; consider a `design_partner` workspace marker/report for auditability. |
| 2 | **message p95 persist → inbox-render < 1s** | k6 message path @ 20 msg/s (`docs/gameday-phase0.md` → Load results, run 2026-07-23): **send persist+ack p95 = 14.7 ms (<250 ms), inbox p95 = 14.38 ms (<300 ms), 0% error** — the interactive-path SLOs (RFC-001 §3) pass with wide headroom. Server-side lag observable via `relay_outbox_oldest_age_seconds`; realtime path = outbox → relay (`relay:outbox` stream) → Redis pub/sub → Centrifugo. | 🟡 | Interactive-path SLOs met locally. The **end-to-end persist→inbox-render round-trip** and the full 20k connection storm still need a **staging (prod-shape) run** to confirm the <1 s figure under realistic fan-out + scale. |
| 3 | **zero cross-tenant leakage under the RLS test suite** | `apps/api/tests/integration/test_tenancy_rls.py` — proves zero leakage across the HTTP surface, that an **unset `app.ws` returns zero rows**, and that RLS (not an app WHERE clause) scopes queries. Backed by `scripts/audit_rls.py` (every tenant table = RLS ENABLED + FORCED + `ws_isolation` policy). Enforced by master rule 1 (`create_tenant_table` + mandatory leakage test per table). Alert/runbook: `runbooks/rls-audit-failure.md`. | ✅ | Keep both green in CI. Any new tenant table must ship its own leakage test + pass `audit_rls.py` (gate enforced). |
| 4 | **SOC 2 controls started** | Security pass artifacts: `scripts/audit_rls.py` (tenant-isolation control), `scripts/scan_secrets.py` (no-secrets-in-code, RFC-001 §13), dependency audit (CI), PII scrub (`apps/api/src/relay/core/observability/scrub.py` — logs + Sentry), least-privilege DB roles (`app_rw`/`app_ro`/`migrator` with BYPASSRLS confined to migrations, CLAUDE.md conventions), and AWS Secrets Manager via Terraform (`infra/terraform/`, secrets never env-baked). Sentry `send_default_pii=False` + `before_send` scrub. | 🟡 | "Started" bar: controls exist in code/CI. Formal SOC 2 program tracking (policies, evidence collection, auditor) is organizational and continues beyond Phase 0. |

## Detail per criterion

### 2 — message p95 persist → inbox-render < 1s
- **SLI:** the fan-out round-trip probe (sender POST → subscriber receive) is the authoritative measurement; `relay_outbox_oldest_age_seconds` is the server-side lag proxy.
- **SLO source:** RFC-001 §3 (fan-out p95 <1s) and RFC-000 §5 (persist→inbox-render <1s).
- **Evidence to fill:** `docs/gameday-phase0.md` → "Message path @ 20 msg/s" table (persist+ack p95 <250 ms; persist→render p95 <1 s). Headroom expectation ≥2× on the interactive path (RFC-001 §2), since 20 msg/s ≪ ~120 msg/s peak envelope.
- **Runbooks:** `slo-burn-message-send.md`, `slo-burn-fanout.md`.

### 3 — zero cross-tenant leakage
- `test_tenancy_rls.py`: signs up two workspaces, asserts one cannot read the other across the HTTP surface; asserts an **unset `app.ws` GUC returns zero rows** from tenant tables; asserts RLS (not app filter) is the enforcement (services carry no explicit `workspace_id` WHERE).
- `audit_rls.py`: keys off the presence of a `workspace_id` column (not a hardcoded list); requires ENABLED + FORCED + a policy; skips partition children (enforced via parent). Exit 1 on any offender.
- Both are gates; RFC-002 §7 + RFC-001 §10 are the design basis; master rule 1 makes them non-negotiable.

### 4 — SOC 2 controls started (mapping)
| Control area | Artifact |
|---|---|
| Logical access / tenant isolation | `scripts/audit_rls.py`, `test_tenancy_rls.py`, RLS enabled+forced (RFC-002 §7) |
| Least-privilege DB access | `app_rw` (RLS-forced runtime), `app_ro` (read-only replicas), `migrator` (BYPASSRLS, CI/migrations only) |
| Secrets management | `scripts/scan_secrets.py` (no secrets in tree), AWS Secrets Manager in `infra/terraform/` |
| Vulnerability management | dependency audit in CI (WS2 security pass) |
| Confidentiality / PII handling | `observability/scrub.py` (log + Sentry redaction), Sentry `send_default_pii=False` |
| Change management | expand/contract migrations, canary + auto-rollback (RFC-001 §13), immutable images |
| Availability / resilience | game-day drills + restore drill (`gameday-phase0.md`), runbooks per alert (`runbooks/`) |

## Gate readiness summary
- **Proven locally (2026-07-23):** criterion 2 interactive-path SLOs (send 14.7 ms / inbox 14.38 ms), all four chaos drills (zero loss / checksum / idempotency), and finding F1 (relay reconnect) fixed — see `gameday-phase0.md`.
- **Still needs a staging run:** criterion 2 end-to-end persist→inbox-render <1 s and the full 20k connection storm (prod-shape scale).
- **Continuously enforced (code/CI):** criterion 3 (RLS suite + audit) and the criterion-4 control artifacts.
- **Organizational:** criterion 1 (10 partners) and the formal SOC 2 program (criterion 4's "program", beyond "controls started").

## Related
- `runbooks/README.md`, `observability.md`, `gameday-phase0.md`.
- RFC-000 §5 (exit criteria), RFC-001 §3/§9/§10/§13, RFC-002 §7. CLAUDE.md master rules 1–6.
