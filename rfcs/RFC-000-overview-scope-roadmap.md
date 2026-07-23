# Relay — Product Overview, Scope & Phased Roadmap (RFC-000)

_Status: Draft · Author: Architecture WG · Date: 2026-07-22_
_One-line summary: Scope and delivery plan for "Relay" (working codename), an AI-first customer-service platform with full Intercom-class capability, built on FastAPI + Next.js + PostgreSQL._

This is the index document of a four-part RFC set:

| RFC | Title | Covers |
|---|---|---|
| **RFC-000** | Overview, scope & roadmap (this doc) | Feature inventory, phasing, sizing assumptions, team & budget |
| **RFC-001** | System architecture | Topology, services, realtime pipeline, channels, capacity math, failure modes, CI/CD, security |
| **RFC-002** | Data layer design | Schema, access patterns, tenancy, indexing, partitioning, search/vector, ops |
| **RFC-003** | AI agent & knowledge subsystem | RAG pipeline, agent orchestration, copilot, guardrails, evals, unit economics |

---

## 1. Context & problem

We are building a multi-tenant SaaS customer-service platform with capability parity to the modern Intercom product family: an embeddable Messenger, an omnichannel helpdesk, an autonomous AI support agent, a hosted help center, proactive/outbound messaging, a no-code automation engine, a customer data platform, and reporting — exposed through a public API and app framework.

The 2026 market reality this competes in: Intercom positions itself as an "AI-first customer service platform" whose center of gravity is **Fin**, an AI agent that autonomously resolves a majority of inbound conversations (Intercom reports ≈65–70% average resolution rates), priced per resolution (≈$0.99), on top of a per-seat helpdesk ($39–$139/seat tiers). Any credible clone must treat the AI agent as a core subsystem, not a bolt-on.

**Legal positioning (load-bearing, read once):** feature sets and product categories are not protectable; this project is lawful competition. What we must not do: copy Intercom's source code, UI assets, CSS/design files, documentation text, or marketing copy; use their trademarks ("Intercom", "Fin") in the product or marketing; or scrape/misuse their services in violation of their ToS. Everything in this RFC set is an original, independent design from publicly observable product behavior. (Not legal advice; have counsel review branding and any migration-import tooling.)

## 2. Product surface inventory (parity map)

The full capability surface, grouped by subsystem. This is the master checklist the roadmap phases draw from.

### 2.1 Messenger (web + mobile SDKs)
Embeddable chat widget (JS snippet, versioned CDN bundle); identity for visitors and logged-in users with HMAC identity verification; conversation UI with typing indicators, delivery/read state, attachments, emoji/GIF, message reactions; Messenger "home" with modular spaces (help articles, news, tasks/checklists, tickets); article search & viewer in-widget; conversation ratings (CSAT); office hours & expected-reply-time messaging; multilingual UI; theming (colors, launcher, position, custom launcher); visibility/targeting rules; iOS & Android SDKs with push notifications; unattended/proactive message receipt.

### 2.2 Omnichannel inbox (Helpdesk)
Shared team inboxes; conversation states (open / snoozed / closed) + waiting-on flags; assignment: manual, round-robin, balanced/load-aware; teams & seat roles; private notes & @mentions; macros/saved replies with variables; tags; conversation-level custom attributes; SLAs with business-hours schedules and breach alerts; custom views (saved filters); collision detection (who is viewing/typing); keyboard-first UI; contact side panel (profile, attributes, events, past conversations, app cards); search across conversations; bulk actions; spam & blocking. Channels unified into the same inbox: web/mobile chat, **email** (forwarding address + custom domain sending), **WhatsApp**, **Facebook Messenger**, **Instagram**, **SMS**, **phone/voice** (phase 3), and an **API channel** for headless use.

### 2.3 Tickets
Customer-visible tickets (with forms), back-office tickets, and tracker tickets (one issue linked to many conversations); custom ticket types with typed attributes; ticket states with customizable labels; ticket portal view in Messenger; linking conversations ↔ tickets; ticket SLAs and reporting.

### 2.4 AI agent ("Neko" — our Fin equivalent)
Autonomous agent over chat, email, WhatsApp, and API (voice in phase 3); retrieval-augmented generation over all knowledge sources; custom answers (curated responses for specific questions); **actions** (admin-defined API calls the agent can invoke, e.g. order status); **procedures** (multi-step, rule-bound task flows); tone/persona and answer-length controls; multilingual; handoff rules and escalation to humans; preview/test sandbox; per-resolution metering; analytics (resolution rate, deflection, CSAT impact, unresolved-topic clustering / content gaps). Plus **Copilot** for agents: AI drafts, conversation summarization, tone rewriting, article suggestion — human always in the loop.

### 2.5 Knowledge
Hosted Help Center: collections/articles, rich editor, multilingual translations, custom domain + SSL, theming, SEO, article feedback (reactions), audience targeting; internal-only articles; **Knowledge Hub**: unified source management — articles, PDFs, synced website URLs, snippets, and third-party syncs (Notion, Confluence, Zendesk import); content audience scoping; AI-readiness status per source.

### 2.6 Outbound / proactive messaging
Message types: in-app chats & posts, banners, tooltips, product tours, checklists, surveys (NPS/CSAT/custom, with branching), news items, mobile push, email (one-off broadcasts + templates, drag-drop editor), SMS. **Series**: visual multi-step campaign builder (triggers, waits, branches, A/B splits, exit rules); audience targeting on attributes/events/segments; control groups; frequency capping & delivery windows; subscription types (opt-in categories) and consent management; per-message and per-series stats (sent/open/click/reply/goal).

### 2.7 Workflows (no-code automation)
Visual builder with versioning; triggers (new conversation, customer message, attribute/event change, admin action, webhook, schedule, SLA breach); condition branches on any attribute; bot steps (ask for reply, collect data, disambiguate, custom question flows); actions (assign, tag, set attribute, snooze, apply SLA, send reply/macro, call webhook/app action, hand to AI agent, route to team); reusable sub-workflows; away-mode rules; execution log per run.

### 2.8 Customer data platform (CRM)
Contacts (users & leads) and companies; typed custom attributes (definitions + values); event tracking API with metadata; dynamic segments (attribute + event + company predicates); CSV import/export with dedupe & merge; subscription/consent state; data-retention policies; GDPR tooling (export, delete/anonymize).

### 2.9 Reporting
Prebuilt reports: conversation volume, responsiveness (first response/handling time), team & teammate performance, SLA attainment, CSAT, AI-agent performance, topics; custom reports: chart builder over defined datasets with filters, group-bys, and drill-down to underlying conversations; dashboards; scheduled exports; realtime queue monitor (waiting count, longest wait, agents online).

### 2.10 Platform & administration
Public REST API + webhooks (topic subscriptions, signed deliveries, retries); OAuth apps + app framework with UI extension points (inbox side panel cards, messenger apps) via a declarative card schema (Canvas-Kit-style); integration directory (Slack, Salesforce, HubSpot, Jira, Stripe, Zapier as early set); data export (CSV now, warehouse sync later); workspaces with regional data hosting (US first, EU phase 3); seats, roles & granular permissions; teams; office-hours schedules; SSO/SAML + 2FA; audit log; usage-based billing (seats + metered AI resolutions); test/dev workspaces.

## 3. Goals / non-goals

**Goals**

- G1. Capability parity with the surface in §2, reached in phases, with an independently sellable product at the end of **each** phase.
- G2. AI agent as a first-class subsystem with measurable resolution rate and per-resolution billing from its first release.
- G3. Growth-SaaS scale envelope (§5) on boring, operable infrastructure a team of ≈10 engineers can run.
- G4. Public API from phase 1 — the platform is API-first internally, so the public API is a thin layer, not a rewrite.

**Non-goals (v1 horizon, ≤18 months)**

- N1. Intercom-scale (25k+ workspaces, billions of messages/mo). We design the growth path but do not build it now.
- N2. Multi-region active-active. Single region (US) with EU residency as a phase-3 cell, not a distributed database.
- N3. Marketing-automation depth beyond §2.6 (no ad-network integrations, no CDP reverse-ETL).
- N4. On-prem/self-hosted deployments.
- N5. Pixel-level imitation of Intercom's UI (legal + we can do better in places).

## 4. Sizing assumptions (the envelope everything is designed to)

| Dimension | Design target (24-month horizon) | Notes |
|---|---|---|
| Tenant workspaces | 1,000–5,000 active | Skewed: median ≈5 seats, p95 ≈50 seats |
| Agent seats | ≈40,000 total; ≈5,000 concurrently active peak | Concurrency ≈ 12% of seats, business-hours peaks |
| Contacts (end users) | ≈100M rows total | Median tenant ≈2k, p95 ≈100k, a few 1M+ outliers |
| New messages (all channels) | 10–50M/month | ≈ 40/s in-hours average, **≈120/s peak** design point at 50M/mo |
| Conversations | 4–6M/month | ≈10 parts per conversation average |
| Tracked events | up to 500M/month | Highest-volume stream; batched ingestion |
| Concurrent Messenger connections | 200k–500k | Widget open on end-customer sites |
| Outbound email | up to 20M/month | Campaigns + transactional |
| AI-agent conversations | up to 1.5M/month | ≈60–70% of inbound eligible for AI first touch |

Where these numbers bite and what gets hot first is worked in RFC-001 §5 and RFC-002 §3.

## 5. Phased roadmap

Each phase ends with a shippable, sellable product; each phase's exit criteria are the next phase's entry gate. Team assumption: 6 engineers at start, ramping to ≈12–14 by phase 2 (see §6).

### Phase 0 — Foundation & core loop (months 0–3)
The minimum loop: visitor messages in the widget → agent answers in the inbox → history persists.

- Platform skeleton: workspaces, auth (email+password, Google), seats/roles v1, billing integration (Stripe) with plans & seat metering.
- CRM v1: contacts, companies, typed custom attributes, identify/track APIs.
- Messenger v1: web widget (chat only), identity verification, attachments, ratings; CDN-delivered versioned bundle.
- Inbox v1: team inboxes, states, manual + round-robin assignment, notes/mentions, macros, tags, realtime updates, contact side panel.
- Email channel v1: inbound forwarding, outbound replies on custom domain (DKIM/SPF), threading.
- Help Center v1: collections/articles, hosted site on subdomain, in-widget article search.
- Reporting v0: conversation volume + responsiveness, realtime queue view.
- Public API v0 (read + message send) + signed webhooks (conversation topics).
- **Exit criteria:** 10 design partners live; message p95 persist→inbox-render < 1s; zero cross-tenant leakage under RLS test suite; SOC 2 controls started.

### Phase 1 — Sellable MVP: AI agent + automation (months 4–6)
What makes it a product rather than a chat tool.

- **Neko v1 (AI agent):** RAG over help center + snippets; handoff rules; preview sandbox; resolution metering + billing; agent analytics v0 (resolution rate, handoff reasons). Chat channel only.
- Workflows v1: triggers/conditions/actions incl. "hand to Neko", collect-data bot steps; execution logs.
- Inbox v2: SLAs + business hours, custom views, balanced assignment, collision detection, CSAT reporting.
- Knowledge Hub v1: PDF + URL sync as sources; audience scoping.
- Outbound v1: in-app posts/chats + one-off email broadcasts with subscription types.
- Segments + CSV import; Slack + Zapier + generic webhook integrations; mobile SDKs beta.
- **Exit criteria:** Neko ≥35% resolution rate on design-partner traffic (measured per RFC-003 §8 definition); workflow engine at-least-once with zero duplicate side effects in chaos tests; first paying tenants on metered billing.

### Phase 2 — Omnichannel + outbound depth + platform (months 7–12)
Parity on breadth; this is the largest phase.

- Channels: WhatsApp, Facebook Messenger, Instagram, SMS (Twilio); Neko on email + WhatsApp.
- Tickets: ticket types, customer/back-office/tracker tickets, portal in Messenger.
- Outbound v2: Series builder (visual graph, waits/branches/A-B), surveys, tours, banners, tooltips, checklists, push; frequency capping, control groups.
- Copilot for agents (drafts, summarize, tone); Neko actions (admin-defined API tools) + custom answers.
- Custom reports & dashboards (chart builder over datasets); CSV scheduled exports.
- App framework v1: OAuth apps, inbox cards + messenger apps (declarative schema); directory with ≈10 launch integrations.
- Enterprise base: SAML SSO, granular permissions, audit log.
- **Exit criteria:** ≥5 channels GA; Neko ≥50% resolution on eligible traffic; series engine survives 10× send-burst load test; app framework has 3 external developers building.

### Phase 3 — Scale, voice & enterprise (months 13–18)
- Voice channel (SIP/Twilio Voice + realtime speech pipeline) with Neko-on-voice pilot.
- Neko v3: procedures (multi-step policies), guidance, per-tenant eval harness, unresolved-topic clustering → content-gap suggestions.
- EU data-residency cell; warehouse export (S3/Parquet + BigQuery/Snowflake share); multibrand (multiple messengers/help centers per workspace).
- Reporting v3: topics via embedding clustering; forecasting (staffing).
- Scale hardening per RFC-001 §8: events pipeline graduation (ClickHouse), search graduation (OpenSearch) **if numbers demand**.
- SOC 2 Type II complete; HIPAA/BAA decision point.
- **Exit criteria:** 99.9% rolling-90-day API availability; support 1M-contact tenant imports; EU cell live.

## 6. Team & cost model (rough, USD)

**Team ramp:** phase 0: 6 eng (2 backend, 2 frontend, 1 full-stack, 1 AI/ML) + design + PM. Phase 1–2: +4–6 (channels, workflows/outbound, AI eval, SRE-leaning backend). Phase 3: ≈14 eng total + first support/solutions hires. Payroll dominates: ≈$2.2–3.5M/yr fully loaded at phase-2 size.

**Infrastructure at target envelope** (detail in RFC-001 §5.4): ≈$8–12k/month at the 50M msg/mo, 500k-connection design point — before LLM inference. **LLM inference is the swing cost:** at 1M Neko conversations/mo, $0.01–0.05 model cost per conversation ⇒ $10–50k/mo, recovered by per-resolution pricing (≈$0.99) with healthy margin (RFC-003 §9). Early phases run at <$2k/mo infra.

## 7. Top risks (project level)

| Risk | Likelihood | Mitigation |
|---|---|---|
| AI resolution rate below sellable bar (<35%) | Medium | Eval harness from day one (RFC-003 §8); curated custom answers; scope Neko to eligible intents first; human-handoff UX that never dead-ends |
| Channel-partner gatekeeping (Meta/WhatsApp BSP approval, carrier SMS review) | Medium-high | Start applications in phase 1; use aggregator (Twilio/Meta BSP) rather than direct integrations; email+chat carry MVP |
| Scope sprawl — §2 is ≈8 products | High | Phase gates above are contractual; each phase sellable; non-goals enforced |
| Realtime fleet cost/complexity underestimated | Medium | Buy-not-build gateway default (RFC-001 §6.2); load test at 2× target connections in phase 1 |
| Data-layer decisions calcify wrong (tenancy, keys, partitioning) | Low-med | RFC-002 spends its deliberation on exactly these irreversible choices; review gate before phase-0 code |
| Legal/brand challenge | Low | Original implementation & branding; counsel review of marketing and import tooling |

## 8. Open questions

1. Pricing architecture: pure seat + metered resolutions (recommended, mirrors market), or bundle tiers? Decide before phase-1 billing build.
2. Buy vs build for SAML/SSO (WorkOS vs self-hosted) and feature flags (Unleash vs LaunchDarkly) — small decisions, phase-2 deadline.
3. Voice stack (Twilio Voice + media streams vs LiveKit) — decide start of phase 3; affects nothing earlier.
4. Migration tooling from Intercom/Zendesk exports (a growth lever; scope in phase 2).

---

_Continue to RFC-001 (system architecture), RFC-002 (data layer), RFC-003 (AI agent)._
