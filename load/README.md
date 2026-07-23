# Load & connection-storm testing (P0.12)

k6 load scripts that gate the Relay message path against its SLOs (RFC-001 §3) and rehearse
the Centrifugo reconnect storm (RFC-001 §5.2 / §9). Neither k6 nor its browser are installed
natively — everything runs via the `grafana/k6` Docker image, reading the script from stdin.

On macOS, containers reach the host-exposed API/gateway via `host.docker.internal`, which is
why `BASE_URL` defaults to `http://host.docker.internal:8000` and the WS URL to
`ws://host.docker.internal:8001/connection/websocket`.

## Prerequisites

The dev stack must be up (`make dev`) and `GET http://localhost:8000/healthz` must return 200.

## 1. Message-path SLO gate — `k6/message_path.js`

```bash
# from the repo root
docker run --rm -i -e BASE_URL=http://host.docker.internal:8000 \
    grafana/k6 run - < load/k6/message_path.js

# or via the Makefile
make load
```

Two scenarios run concurrently:

| Scenario | What it drives | Rate | Tag |
|---|---|---|---|
| `send` | `POST /v0/conversations` + `/reply` (full persist+ack) | 20 msg/s | `endpoint:send` |
| `inbox` | `GET /v0/conversations` (agent inbox list) | 10 req/s | `endpoint:inbox` |

20 msg/s is **2× the RFC-000 §4 phase-0 baseline** of ~10 msg/s. `setup()` signs up one
workspace (random email suffix for uniqueness) and identifies one contact; every VU reuses
that token.

### Thresholds → SLOs (RFC-001 §3)

| Threshold | SLO |
|---|---|
| `http_req_duration{endpoint:send}: p(95)<250` | message send persist+ack p95 < 250 ms |
| `http_req_duration{endpoint:inbox}: p(95)<300` | inbox view (list) p95 < 300 ms |
| `http_req_failed: rate<0.01` | < 1% error budget |

(Message fan-out < 1 s is verified end-to-end in the chaos drills / realtime tests, not here —
this script measures the synchronous HTTP request path.)

### Tuning

- `SEND_RATE` (default `20`) — send arrival rate in msg/s.
- `DURATION` (default `30s`) — kept short so a single-instance local stack finishes the smoke.
- `BASE_URL` — point at staging, e.g. `-e BASE_URL=https://api.staging.relay.example`.

Locally the numbers will sit well under target (single instance, no network). The gate's job
is to catch regressions and to run at real rate against staging.

## 2. Connection storm — `k6/connection_storm.js`

```bash
# LOCAL SMOKE — a dev-scale Centrifugo + one k6 container cannot open 20k real sockets.
docker run --rm -i -e BASE_URL=http://host.docker.internal:8000 -e STORM_TARGET=200 \
    grafana/k6 run - < load/k6/connection_storm.js

# or via the Makefile (defaults STORM_TARGET low for local; override for staging)
make load-storm
```

Each VU mints/reuses an **identity-only agent connection JWT** (`POST /v0/realtime/token`),
dials the Centrifugo websocket, sends the JSON `connect` command, waits for the reply, then
closes — one modeled reconnect. Because the agent token pins no channels, **one token backs
every simulated connection**, so we mint once in `setup()` and reuse it (minting 20k tokens is
unnecessary and impractical).

| Threshold | Meaning |
|---|---|
| `ws_connecting: p(95)<1000` | 95% of WS handshakes complete in < 1 s |
| `storm_session_success: rate>0.95` | > 95% of reconnect sessions complete the connect handshake |

### Scaling — the full 20k (RFC-001 §5.2)

`STORM_TARGET` defaults to **20000** (the spec number). A single dev Centrifugo node and a
single k6 container will exhaust file descriptors / VUs long before 20k real sockets, so:

- **Locally**, override down: `-e STORM_TARGET=200` (or a few hundred) for a correctness smoke.
- **At staging scale**, run against the multi-node Centrifugo tier (Terraform, `infra/terraform`)
  and either raise k6 VU/ulimit headroom or shard the run across multiple k6 pods
  (`k6 run --execution-segment`) so the 20k reconnects/window are generated in aggregate. The
  script's rate/VU math already targets 20000 when `STORM_TARGET` is left at its default.
- `RAMP` (default `60s`) controls how fast the fleet reconnects; `WS_URL` overrides the gateway.

## Pointing at staging

Set `BASE_URL` (and `WS_URL` for the storm) to the staging endpoints and provide network
reachability. Everything else is identical:

```bash
docker run --rm -i \
    -e BASE_URL=https://api.staging.relay.example \
    -e WS_URL=wss://rt.staging.relay.example/connection/websocket \
    -e STORM_TARGET=20000 \
    grafana/k6 run - < load/k6/connection_storm.js
```
