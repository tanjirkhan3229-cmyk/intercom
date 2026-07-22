# Phase 2 — Omnichannel, Outbound Depth & Platform (months 7–12)

Goal: parity on breadth — WhatsApp/Meta/SMS channels, tickets, Series, Copilot, Aide Actions, custom reports, app framework, enterprise base. Exit criteria: RFC-000 §5 Phase 2. Largest phase; the ∥ tracks are designed for parallel teams.

---

### P2.1 — Channel framework + WhatsApp/Messenger/Instagram
**Depends on:** P1 gate · **Read first:** RFC-001 §6.6 (envelope principle), §9 (Meta row); RFC-002 §5.6 (channel_accounts)

> Formalize the adapter contract, then ship the Meta family:
>
> - `ChannelMessage` envelope (normalized inbound/outbound shape: author, body blocks, attachments, channel refs, delivery receipts) + adapter interface (`inbound(webhook) → envelope`, `outbound(part) → provider call`, `capabilities()`); email (P0.7) refactored onto it. Channel quirks die at the adapter — core stays channel-agnostic (assert: no channel conditionals in `messaging`).
> - WhatsApp (Cloud API via BSP), Facebook Messenger, Instagram DM adapters: onboarding flows (embedded signup where supported), per-tenant tokens in Secrets Manager, webhook signature verification, media handling via S3, template-message support for WhatsApp's 24-h window rule (session vs template logic surfaced in composer), per-app rate budgets with token buckets, delivery/read receipts mapped to part states.
> - Per-workspace **channel status page** (connected/erroring/token-expiring) + re-auth flows.
>
> **Acceptance:** live sandbox round-trip per channel; 24-h window rule enforced in composer with clear UX (blocked send offers template picker); token expiry surfaces status + notification, not silent failure; adapter conformance test suite passes for all four adapters (email included).

### P2.2 — SMS + Aide on email & WhatsApp
**Depends on:** P2.1 · **Read first:** RFC-003 §2 (channel goals); RFC-001 §6.6

> - SMS adapter (Twilio): number provisioning UI, inbound/outbound, opt-out keywords (STOP/START) auto-handled into consents, segment-length cost preview in composer.
> - Aide channel expansion: email (async turn model — full-message replies, subject preservation, no streaming; signature/quote stripping on inbound before retrieval) and WhatsApp (session-window-aware: Aide replies only in-session; template fallback offers handoff). Channel-aware formatting layer in the generate step (chat: short + markdown; email: structured paragraphs + greeting/sign-off per persona).
> - Eligibility/preflight updated per channel; metering unchanged (resolution definition is channel-agnostic).
>
> **Acceptance:** Aide resolves a staged email thread end-to-end with correct threading; WhatsApp out-of-window turn routes to handoff/template path; SMS opt-out instantly blocks sends (consent test); per-channel resolution analytics split correctly.

### P2.3 — Tickets ∥
**Depends on:** P1 gate · **Read first:** RFC-000 §2.3; RFC-002 §5.2 (tickets 1:1 conversations), §5.6

> Build `tickets` atop conversations per RFC-002 (one thread model, one reporting spine):
>
> - `ticket_types` with typed attribute schemas (reuse `attribute_definitions` machinery, required/optional, conditional visibility), kinds: customer, back-office, tracker.
> - `tickets` 1:1 with conversations (extension row): custom states (mapped to open/waiting/resolved semantics for SLA/reporting), forms (public + in-widget submission), assignment/teams inherited from conversation core.
> - **Tracker tickets:** one ticket linked to many conversations; replies broadcast-optional to linked conversations; link/unlink UX in inbox side panel.
> - Widget ticket portal: my-tickets list, status timeline, form submission; email notifications on state change.
> - Inbox integration: ticket panel, convert-conversation-to-ticket, filtered ticket views; ticket SLAs + reporting datasets.
>
> **Acceptance:** tracker with 50 linked conversations broadcasts a resolution update exactly once each (idempotent fan-out); required form field enforcement API+UI; ticket state custom labels round-trip to reporting correctly; converting a conversation preserves the full part history.

### P2.4 — Series (visual campaign orchestration) ∥
**Depends on:** P1.5, P1.8 · **Read first:** RFC-000 §2.6 (Series); RFC-001 §6.7; RFC-002 §5.6 (outbound)

> Build Series on the workflow engine's execution substrate (separate graph type, same ledger/timers discipline):
>
> - Nodes: entry rules (segment match, event, date), wait (duration/until/weekday windows), condition branches, A/B split (deterministic hash bucketing), message nodes (email, in-app post/chat, SMS, push), goal + exit rules (goal met, unsubscribed, manual), control-group split (hold-back % with conversion tracking).
> - Delivery windows + frequency capping integrated (deferrals as timers, not drops); per-node stats (entered/sent/converted) on rollups.
> - Contact journey ledger: one active run per contact per series (unique constraint), re-entry rules explicit; simulator mode (dry-run a contact through the graph with reasons logged).
> - Builder UI: extends the React Flow foundation with per-node stat overlays and a journey inspector.
>
> **Acceptance:** 10× send-burst load test per RFC-000 phase-2 exit (fire a 100k-contact series; token buckets shape traffic, zero duplicate sends, stats reconcile); A/B buckets are deterministic and balanced ±1%; control group receives nothing but converts trackably; simulator output matches real execution on fixtures.

### P2.5 — Outbound surfaces: surveys, tours, banners, tooltips, checklists, push ∥
**Depends on:** P1.8, P0.6 · **Read first:** RFC-000 §2.6; RFC-001 §9 (widget bundle row)

> Extend the widget SDK + outbound to the remaining in-product surfaces, keeping the loader ≤ 50 KB gz via lazy-loaded modules per surface:
>
> - **Surveys:** NPS/CSAT/custom with branching logic, in-widget + email delivery, response datasets in reporting.
> - **Product tours:** element-anchored steps (CSS selector + text fallback), draft/preview mode via query param, completion/skip tracking; **tooltips & banners:** targeting + dismissal state; **checklists:** task lists with event-based auto-completion linked to tracked events.
> - All surfaces: audience predicates, trigger rules (page URL match, time-on-page, event), frequency caps, goal tracking; stats per surface.
> - Mobile push campaigns: deep links, delivery/open tracking through the P1.10 registry.
>
> **Acceptance:** tour anchored to a re-rendered SPA element survives DOM churn (retry/fallback logic tested); survey branching produces correct dataset rows; widget bundle budget still enforced (CI) with two surfaces lazy-loaded; a dismissed banner stays dismissed across sessions.

### P2.6 — Copilot for agents ∥
**Depends on:** P1.2 · **Read first:** RFC-003 §7; RFC-001 §6.4 (`ai.interactive`)

> Build Copilot in the inbox composer, reusing the P1.2 machinery (same retrieval, same `agent_runs` ledger, `surface='copilot'`):
>
> - Draft suggestion (grounded in knowledge + conversation context, citations shown to the agent, one-click insert-then-edit — **never auto-send**), summarize conversation (side panel + on-handoff), tone rewrite (friendlier/more formal/shorter), article-from-resolution (drafts a knowledge article from a resolved conversation into the editor as draft).
> - Per-seat rate limits; per-workspace enable; latency budget: draft < 4 s p95 (non-streamed acceptable, show skeleton).
> - Usage analytics: suggestion acceptance rate by agent/team (edit-distance-bucketed) — feeds the phase-3 quality loop.
>
> **Acceptance:** drafts cite sources visible on hover; acceptance-rate metric populates; Copilot failure (provider down) degrades to hidden UI, never blocks the composer; ledger rows distinguish copilot from autonomous turns for metering (copilot is never billed as resolution).

### P2.7 — Aide Actions + custom answers
**Depends on:** P1.2 · **Read first:** RFC-003 §5 (actions), §6; RFC-001 §10 (SSRF proxy)

> - **Actions:** admin-defined HTTP tools (name, description-for-model, method/URL template, auth ref from Secrets vault, JSON schema for inputs/outputs, PII flags); execution only through the SSRF-guard proxy (deny RFC-1918, resolve-then-connect pinning, 10 s timeout), per-action rate limits + idempotency keys on mutating verbs; dry-run test console with sample inputs; responses schema-validated and injected as typed data (untrusted-input posture per RFC-003 §6). Tool-loop integration in the generate→act cycle with max-2-actions-per-turn budget.
> - **Custom answers:** curated intent→response pairs matched by embedding similarity with confirmation margin (RFC-003 §4); short-circuit retrieval; analytics on match rates; suggested-from-gaps flow stub (full clustering lands P3.2).
> - Red-team suite extended: action-exfiltration attempts, URL-template injection, schema-violation responses.
>
> **Acceptance:** an action against a staging order-API resolves an order-status conversation e2e; RFC-1918 target blocked (test); duplicate action execution prevented by idempotency key under forced retry; extended red-team ≥ 98%; custom answer beats generation when above margin (fixture).

### P2.8 — Custom reports & dashboards ∥
**Depends on:** P0.9 · **Read first:** RFC-000 §2.9; RFC-002 §5.6 (reporting), §2 R9

> - **Dataset registry:** curated, documented datasets (conversations, teammate activity, SLA events, CSAT, Aide runs, campaign stats) as versioned SQL views over rollup/metrics tables — the chart builder never touches raw partitioned tables (enforce by role grants).
> - **Chart builder:** metric + group-by + filter AST (reuse predicate components) → validated query plan → chart (line/bar/table/number, Recharts); drill-down to underlying conversations where the dataset supports it; saved reports + dashboards with layout grid; scheduled email exports (CSV attach) via `housekeeping`.
> - Query guardrails: per-workspace concurrency cap, statement timeout 15 s, result cap with pagination.
>
> **Acceptance:** builder cannot produce a query plan touching raw `conversation_parts`/`events` (grant test); a 12-month weekly-grouped report on the largest staging tenant returns < 3 s (R9 budget); scheduled export lands with correct filters; dashboards survive dataset version bump (pinning).

### P2.9 — App framework + directory ∥
**Depends on:** P0.11 · **Read first:** RFC-000 §2.10; RFC-001 §10 (platform security)

> - **OAuth apps:** authorization-code flow with granular scopes, per-install tokens, revocation, developer settings pages; publish public API `/v1` (supersets v0; versioning policy doc).
> - **UI extension points** via declarative card schema (Canvas-Kit-style, original schema): inbox side-panel cards and messenger apps — apps return JSON component trees (text, fields, buttons, inputs) rendered by our sandboxed renderer; interactions POST back to the app's endpoint with signed context; strict allowlist of component types, no arbitrary HTML/JS.
> - **Directory:** listing pages, install flows with scope consent, per-workspace app management/audit; ship 6–10 first-party integrations on the framework (Salesforce/HubSpot contact sync cards, Jira issue card + create action, Stripe customer card, Slack from P1.9 migrated, GitHub issue link).
> - Developer docs site with a runnable sample app.
>
> **Acceptance:** external-style sample app (built only from docs) installs, renders an inbox card, performs a signed interaction round-trip; scope violations rejected; card renderer fuzz test (malformed trees) never breaks the inbox; ≥3 pilot external developers building (per RFC-000 gate).

### P2.10 — Enterprise base: SSO, permissions, audit
**Depends on:** P0.1 · **Read first:** RFC-000 §2.10, §8 (buy-vs-build); RFC-001 §10

> - **SAML/OIDC SSO** (decide buy-vs-build per RFC-000 §8 — default WorkOS unless cost analysis says otherwise; abstract behind our session issuance either way), SCIM-lite user provisioning (create/deactivate), enforced-SSO workspace mode, 2FA (TOTP) for password accounts.
> - **Granular permissions:** permission matrix (resources × actions) layered on the P0.1 role system; custom roles; per-team scoping (agent sees only their teams' inboxes); export/billing/settings gates. Single choke-point enforcement retained — extend the P0.1 helper, no scattered checks.
> - **Audit log:** append-only per RFC-002 §5.6 for security-relevant events (auth, permission changes, exports, API key ops, Aide setting changes, app installs) + filterable UI + CSV export; retention per plan.
>
> **Acceptance:** SSO e2e against a real IdP sandbox (Okta dev); deactivation via SCIM kills sessions ≤ 60 s; permission matrix property tests (no action reachable without its permission — generate cases from the matrix); audit rows immutable (no UPDATE/DELETE grants — verified).

### P2.11 — Phase 2 gate
**Depends on:** all above · **Read first:** RFC-000 §5 Phase 2 exit criteria

> Run the gate: ≥5 channels GA with adapter conformance green; Aide ≥50% resolution on eligible traffic (2-week window, per-channel breakdown); Series 10× burst test signed off; app framework external-dev evidence; SSO/permissions pen-test pass (external tester); load re-test at phase-2 targets (60 msg/s, 200k connections staged); RFC diffs merged. Write `docs/gates/phase-2.md`.
