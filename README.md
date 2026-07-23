# Relay — an Intercom-class customer service platform (blueprint)

**Relay** (working codename) is a full architecture blueprint and phased build plan for an AI-first customer-service SaaS with Intercom-class capability: embeddable Messenger, omnichannel helpdesk, autonomous AI agent ("Neko"), help center, outbound messaging, no-code workflows, CRM, reporting, and a public API/app platform — designed for **FastAPI + Next.js + PostgreSQL** at growth-SaaS scale (1–5k workspaces, 10–50M messages/month).

This repo currently contains the **design docs and executable build prompts**. Application code lands via the phased plan below (Prompt P0.0 scaffolds the monorepo).

## What's here

```
├── rfcs/                      # The design — source of truth (each .md has a .docx twin)
│   ├── RFC-000-overview-scope-roadmap    # Feature parity map, sizing envelope, 4-phase roadmap, team & budget
│   ├── RFC-001-system-architecture       # Topology, realtime pipeline, queues/outbox, failure modes, CI/CD, security
│   ├── RFC-002-data-layer                # Postgres schema, tenancy/RLS, indexing, partitioning, pgvector+FTS, ops
│   └── RFC-003-ai-agent                  # Neko: RAG pipeline, orchestration, guardrails, evals, unit economics
└── build-prompts/             # 42 ordered agent prompts that build the product, phase by phase
    ├── README.md                          # How to run them + master engineering rules
    ├── phase-0-foundation.md              # Months 0–3: core loop (widget → inbox), email, help center, billing, API
    ├── phase-1-ai-automation.md           # Months 4–6: Neko v1, workflows, SLAs, outbound v1 → sellable MVP
    ├── phase-2-omnichannel-platform.md    # Months 7–12: WhatsApp/Meta/SMS, tickets, Series, Copilot, apps, SSO
    └── phase-3-scale-enterprise.md        # Months 13–18: voice, procedures/evals, EU cell, scale graduations
```

## Architecture at a glance

A **modular FastAPI monolith** deployed as four runtime shapes — API, segregated Celery worker bulkheads, a websocket gateway (Centrifugo), and Next.js frontends — over **one pooled Postgres** (shared-schema tenancy + row-level security, UUIDv7 keys, monthly-partitioned hot tables, pgvector + FTS hybrid retrieval), stitched together by a **transactional outbox**. Scaling levers (ClickHouse for events, OpenSearch for search, bounded-context DB splits) are pre-designed with contractual triggers, not built prematurely. Full reasoning, capacity math, and failure-mode analysis: `rfcs/RFC-001` and `rfcs/RFC-002`.

The AI agent is a first-class subsystem: hybrid RAG over tenant knowledge, a grounding-gated turn pipeline with streamed responses, admin-defined Actions behind an SSRF-guarded proxy, billing-grade resolution metering, and an eval harness that gates every prompt/model change (`rfcs/RFC-003`).

## How to build it

1. Read `build-prompts/README.md` — it explains the workflow and the master rules (tenancy/RLS, outbox, idempotency, lock-safe migrations) every prompt assumes.
2. Feed the prompts to a coding agent (e.g. Claude Code) **in order**, starting with `P0.0` (monorepo scaffold + `CLAUDE.md` conventions).
3. Hold each milestone to its **Acceptance** list; run each phase's gate prompt before moving on.
4. Treat the RFCs as source of truth — when implementation diverges, update the RFC in the same PR.

## Status & positioning

- **Status:** design complete (RFCs v1, 2026-07); implementation not started.
- **Sizing:** all numbers target the RFC-000 §4 envelope; early phases run on a <$1.5k/mo footprint.
- **Legal:** this is an original, independent design for a competing product in the same category as Intercom. No Intercom code, assets, copy, or trademarks are used — see RFC-000 §1 for the boundaries (not legal advice).
