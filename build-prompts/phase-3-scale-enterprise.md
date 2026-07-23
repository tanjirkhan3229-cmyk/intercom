# Phase 3 — Scale, Voice & Enterprise (months 13–18)

Goal: voice channel, Neko procedures + eval harness, EU residency cell, warehouse export, multibrand, and the *conditional* scale graduations — pulled only if their RFC triggers fire. Exit criteria: RFC-000 §5 Phase 3.

---

### P3.1 — Voice channel pilot
**Depends on:** P2 gate · **Read first:** RFC-000 §5 Phase 3, §8 (voice stack decision); RFC-003 §7 (voice pointer)

> First: write the voice design doc RFC-000 §8 defers (Twilio Voice + Media Streams vs LiveKit; decide with a latency spike, record as RFC-004). Then build the pilot:
>
> - Media service colocated with the gateway tier (RFC-001 §6.6): SIP/number provisioning, inbound call → streaming ASR → the P1.2 turn pipeline in voice mode (partial-utterance handling, barge-in, smaller model tier + aggressive custom-answer matching per RFC-003 §7) → streaming TTS; human escalation = warm transfer to agent browser (WebRTC) with live transcript; voicemail fallback off-hours.
> - Calls land as conversations (`channel='voice'`) with recording + transcript parts; consent/recording disclosures configurable per workspace jurisdiction.
> - Neko-on-voice behind per-workspace pilot flag; latency budget: first audible response < 1.5 s p95.
>
> **Acceptance:** live pilot call resolves an order-status query via an Action, then warm-transfers on request with transcript visible to the agent; sub-1.5 s p95 measured over 100 staged calls; recording consent line plays where configured; transcript quality spot-audit ≥ 95% WER-acceptable.

### P3.2 — Neko v3: procedures, guidance, per-tenant evals, content gaps
**Depends on:** P2.7 · **Read first:** RFC-003 §5 (procedures), §8 (evals, gaps); RFC-002 §5.6 (workflow ledger pattern)

> - **Procedures:** versioned declarative multi-step policies (guard conditions, steps, slot schema) compiled to constrained plans walked step-by-step on a ledger (reuse the workflow_run_steps pattern) — the model fills slots and drafts messages; **the ledger owns control flow** (RFC-003 §5 verbatim). Admin authoring UI with test-run sandbox; procedures can invoke Actions with the same budgets.
> - **Guidance:** freeform per-workspace behavioral rules injected as policy (versioned, eval-gated on save — a guidance change runs the tenant's eval set before activating).
> - **Per-tenant eval harness UI** per RFC-003 §8: golden sets (sampled + hand-labeled transcripts, synthetic per vertical), LLM-judge with calibration workflow (human spot-audit queue, κ tracked), run-on-demand + auto-run on any prompt/model/guidance change; shadow mode + auto-halt flag wired to the judge metrics.
> - **Content gaps:** weekly clustering of unresolved/handoff `agent_runs` embeddings (HDBSCAN batch on `housekeeping`), surfaced as ranked "write this article" suggestions with example questions; one-click draft via Copilot's article generator.
>
> **Acceptance:** a multi-step refund procedure executes with a forced mid-run crash and resumes without repeating the refund Action (ledger test); guidance change that breaks a golden set is blocked with a diff report; gap suggestions on the pilot tenants rated useful ≥ 60% by admins (survey); judge–human agreement κ ≥ 0.7 on the calibration set.

### P3.3 — EU data-residency cell
**Depends on:** P2 gate · **Read first:** RFC-001 §8 (cell model — full second stack, no cross-region data plane), §12; RFC-002 §10

> Write the cell design doc first (RFC-005: cell = complete regional stack; what's global — billing account registry, signup router, status page — vs per-cell — everything else). Then:
>
> - Terraform-driven second full stack in eu-central-1 (all RFC-001 runtime shapes + data stores); region choice at workspace creation, immutable thereafter (v1); signup/login router resolves workspace→cell; widget/API endpoints per-cell (`eu.` subdomains) with the loader auto-resolving from app_id.
> - Global control plane kept minimal: workspace registry + Stripe (documented data flows for DPA); zero tenant data crosses regions (assert with VPC flow-log audit).
> - Ops parity: the full observability/deploy/DR stack per cell; game-day drill in EU cell; per-cell status pages.
> - GDPR completion: DPA template, SCC review with counsel, data-flow map, EU SES/provider selection (LLM providers with EU endpoints where available — document the exceptions).
>
> **Acceptance:** an EU workspace's entire request path (traced) touches only EU resources; deploy pipeline promotes to both cells with independent canaries + rollback; EU restore drill passes; residency claims reviewed by counsel before marketing says anything.

### P3.4 — Warehouse export + multibrand ∥
**Depends on:** P2 gate · **Read first:** RFC-000 §5 Phase 3; RFC-002 §5.3 (partition archival), §9

> - **Warehouse export:** nightly per-workspace S3 Parquet exports (conversations, parts, contacts, events, campaign stats — versioned schemas, manifest files) + Snowflake/BigQuery share docs; incremental by updated-watermark; backfill command. Reuse the partition-archival machinery.
> - **Multibrand:** multiple messenger identities + help centers per workspace (brand = theming + domains + channel accounts + office hours), brand picker on outbound/articles, per-brand widget app_ids; help-center custom domains GA (automated cert provisioning) for all brands.
>
> **Acceptance:** exported Parquet round-trips into DuckDB/BigQuery with documented schema and row counts matching source (±0 on a frozen fixture day); two brands on one workspace serve distinct widgets/help centers with correctly scoped articles and no cross-brand leakage in targeting.

### P3.5 — Conditional scale graduations (pull only if triggers fired)
**Depends on:** trigger metrics · **Read first:** RFC-001 §8 (the table — triggers are contractual); RFC-002 §8

> For each lever, build **only if its RFC trigger has fired** (check dashboards; record the decision either way in `docs/decisions/`):
>
> - **Events → ClickHouse** (trigger: >1.5B events/mo or rollup lag >15 min): stand up ClickHouse Cloud, feed from the outbox stream (new consumer, backfill from partitions), move report datasets to it behind the dataset registry (P2.8 makes this swappable), shrink Postgres events retention to 90 days.
> - **Search → OpenSearch** (trigger: FTS p95 >1 s): outbox-fed indexer, dual-read verification period, cutover conversations/contacts search; Postgres FTS remains fallback for recent-90-days.
> - **Messaging bounded-context split** (trigger: writer >60% CPU sustained / WAL >30 MB/s): execute the RFC-002 §8 playbook — new cluster, logical-replication sync, dual-write verification, connection-string cutover by module, cross-seam FKs demoted to app-checked + nightly reconciler.
> - **Gateway fleet regionalization** (trigger: >1M conns): additional gateway PoPs + connection-establish rate limiting.
>
> **Acceptance (per executed lever):** zero-downtime cutover with a rehearsed rollback, dual-read/write verification report, dashboards updated to the new source of truth, RFC updated. If no trigger fired: the decision docs exist and say so with the numbers.

### P3.6 — Reporting v3 + compliance close-out ∥
**Depends on:** P3.2 · **Read first:** RFC-000 §5 Phase 3; RFC-003 §8

> - **Topics:** embedding-cluster conversation topics (weekly batch, human-nameable clusters) as a reporting dimension + trend view ("what are customers asking about"); staffing forecast v0 (volume seasonality → suggested coverage by team/hour).
> - **SOC 2 Type II close-out:** evidence automation (access reviews, change-management exports from CI, incident log), vendor review, policy docs current; **pen-test remediation** from the phase-2 external test tracked to zero criticals.
>
> **Acceptance:** topic clusters stable week-over-week (>0.7 adjusted Rand on overlapping windows); auditor evidence requests fulfilled from automation, not manual scramble; pen-test report closed.

### P3.7 — GA hardening gate (phase 3 / v1 exit)
**Depends on:** all above · **Read first:** RFC-000 §5 Phase 3 exit criteria; RFC-001 §3 (SLOs), §9

> Final gate: 99.9% rolling-90-day API availability evidenced from SLO dashboards; 1M-contact tenant import validated (P1.9 path at 10× with memory profiling); EU cell live with a real tenant; full failure-mode drill matrix from RFC-001 §9 re-run (both cells); cost review against RFC-000 §6 envelope (document drift); on-call rotation + runbooks audited; RFC set updated to as-built. Write `docs/gates/phase-3-ga.md` with the go/no-go and the v2 backlog (everything consciously deferred, from RFC non-goals to gate findings).
