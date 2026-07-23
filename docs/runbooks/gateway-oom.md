# Runbook — Gateway (Centrifugo) node OOM / crash

**Severity:** SEV-2 (realtime degraded on affected node; reconnect storm risk).

## Alert name
`GatewayNodeOOM`

## Metric / expression
Centrifugo is a separate tier (not a `relay_*` emitter). Alert on the gateway's own node metrics (memory, restart count) and on the fan-out impact:

```promql
# Node memory near the 4 GB/node envelope, or a crash-restart
centrifugo_node_memory_bytes / (4 * 1024 * 1024 * 1024) > 0.9
```

Impact corroboration from Relay side: `relay_outbox_pending_rows`/`oldest_age` stay healthy (relay is fine) while the round-trip fan-out probe degrades and gateway connection count drops then re-climbs.

## Symptom / user impact
A gateway node dies; its ~50k–80k websockets drop and reconnect. During the reconnect storm (up to 8.3k handshakes/s over 60s at full scale, RFC-001 §5.2) realtime render latency spikes transiently. Clients on healthy nodes are unaffected. Polling fallback covers any gap.

## Dashboards to open
- **Gateway (Centrifugo)** — per-node memory/CPU, connection count, handshake rate, restarts.
- **Round-trip probe** — sender→subscriber p95 during the storm.
- **Relay / saturation** — confirm the delay is gateway-side, not relay-side.

## Diagnosis steps
1. Which node(s) OOM'd? Check memory trend vs the 4 GB/node envelope (RFC-001 §5.2).
2. Is it steady-state growth (leak / undersized) or a reconnect-storm feedback loop (a deploy or another node's crash caused a wave)?
3. Confirm jittered client backoff is active — an un-jittered storm re-crashes the surviving nodes.
4. Verify the OOM was isolated to the gateway tier and did **not** touch the API (the tier is deliberately separate so a gateway OOM can't take the API down, RFC-001 §6.1).

## Mitigation
**Immediate**
- Let the orchestrator replace the crashed node; connections rebalance.
- If a storm is cascading, temporarily add gateway capacity (nodes are 4 GB, 6–10 at envelope) to absorb handshakes.
- Confirm clients are reconnecting with jittered backoff (not synchronized).

**Follow-up**
- If memory grew steadily, right-size nodes or raise the count; investigate a connection/subscription leak.
- Verify staged rollout for gateway config/image changes to avoid storm-inducing simultaneous restarts.

## Escalation
Gateway on-call. Page if multiple nodes cascade or if the API tier shows any correlated impact (should be impossible by design — investigate the isolation).

## Related RFC / runbooks
- RFC-001 §5.2 (sizing, reconnect storm), §6.1 (separate tier), §9 (gateway OOM row).
- `slo-burn-fanout.md`, `redis-pubsub-down.md`.
- Game-day drill: `scripts/chaos/kill_gateway.sh` (see `docs/gameday-phase0.md`).
- IaC: `infra/terraform/centrifugo.tf` (node count / memory sizing).
