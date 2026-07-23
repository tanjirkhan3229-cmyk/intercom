# Runbook — SLO burn: message fan-out (realtime render)

**Severity:** SEV-2 (realtime path SLO burn); degrades gracefully to polling.

## Alert name
`SLOBurnFanout`

## SLO
Message fan-out: sender → recipient render **p95 < 1 s** (RFC-001 §3). Also the Phase-0 exit bar (persist→inbox-render <1s, RFC-000 §5).

## Metric / expression
Fan-out is not a single HTTP route; it is the end-to-end chain **request → outbox → relay publish → Redis pub/sub → Centrifugo → client**. Approximate the server-side portion from the outbox saturation gauges + a synthetic probe:

```promql
# Server-side lag proxy: oldest un-fanned-out event
relay_outbox_oldest_age_seconds > 1
```

The authoritative measurement is the k6/synthetic round-trip probe (see `docs/gameday-phase0.md` connection/round-trip test) that times sender-POST → subscriber-receive. Alert on that probe's p95 crossing 1 s.

## Symptom / user impact
Messages appear late for the recipient (visitor or watching agent). If pub/sub is fully down, clients fall back to polling (degraded but functional, RFC-001 §2 "realtime down ⇒ polling still works").

## Dashboards to open
- **Relay / saturation** — `relay_outbox_oldest_age_seconds`, `relay_outbox_pending_rows` (is the delay upstream in the relay?).
- **Gateway (Centrifugo)** — node health, connection count, publish latency, reconnect storm.
- **Redis pub/sub** — publish throughput, CPU.
- **Synthetic round-trip probe** — sender→subscriber p95.

## Diagnosis steps
1. Is the lag before or after the relay? High `relay_outbox_oldest_age_seconds` ⇒ upstream (relay stalled) → `outbox-oldest-age.md`.
2. Relay healthy (publishing) but clients slow ⇒ pub/sub or gateway. Check Redis pub/sub and Centrifugo.
3. Gateway node OOM/crash → `gateway-oom.md`; reconnect storm (8.3k handshakes/s over 60s, RFC-001 §5.2) inflates render latency transiently.
4. Redis pub/sub down → `redis-pubsub-down.md` (clients should be polling).

## Mitigation
**Immediate**
- Relay-side lag: restart/unblock the relay (`outbox-oldest-age.md`).
- Gateway-side: replace the crashed node; jittered client reconnect spreads the storm.
- Pub/sub down: confirm clients fell back to polling; restore Redis pub/sub.

**Follow-up**
- Verify jittered-backoff reconnect config on the client to prevent storm-induced latency spikes.
- Ensure the fan-out round-trip probe runs continuously as the SLI.

## Escalation
Gateway/backend on-call if p95 >1 s persists >10 min, or immediately if pub/sub is fully down and polling fallback is not observed.

## Related RFC / runbooks
- RFC-001 §2, §3, §5.2, §6.5. RFC-000 §5 (exit criteria).
- `outbox-oldest-age.md`, `redis-pubsub-down.md`, `gateway-oom.md`.
