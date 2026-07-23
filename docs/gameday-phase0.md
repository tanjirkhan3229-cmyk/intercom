# Game-day record — Phase 0

Chaos drills and load tests exercising the RFC-001 §9 failure modes and the §3 SLOs, satisfying the Phase-0 gate requirement: *"a game-day doc records chaos results and fixes"* and RFC-001 §9's *"quarterly restore + rollback drills"* / *"load test the §5 numbers before each phase gate."*

Drill scripts live under `scripts/chaos/`; k6 scenarios under `load/k6/`.

**Run of record: 2026-07-23**, against the full docker-compose stack (`infra/docker-compose.yml` — all four runtime shapes + the outbox-relay / realtime-fanout / channels-dispatch / webhook-dispatch consumers), metrics scraping live (`/metrics`, `METRICS_PORT` 9100). Row counts are read via the Postgres superuser (RLS bypass) for exact before/after checksums. This stack is single-instance, so timing-based targets (RPO/RTO windows, a full 20k-connection storm, Multi-AZ auto-failover duration) are re-measured on staging (prod shape) before GA — those rows are marked accordingly. The *logical* invariants (zero loss, idempotent dedupe, checksum integrity, SLO latencies) are proven here.

## Findings & fixes (headline)

| # | Finding | Severity | Fix | Verified |
|---|---|---|---|---|
| F1 | The **outbox relay did not survive a Postgres failover**: `run_relay()` had no reconnect handling, so the writer restart in Drill 3 killed it with an unhandled `psycopg.OperationalError` (at `ctl.notifies`). It exited (1) and did not recover, so the outbox stopped draining (backlog climbed, undetected). | **High** (silent async-delivery stall after any DB failover) | Wrapped the relay session in a reconnect loop with capped backoff (`outbox_relay.run_relay`); the advisory lock auto-releases on session death and at-least-once holds because unpublished rows are never deleted. Added regression test `tests/unit/test_outbox_relay_reconnect.py`. | Re-ran Drill 3: relay logged `outbox.relay.reconnect` → `outbox.relay.started` and stayed `Up`; Drill 1 then drained normally. |

This is exactly what the gate is for — a chaos drill surfaced a real resilience hole (RFC-001 §9 "Postgres failover → app-side reconnect") that unit/integration tests didn't cover, and it was root-caused from the container logs and fixed before sign-off.

---

## Drill 1 — Kill Redis broker

**Hypothesis.** Killing the Celery broker Redis stalls async task processing but loses **no** must-not-lose effects: they buffer in the `outbox` (written in the same txn as the domain write) and drain on recovery, deduped by `outbox_id`. The interactive send/persist path (a DB txn) stays up.

**Method.** `scripts/chaos/kill_redis_broker.sh` — baselines `conversation_parts` + `outbox`, stops `redis-broker`, drives 5 conversations + replies (10 parts) through the API with the broker down, asserts every write is 2xx, restarts the broker, polls until the outbox drains, and re-checks row counts.

**Expected (RFC-001 §9 "Redis broker down" + §6.5).** HTTP send path unaffected (writes persist to Postgres + outbox in one txn, independent of the broker); on recovery the backlog drains with zero loss and zero duplicate effects (at-least-once + idempotent).

**Observed — PASS.**

| Signal | Before | During | After recovery | PASS/FAIL |
|---|---|---|---|---|
| `conversation_parts` | 4212 | +10 (→4222) | 4222 | ✅ |
| HTTP write success (broker down) | — | 100% 2xx (10/10) | — | ✅ |
| Parts lost | 0 | — | 0 | ✅ |
| `outbox` backlog | 0 | buffered | drained → 0 | ✅ |

**Result:** parts 4212 → 4222 (+10, no loss); outbox drained 0 → 0. Runbook: `runbooks/redis-broker-down.md`.

---

## Drill 2 — Kill gateway node

**Hypothesis.** Killing Centrifugo drops its websockets; the API tier is unaffected (separate tier, RFC-001 §6.1) and realtime fan-out buffers on the outbox; clients reconnect with jittered backoff.

**Method.** `scripts/chaos/kill_gateway.sh` — asserts `centrifugo /health` OK, stops `centrifugo`, drives messages while it's down (asserting the API still serves + parts persist), restarts it, polls health back to OK, re-checks parts.

**Expected (RFC-001 §5.2, §6.1, §9 gateway OOM row).** API golden signals show no correlated impact; messages persist and fan-out is buffered; gateway health recovers.

**Observed — PASS.**

| Signal | Before | During | After recovery | PASS/FAIL |
|---|---|---|---|---|
| API `/healthz` (gateway down) | 200 | 200 | 200 | ✅ |
| `conversation_parts` | 4225 | +10 | 4235 | ✅ |
| Centrifugo `/health` | OK | down | OK | ✅ |
| Parts lost | 0 | — | 0 | ✅ |

**Result:** API served throughout; parts 4225 → 4235 (+10, fan-out buffered on the outbox); gateway health recovered. Real clients reconnect with jittered backoff (RFC-001 §9); the full reconnect-storm-at-scale timing is a staging measurement (see connection storm below). Runbook: `runbooks/gateway-oom.md`.

---

## Drill 3 — Postgres failover simulation

**Hypothesis.** A writer restart causes a brief write blip; a client retry with the same `Idempotency-Key` is non-duplicating; the app + outbox relay reconnect to the promoted writer.

**Method.** `scripts/chaos/pg_failover_sim.sh` — sends a create with a fixed `Idempotency-Key`, restarts `postgres`, waits for `pg_isready`, retries the **same** request with the **same** key, and verifies exactly one row was created.

**Expected (RFC-001 §3 RPO/RTO, §9 Postgres failover row, RFC-002 §9).** Retried write applies exactly once (no duplicate row); app + relay resume after the failover window.

**Observed — PASS (and surfaced finding F1).**

| Signal | Before | After recovery | PASS/FAIL |
|---|---|---|---|
| Same request, same `Idempotency-Key` → conversation id | `cnv_33uZbfuzM1GENW63B2rhJ` | **same** id returned | ✅ |
| `conversations` | 3587 | +1 (3588) | ✅ (dedupe) |
| `conversation_parts` | 4224 | +1 (4225) | ✅ (dedupe) |
| Duplicate rows from retry | 0 | 0 | ✅ |
| Outbox relay after failover | running | **reconnected + running** (post-fix F1) | ✅ |
| RPO / RTO window | — | re-measure on staging Multi-AZ | ⏳ staging |

**Result:** idempotency deduped the cross-failover retry (same id both times, +1/+1). **Before fix F1** the relay died on the restart; **after fix** it logged `outbox.relay.reconnect (backoff_s=1.0)` → `outbox.relay.started` and kept draining. Runbook: `runbooks/postgres-failover.md`.

---

## Drill 4 — Restore drill (backup restore)

**Hypothesis.** A dump/restore into a scratch DB reproduces the source exactly (row-count checksum), proving the backup is restorable (RFC-001 §9 "an untested backup is a prayer").

**Method.** `scripts/chaos/restore_drill.sh` — seeds a conversation + reply, `pg_dump`s `relay`, restores into a scratch `relay_restore` DB, compares row counts of `conversations`, `conversation_parts`, `contacts`, `outbox`, then drops the scratch DB.

**Expected (RFC-001 §3, RFC-002 §9).** Row counts match exactly; restore succeeds.

**Observed — PASS.**

| Table | Source | Restored | PASS/FAIL |
|---|---|---|---|
| `conversations` | 3587 | 3587 | ✅ |
| `conversation_parts` | 4224 | 4224 | ✅ |
| `contacts` | 2970 | 2970 | ✅ |
| `outbox` | 0 | 0 | ✅ |
| RPO / RTO timing | — | re-measure on staging PITR | ⏳ staging |

**Result:** row-count checksum matched on all key tables. PITR RPO ≤ 5 min / RTO ≤ 1 h are staging measurements (require real snapshot + WAL). Related: `runbooks/postgres-failover.md`, `runbooks/rls-audit-failure.md`.

---

## Load results (k6)

### Message path @ 20 msg/s — PASS

`load/k6/message_path.js`, 20 send iters/s + 10 inbox iters/s for 30s, via `grafana/k6` against the stack.

| Metric | SLO (RFC-001 §3) | p95 measured | PASS/FAIL |
|---|---|---|---|
| Send persist + ack | < 250 ms | **14.7 ms** | ✅ |
| Inbox view (list) | < 300 ms | **14.38 ms** | ✅ |
| Error rate | ~0 | **0.00%** (0 / 1505) | ✅ |

Checks: signup 201, identify 200, inbox 200, conversation 201, reply 201 — all ✅. Comfortable ≥10× headroom on the interactive path at Phase-0 scale.

### Connection storm — PASS (local smoke) / ⏳ staging for full 20k

`load/k6/connection_storm.js` (`k6/ws`, Centrifugo). Local smoke `STORM_TARGET=200`:

| Metric | Target | Measured | PASS/FAIL |
|---|---|---|---|
| Successful WS sessions | > 95% | **100%** (169/169) | ✅ |
| `ws_connecting` p95 | absorbed | **8.95 ms** | ✅ |
| Full 20k reconnect burst + gateway memory | no OOM, fan-out p95 < 1 s | re-run on staging-scale gateway | ⏳ staging |

The full 20k-connection storm (RFC-001 §5.2) needs the staging gateway tier (6–10 × 4 GB nodes); locally it is scaled via `STORM_TARGET`. See `load/README.md`.

---

## Sign-off

| Item | Date | Result |
|---|---|---|
| Drills 1–4 run (docker-compose, prod-shape topology) | 2026-07-23 | ✅ all pass; F1 found + fixed |
| k6 message path @ 20 msg/s vs §3 SLOs | 2026-07-23 | ✅ send 14.7ms / inbox 14.38ms / 0% error |
| Connection storm (local smoke) | 2026-07-23 | ✅ 100% sessions; full 20k ⏳ staging |
| Staging re-run for timing targets (RPO/RTO, Multi-AZ failover duration, 20k storm) | — | ⏳ before GA |

This record is the chaos-results evidence for the Phase-0 gate (see `phase0-exit-criteria.md`).
