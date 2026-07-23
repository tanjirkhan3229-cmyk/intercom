# Runbook — Redis pub/sub down (realtime fan-out outage)

**Severity:** SEV-2 (realtime degraded; polling fallback keeps the product working).

## Alert name
`RedisPubSubDown`

## Metric / expression
The pub/sub / cache Redis (port 6379 dev) is what the relay publishes the `relay:outbox` stream to and what feeds Centrifugo fan-out. Symptom: relay publishes fail → outbox oldest-age climbs, and the fan-out probe stalls.

```promql
relay_outbox_oldest_age_seconds > 60
  and rate(relay_outbox_pending_rows[5m]) >= 0
```

Corroborate with `/readyz` `checks.redis=false` and the sender→subscriber round-trip probe timing out.

## Symptom / user impact
Realtime updates stop propagating: new messages don't push to widgets/inbox in real time. **Product still works** — clients fall back to polling (RFC-001 §2: "realtime down ⇒ polling still works"). The relay cannot publish to the stream, so `relay_outbox_oldest_age_seconds` rises (rows correctly buffer in Postgres, at-least-once).

## Dashboards to open
- **Relay / saturation** — `relay_outbox_oldest_age_seconds`, `relay_outbox_pending_rows`.
- **Gateway (Centrifugo)** — publish errors, subscriber counts (should hold; clients reconnect/poll).
- **Redis (pub/sub / cache)** — reachability, failover, CPU/memory.
- **Round-trip probe** — sender→subscriber latency (expected to break during outage).

## Diagnosis steps
1. Confirm it is the pub/sub/cache Redis (6379), distinct from the broker (6380) — a broker outage is `redis-broker-down.md`.
2. Verify the relay is logging publish failures / not emitting `outbox.relay.published`.
3. Confirm clients degraded to polling (the widget/agent app should switch automatically).
4. Check ElastiCache for node failure/failover on the cache cluster.

## Mitigation
**Immediate**
- Restore/replace the pub-sub Redis node.
- On recovery the relay drains the buffered `relay:outbox` stream automatically; consumers dedupe by `outbox_id`, so redelivery is safe.
- No manual replay needed.

**Follow-up**
- Verify Centrifugo reconnect with jittered backoff (avoid reconnect storm on recovery, RFC-001 §5.2).
- Confirm the polling-fallback path is exercised in the game-day (`kill-redis` variants) and meets acceptable degraded latency.

## Escalation
Infra/on-call for the ElastiCache outage. Gateway on-call if Centrifugo does not resume publishing after Redis recovers.

## Related RFC / runbooks
- RFC-001 §2, §5.2, §6.5, §9 (Redis pub/sub down row).
- `outbox-oldest-age.md`, `slo-burn-fanout.md`, `gateway-oom.md`, `redis-broker-down.md`.
