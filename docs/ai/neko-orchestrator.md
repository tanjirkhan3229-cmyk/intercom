# Neko orchestrator (P1.2)

The `ai` module's turn pipeline — the autonomous support agent from RFC-003. Stateless orchestration
code on the `ai.interactive` worker queue over state in Postgres/Redis; no separate AI service.

## The turn (RFC-003 §3 state machine)

```
customer message (outbox → ai-dispatch → ai.interactive)
  → preflight (cheap model, ≤400 ms: language, safety, "talk to a person", is-question)
  → query rewrite (cheap model)
  → retrieve (P1.1 hybrid RRF, RLS-scoped)
  → grounding gate ─ insufficient ─▶ clarify once, then handoff
  → generate (frontier model, streamed, citations required)
  → verify (cheap model: groundedness + policy filters)
  → emit answer  |  handoff
```

Every edge has a timeout + fallback (settings `ai_*_timeout_seconds`); the total turn budget is
20 s. **Neko never dead-ends a customer:** an explicit "talk to a person", a safety flag, low
grounding past the clarify budget, an uncited or verifier-rejected answer, or provider exhaustion all
route to a human — with a private recap note (recap, sources tried, sentiment) and
`conversations.ai_status = handed_off`.

Streaming is ephemeral: generation tokens go **directly** through Redis pub/sub → the gateway (never
the durable outbox); only the final, verified part is persisted (and fanned out normally). If the
verifier rejects the draft, a `superseded` signal tells the widget to drop it and the durable handoff
arrives via the outbox.

## Ledger + replay (RFC-003 §3, §8)

Every turn writes one `agent_runs` row — retrieval set, prompt hash, per-stage models/tokens/cost,
latency breakdown, verdict, outcome, and a full `trace`. `POST /v0/ai/runs/{id}/replay` re-runs
generation from the stored trace: the **prompt hash always reproduces** (same inputs ⇒ same prompt);
the **answer reproduces exactly** under the deterministic/seeded model — the "why did Neko say that?"
guarantee. `UNIQUE (workspace_id, trigger_part_id)` is the exactly-once claim gate (a redelivered
trigger no-ops).

## Provider abstraction + resilience (RFC-003 §9, RFC-001 §9)

Two providers behind one interface (`providers.py`): `DeterministicProvider` (hermetic dev/test/CI —
reproducible, structurally injection-safe) and `HttpLLMProvider` (prod, OpenAI-compatible, streaming
+ tool calls + token accounting). `resilience.py` adds per-provider **circuit breakers**, **rate-limit
pools**, **timeout budgets**, **model tiering** (cheap: preflight/rewrite/verify; frontier:
generation), and **failover**: the router walks providers in order, skipping open breakers, until one
answers. Streaming fails over on the *first token*, so a blackholed provider is abandoned before any
delta reaches the customer — failover is user-invisible.

## Injection posture (RFC-003 §6)

Retrieved chunks and customer text are **data, never instructions**: framed as delimited, typed DATA
blocks (`protocol.py`), with the delimiters escaped out of the content so a chunk cannot close its
own block and smuggle a directive. The system policy is the only instruction channel; generation must
cite chunk ids and the verifier rejects ungrounded claims. Retrieval runs under the same RLS/`app.ws`
regime as everything else, so the model can never be prompted into another tenant's corpus. The
`redteam.py` corpus (injection / jailbreak / exfiltration / cross-tenant) runs in CI with a ≥ 98 %
pass-rate gate (`test_ai_redteam.py`).

## Kill switches (RFC-003 §6)

- **Per-workspace:** `ai_settings.enabled` (opt-in; Neko is off until a workspace turns it on).
- **Global:** `AI_GLOBAL_ENABLED` + `AI_MODEL_ROUTE` (`auto` | `primary` | `secondary` | `off`).
  `off` routes every turn to humans; a provider name pins the route. Settings bools, runtime-toggled
  like `realtime_fallback` (Unleash graduates them in P1.3).

## Operations

- `relay ai-dispatch` — the outbox → `ai.interactive` trigger consumer (its own process/compose
  service, like the realtime fan-out). Filters customer `comment` parts, enqueues `ai.run_turn`.
- Workers already consume `ai.interactive` (concurrency-capped per provider **and** per workspace).

## Implementation notes / deviations from RFC-003

1. **Grounding gate signal.** RFC-003 §5 frames the gate on "fused retrieval confidence". Raw RRF
   scores are method- and scale-dependent, so the gate uses a method-agnostic proxy: the fraction of
   distinctive (rewritten-)query terms grounded in the retrieved evidence (morphology-tolerant),
   thresholded by the per-workspace `grounding_threshold` (default 0.1, lenient). The **verifier** is
   the fine-grained groundedness guard on the generated answer; the gate is a coarse
   answer-vs-clarify pre-filter. Revisit with the rerank benchmark (RFC-003 §10 open item).
2. **Actions / procedures (RFC-003 §5) are deferred.** P1.2 builds the answer pipeline
   (retrieve → generate → verify → emit/handoff). The tool interface (`ToolSpec`/`ToolCall`, SSRF-proxied
   execution) is present on the provider seam but the allowlisted tool loop / procedure walker is a
   later milestone; the `ACT → TOOLS` branch of the §3 diagram is not yet wired.
3. **Stale `pending` runs.** A worker crash between the claim and finalize leaves a `pending`
   `agent_runs` row that the claim gate will not retry (never double-answers). A beat sweep to
   re-open stale pending rows is a documented follow-up; the customer is never dead-ended (they can
   ask for a human).
