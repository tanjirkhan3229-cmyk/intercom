# Phase 1 — Sellable MVP: AI Agent + Automation (months 4–6)

Goal: Neko (the AI agent) resolving real conversations with metered billing, a workflow engine, SLAs/views, and outbound v1. Exit criteria: RFC-000 §5 Phase 1 (incl. Neko ≥35% resolution on eligible design-partner traffic).

---

### P1.1 — Knowledge Hub + retrieval pipeline
**Depends on:** P0.8 · **Read first:** RFC-002 §5.5 + Appendix B; RFC-003 §3 (ingestion), §4

> Extend `knowledge` into the Knowledge Hub and build the retrieval substrate:
>
> - Sources: `external_sources` (kinds: url, pdf, snippet) with sync jobs — URL crawler (sitemap-aware, boilerplate-stripped via readability extraction, re-sync diffing), PDF ingestion (text extraction, OCR fallback), snippets CRUD. Per-source AI-readiness status (synced/ingesting/error) surfaced in UI.
> - Chunking + embedding pipeline per RFC-003 §4: heading-aware semantic chunks 400–800 tokens, 10–15% overlap; batch embed via provider abstraction; write `content_chunks` exactly per RFC-002 §5.5 DDL (halfvec 1536, `emb_version`, HNSW + FTS + audience metadata). Articles re-chunk on publish via outbox (freshness ≤ minutes). Re-embed migration path: dual-version write, atomic per-workspace cutover, old-version cleanup.
> - Retrieval service: the hybrid RRF query from RFC-002 Appendix B wrapped as `retrieve(workspace_id, query, locale, k)` with filters, `ef_search` tunable per call, all under the RLS session regime.
> - **Retrieval eval harness (build this now, not later):** labeled corpora (seed with 3 synthetic workspaces of ≥200 docs), recall@k + MRR metrics, run in CI as a regression gate; store runs in `retrieval_evals`.
>
> **Acceptance:** URL re-sync only re-embeds changed chunks (test with a diff); recall@10 ≥ 0.85 on the synthetic corpora; hybrid beats vector-only and FTS-only on the harness (documented numbers); cross-tenant retrieval impossible (RLS test with adversarial embeddings).

### P1.2 — Neko orchestrator v1
**Depends on:** P1.1 · **Read first:** RFC-003 §3 (turn diagram), §5, §6, §9; RFC-001 §6.4 (`ai.interactive` queue), §9 (LLM rows)

> Build the `ai` module's turn pipeline exactly to the RFC-003 §3 state machine, running on the `ai.interactive` queue:
>
> - **Provider abstraction first:** two providers behind one interface (streaming, tool calls, token accounting); per-provider circuit breakers, rate-limit pools, timeout budgets, failover order; model tiering config (cheap tier: preflight/verify; frontier tier: generation).
> - Pipeline: preflight (language, eligibility vs workspace scope, safety class — cheap model, ≤400 ms) → query rewrite → retrieve (P1.1) → grounding gate (tunable threshold; insufficient ⇒ one clarifying question max, then handoff) → generate (streamed via outbox→Redis→gateway to the widget; citations to chunk ids required) → verify (groundedness + policy filters, cheap model) → emit part or handoff.
> - Handoff: honors "talk to a person" instantly; posts private summary note (recap, sources tried, sentiment); flips `conversations.ai_status`.
> - `agent_runs` ledger per RFC-003 §3: chunks+scores, prompt hash, models, token counts, cost, latency breakdown, outcome — every turn, no exceptions.
> - Prompt-injection posture per RFC-003 §6: retrieved/customer content typed as data, delimited; red-team suite (injection corpus, cross-tenant probes, exfiltration attempts) as a CI job with a pass-rate gate.
> - Kill switches: per-workspace Neko flag, global model-route flag.
>
> **Acceptance:** first streamed token p95 < 3 s on staging with warm cache; provider blackhole test fails over mid-conversation without user-visible error; verifier rejects a planted ungrounded claim (fixture); red-team suite ≥ 98% pass; every turn reproducible from `agent_runs` (replay tool included).

### P1.3 — Neko product surface + resolution metering
**Depends on:** P1.2, P0.10 · **Read first:** RFC-003 §8 (resolution definition — implement verbatim), §9 (spend caps); RFC-000 §8 (pricing)

> Ship Neko's workspace-facing controls and the money loop:
>
> - Settings: enable per channel (chat only this phase), persona/tone (friendly/neutral/formal + custom guidance text), answer length, grounding-gate conservatism slider, handoff rules (always-handoff intents list, office-hours behavior), scope (which sources/collections Neko may use).
> - **Preview sandbox:** test conversations against current knowledge with retrieval trace visible (chunks + scores) — admins must be able to see *why* an answer happened.
> - Resolution metering per RFC-003 §8 verbatim: participation + no-human-after + (confirm OR 72 h silence) + no-reopen-within-72 h ⇒ `usage_records` row (same-txn with the qualifying state change); reopen claw-back as negative row; Stripe metered sync + monthly reconciliation job; per-workspace monthly spend cap → past cap, Neko routes to humans (never silent drop) and notifies admins.
>
> **Acceptance:** metering fixture suite covers confirm/silence/reopen/claw-back paths to the exact definition; double webhook = no double meter; cap breach flips routing within one turn; sandbox trace matches `agent_runs`.

### P1.4 — Neko analytics v0 ∥
**Depends on:** P1.3 · **Read first:** RFC-003 §8 (analytics), RFC-002 §5.6 (reporting spine)

> Analytics pages fed from `agent_runs` + `conversation_metrics`: resolution rate & deflection over time, handoff reasons breakdown, CSAT delta (Neko-touched vs not), latency + cost per conversation, and a **run inspector** (searchable list → full turn trace: retrieval set, decisions, outputs). Rollup tables per the reporting spine — no raw `agent_runs` scans in dashboards.
>
> **Acceptance:** numbers reconcile with the metering fixtures from P1.3; run inspector loads any production turn < 1 s; a support engineer can answer "why did Neko say X" without engineering help (usability check).

### P1.5 — Workflow engine
**Depends on:** P0.3 · **Read first:** RFC-001 §6.7 (engine semantics); RFC-002 §5.6 (workflows, timers DDL)

> Build the `automation` module's execution core (no UI yet):
>
> - Model: `workflows` / `workflow_versions` (graph JSONB: nodes typed trigger/condition/action/bot-step/wait; zod-style server validation), `workflow_runs`, `workflow_run_steps` with **UNIQUE (run_id, step_id)** — the exactly-once-effects ledger. Runs pin a version; editing never mutates in-flight runs.
> - Triggers via outbox consumers: conversation.created, contact.message.created, attribute/event changed, admin action, schedule (beat), webhook-in. Trigger filters compiled from the same predicate AST as segments.
> - Executor: advances a run step-by-step; each side effect (assign, tag, set attribute, snooze, apply SLA*, send reply/macro, call webhook, hand to Neko, route to team) executes through the ledger — a replayed task sees the ledger row and skips. Bot steps: ask-with-buttons, collect data (typed into attributes), disambiguate — rendered natively in the widget via part metadata. (*SLA action lands with P1.7; register the action type now behind a flag.)
> - Waits: durable `timers` rows claimed with `FOR UPDATE SKIP LOCKED` by beat per RFC-002 W6; survives broker wipe (chaos test).
>
> **Acceptance:** chaos suite — kill workers mid-run, duplicate trigger delivery, broker flush — yields zero duplicate side effects and all runs complete or park with resumable state; 1k concurrent runs on staging without lock contention (measure); execution log API returns full step history.

### P1.6 — Workflow builder UI ∥ (with P1.5 API stubs)
**Depends on:** P1.5 · **Read first:** RFC-000 §2.7

> Visual builder in the agent app (React Flow): node palette (triggers/conditions/branches/bot steps/actions/wait), drag-connect with type-checked edges, inline node config panels reusing the attribute/segment predicate components, validation surfacing broken references before publish, version list with draft→publish flow and "runs on old versions" indicator, per-workflow run log view (step timeline, errors, re-run from failed step for idempotent steps).
>
> **Acceptance:** Playwright: build "new conversation outside office hours → collect email → hand to Neko → if unresolved route to Team X" entirely in UI, publish, execute e2e; invalid graph (orphan node, missing config) cannot publish; editing a live workflow leaves in-flight runs on the pinned version (verified).

### P1.7 — Inbox v2: SLAs, views, balanced assignment, collision
**Depends on:** P0.5 · **Read first:** RFC-000 §2.2, §5 Phase 1; RFC-002 §5.6 (sla tables), §2 R1/R4

> - **Office-hours schedules** (workspace + per-team, timezone-aware, holidays) powering expected-reply-time in the widget and business-hours SLA math.
> - **SLA policies:** first-response / next-response / resolution targets, business-hours-aware timers as durable `timers` rows; breach ⇒ outbox event → escalation actions (highlight, reassign, notify) + `sla_events` for reporting. Applied manually, by workflow action, or by rule on conversation attributes.
> - **Custom views:** saved filter ASTs (same predicate engine) with live counts (cached per R4), shareable per team; sidebar management UI.
> - **Balanced assignment:** load-aware round-robin (open-conversation count weighting, Redis-tracked, DB-reconciled) with per-agent capacity and away toggle.
> - **Collision detection:** who's-viewing/typing via presence channels; soft lock warning in composer.
> - CSAT report page joins ratings to teams/agents.
>
> **Acceptance:** SLA timer fires business-hours-correct across a weekend fixture (property tests on the schedule math); view counts match query truth within 10 s; balanced assignment distributes a 100-conversation burst within ±1 of ideal in test; two agents replying simultaneously both see the collision warning.

### P1.8 — Outbound v1: subscription types, posts/chats, email broadcasts
**Depends on:** P0.7, P0.2 · **Read first:** RFC-000 §2.6; RFC-001 §6.7 (campaign fire); RFC-002 §5.6 (outbound tables)

> Build `outbound` v1:
>
> - `subscription_types` + `consents` (per contact × type, audit-trailed, unsubscribe pages + one-click List-Unsubscribe headers).
> - In-app **posts & chats:** compose (reuse block editor), audience = segment/predicate snapshot, delivery via gateway to matching active widget sessions + catch-up on next boot; seen/click tracking as `message_events`.
> - **Email broadcasts:** MJML-based template editor with variables, test-send, audience snapshot at fire per RFC-001 §6.7 (chunked enqueue 1k/task, per-tenant + global token buckets, `sends` UNIQUE (campaign_id, contact_id), suppression + consent checks at send time), SES event webhooks → `message_events` → `campaign_stats` rollups; per-campaign report (sent/delivered/open/click/bounce/unsub).
> - Frequency capping v0: per-contact daily/weekly caps enforced at send.
>
> **Acceptance:** re-firing a campaign sends zero duplicates (unique ledger proven under concurrent workers); unsubscribed contact excluded at send-time even if snapshotted before; 100k-recipient staging send sustains ≥200/s within rate budgets and stats reconcile with SES event counts ±0.5%; consent change mid-send respected.

### P1.9 — Segments, imports, first integrations ∥
**Depends on:** P0.2 · **Read first:** RFC-002 §5.4 (segments, rollups); RFC-000 §5 Phase 1

> - **Segments:** predicate AST (attributes, company fields, event counts/windows via `event_rollups`) compiled to SQL; live preview count; materialized membership refreshed incrementally by outbox consumers (attribute/event changes) + nightly full reconcile.
> - **CSV import:** streaming parser, column mapping UI, validation report, dedupe/merge rules (external_id > email precedence), 1M-row target via COPY batches with progress; export symmetric.
> - **Integrations:** Slack (conversation notifications → channel, reply-from-Slack v0), Zapier app (triggers: conversation created, contact created; actions: create contact, send message), generic outbound webhook recipes doc.
>
> **Acceptance:** 1M-row import completes < 15 min on staging with zero duplicate contacts (idempotent re-run proven); segment membership converges after attribute flips (delta path, not just nightly); Slack round-trip e2e.

### P1.10 — Mobile SDKs beta ∥ (separate track)
**Depends on:** P0.6 APIs · **Read first:** RFC-000 §2.1

> iOS (Swift) + Android (Kotlin) SDKs: boot/identify (same HMAC scheme), conversation list + thread UI (native), push notifications (APNs/FCM: registration, deep-link to conversation, reply from notification on Android), attachment upload, theming hooks. Publish beta via SPM/Maven with a sample app each. Backend: device-token registry + push fan-out worker on the `send.channels` queue.
>
> **Acceptance:** sample apps round-trip messages with push received in background state; token rotation handled; SDK size budgets (iOS ≤ 3 MB, Android ≤ 2.5 MB).

### P1.11 — Phase 1 gate
**Depends on:** all above · **Read first:** RFC-000 §5 Phase 1 exit criteria; RFC-003 §8

> Run the gate: (1) Neko shadow-mode then live on ≥10 design partners; measure resolution per the RFC-003 §8 definition over ≥2 weeks — require ≥35% on eligible traffic, with the analytics dashboard as evidence; (2) workflow chaos suite green in CI for 2 consecutive weeks; (3) billing e2e on real Stripe test clock incl. metered resolutions; (4) load re-test message path at phase-1 target (30 msg/s) + campaign burst; (5) update all four RFCs where reality diverged (list the diffs in the gate PR).
>
> **Acceptance:** a written gate report in `docs/gates/phase-1.md` with metrics, incidents, and go/no-go.
