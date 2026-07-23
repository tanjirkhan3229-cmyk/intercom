# Team Split — Phases 1–3, Two Developers in Parallel

Phase 0 is done. This divides the remaining 29 prompts between two developers so both can run continuously with minimal blocking. The split is **by domain, not by stack** (each dev owns backend+frontend of their features): with two people, a frontend/backend split would make every feature a handoff — domain ownership means each of you ships end-to-end and reviews the other.

Pick who is who and note it here:

- **Dev A — "AI & Channels" track:** owns modules `ai`, `knowledge`, `channels` → _________
- **Dev B — "Platform & Product" track:** owns modules `automation`, `outbound`, `crm`, `tickets`, `reporting`, `platform`, `identity`, `billing` → _________
- **Shared (either may touch, other must review):** `messaging` core, `core/`, `apps/widget` shell, infra/CI.

Rationale: Dev A's prompts chain through the Neko/retrieval/channel stack (deep context compounds); Dev B's chain through the engine/product surfaces (workflow engine → Series → surfaces reuse each other). Cross-track dependencies reduce to three explicit interface contracts (§4).

## 1. Phase 1 (target: months 4–6)

| Order | Dev A — AI & Channels | Dev B — Platform & Product |
|---|---|---|
| 1 | **P1.1** Knowledge Hub + retrieval pipeline | **P1.5** Workflow engine |
| 2 | **P1.2** Neko orchestrator v1 | **P1.6** Workflow builder UI |
| 3 | **P1.3** Neko product surface + metering | **P1.7** Inbox v2 (SLAs, views, balanced, collision) |
| 4 | **P1.4** Neko analytics v0 | **P1.8** Outbound v1 (consents, posts, broadcasts) |
| 5 | **P1.10** Mobile SDKs beta (slack-time item; drop to phase 2 if Neko slips) | **P1.9** Segments, imports, Slack/Zapier |
| 6 | **P1.11 Gate — together** (Neko measurement is A, chaos/load is B) | ← same |

Notes: P1.5's "hand to Neko" action ships behind a flag until sync point S1. P1.7's SLA timers reuse P1.5's `timers` infra — B sequences internally, no cross-block. P1.3 needs nothing from B (billing meters exist from P0.10).

## 2. Phase 2 (target: months 7–12)

| Order | Dev A — AI & Channels | Dev B — Platform & Product |
|---|---|---|
| 1 | **P2.1** Channel framework + WhatsApp/FB/IG (start BSP/Meta applications on day 1 — approval lead time is the risk) | **P2.3** Tickets |
| 2 | **P2.2** SMS + Neko on email/WhatsApp | **P2.4** Series builder |
| 3 | **P2.7** Neko Actions + custom answers | **P2.5** Outbound surfaces (surveys, tours, banners, checklists, push) |
| 4 | **P2.6** Copilot | **P2.9** App framework + directory |
| 5 | **P2.8** Custom reports & dashboards (decoupled breather item) | **P2.10** Enterprise base (SSO, permissions, audit) |
| 6 | **P2.11 Gate — together** | ← same |

Notes: P2.5's widget-side SDK additions touch shared `apps/widget` — B builds, A reviews (A owns the widget messaging paths). P2.7's SSRF proxy is shared infra — agree its interface at S3 before either consumes it (Actions now, app framework callbacks later). P2.8 sits with A deliberately: it only depends on P0.9 and balances A's queue while Meta approvals are pending.

## 3. Phase 3 (target: months 13–18)

| Order | Dev A — AI & Channels | Dev B — Platform & Product |
|---|---|---|
| 1 | **P3.1** Voice channel pilot (write RFC-004 first — pair on the stack decision) | **P3.3** EU data-residency cell (write RFC-005 first — pair on the cell model) |
| 2 | **P3.2** Neko v3: procedures, guidance, evals, content gaps | **P3.4** Warehouse export + multibrand |
| 3 | **P3.6** Reporting v3 topics + forecasting (SOC 2 evidence portion is B's) | **P3.5** Conditional scale graduations (only if RFC triggers fired) |
| 4 | **P3.7 GA gate — together** | ← same |

## 4. Interface contracts (agree these before the dependent work starts)

| # | Contract | Provider → Consumer | Freeze by |
|---|---|---|---|
| C1 | `hand_to_neko(conversation_id, context)` service interface + Neko handoff event (`ai_status` transitions, summary-note shape) | A → B (workflow action, routing rules) | S1 |
| C2 | Predicate AST + audience components (segments/views/targeting share one engine) | B → A (knowledge audience scoping, Neko eligibility rules) | End of P1.5 |
| C3 | SSRF-guard proxy API (egress policy, timeouts, idempotency headers) | A builds in P2.7 → B reuses in P2.9 | S3 |

Everything else crosses tracks only via the outbox topics already defined in the RFCs — additive changes are safe, renames need the other's sign-off.

## 5. Sync points & working agreements

- **S1 (≈ week 3 of phase 1):** P1.2 + P1.5 both landed → wire C1, unflag hand-to-Neko, e2e test together.
- **S2:** P1.11 gate week — run it as a pair (A: resolution measurement; B: chaos/load/billing).
- **S3 (start of P2.7):** C3 design review, 1 hour.
- **S4:** P2.11 gate week — pair.
- **S5 (phase 3 kickoff):** two pair-design days — RFC-004 (voice) and RFC-005 (EU cell) — decisions here are expensive to get wrong solo.
- **Git:** trunk-based; branches `feat/p<prompt>-<slug>` (e.g. `feat/p1.2-neko-orchestrator`); PRs ≤ ~800 lines reviewed by the other dev within 24 h; rebase, don't merge-commit; **never run two agents' git operations in the working tree simultaneously** (use separate clones/worktrees per dev).
- **CODEOWNERS:** encode the module ownership above so reviews route automatically; shared paths (`apps/api/src/relay/core/`, `apps/api/src/relay/modules/messaging/`, `apps/widget/`) require the non-author's approval.
- **Master rules are non-negotiable in review** (tenancy/RLS, outbox, idempotency, expand/contract migrations — `build-prompts/README.md`). A PR that weakens one is rejected regardless of deadline.
- **Weekly:** 30-min Monday planning (what lands this week, any contract drift), gate weeks together. If a track stalls on an external dependency (Meta approval, IdP sandbox), pull the next decoupled item from your own column — don't cross tracks without a sync.
