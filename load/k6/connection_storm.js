// P0.12 LOAD — Centrifugo connection storm (RFC-001 §5.2 / §9).
//
// Models the reconnect storm the gateway must survive: a large fleet of clients that all
// drop and reconnect within a short window (deploy, gateway restart, network blip). Each VU
// mints/reuses a Centrifugo connection JWT from the API, dials the WS, sends the
// `connect` command, waits for the reply, then closes — a single reconnect.
//
// Realtime token flow confirmed against apps/api (relay/core/realtime.py + router
// /v0/realtime/token + tests/integration/test_realtime_tokens.py):
//   POST /v0/realtime/token (Bearer) -> 200 {token, ws_url}
// The token is an identity-only agent connection JWT (HS256, no pinned channels), so ONE
// token can back many simultaneous connections — we mint once in setup() and reuse it,
// which is what makes a 20k storm feasible without minting 20k tokens.
//
// Centrifugo WS (host): ws://host.docker.internal:8001/connection/websocket
// The client protocol is JSON framed commands; the first frame is:
//   {"connect": {"token": "<jwt>"}, "id": 1}
// Centrifugo replies with a matching {"id":1,"connect":{...}} on success.
//
// TARGET: RFC-001 §5.2/§9 calls for a 20000-connection reconnect storm. A single-node dev
// Centrifugo + a single k6 container cannot open 20k real sockets; STORM_TARGET defaults to
// 20000 (the spec number) and is overridable so the same script runs as a local smoke
// (e.g. STORM_TARGET=200) or at staging scale. See load/README.md.
//
// Run:
//   docker run --rm -i -e BASE_URL=http://host.docker.internal:8000 \
//       -e STORM_TARGET=200 grafana/k6 run - < load/k6/connection_storm.js

import ws from 'k6/ws';
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://host.docker.internal:8000';
const WS_URL =
  __ENV.WS_URL || 'ws://host.docker.internal:8001/connection/websocket';

// Full storm = 20000 (RFC-001 §5.2). Override down for local smoke.
const STORM_TARGET = parseInt(__ENV.STORM_TARGET || '20000', 10);
const RAMP = __ENV.RAMP || '60s'; // ramp the whole fleet in ~60s

const sessionSuccess = new Rate('storm_session_success');
const connectMs = new Trend('storm_connect_ms', true);

export const options = {
  scenarios: {
    storm: {
      executor: 'ramping-arrival-rate',
      startRate: 0,
      timeUnit: '1s',
      // Reach STORM_TARGET reconnects/sec over RAMP, hold briefly, then drain.
      preAllocatedVUs: Math.min(STORM_TARGET, 2000),
      maxVUs: Math.min(STORM_TARGET, 5000),
      stages: [
        { target: Math.ceil(STORM_TARGET / 60), duration: RAMP },
        { target: Math.ceil(STORM_TARGET / 60), duration: '10s' },
        { target: 0, duration: '5s' },
      ],
      exec: 'reconnect',
    },
  },
  thresholds: {
    // ws_connecting is k6's built-in WS handshake timing.
    ws_connecting: ['p(95)<1000'],
    // >95% of reconnect sessions must complete the connect handshake cleanly.
    storm_session_success: ['rate>0.95'],
  },
};

// Mint ONE agent connection token; reuse it across every simulated reconnect.
export function setup() {
  const suffix = `${Date.now()}-${Math.floor(Math.random() * 1e9)}`;
  const signup = http.post(
    `${BASE_URL}/v0/auth/signup`,
    JSON.stringify({
      workspace_name: `storm-${suffix}`,
      email: `storm-${suffix}@example.com`,
      password: 'password123',
      name: 'Storm Owner',
    }),
    { headers: { 'Content-Type': 'application/json' } },
  );
  check(signup, { 'signup 201': (r) => r.status === 201 });
  const accessToken = signup.json('access_token');

  const rt = http.post(`${BASE_URL}/v0/realtime/token`, null, {
    headers: { Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/json' },
  });
  check(rt, { 'realtime token 200': (r) => r.status === 200 });

  return { token: rt.json('token'), wsUrl: rt.json('ws_url') || WS_URL };
}

export function reconnect(data) {
  // Per-VU jitter so reconnects are spread, not perfectly synchronized.
  sleep(Math.random() * 0.5);

  const start = Date.now();
  let connected = false;

  const res = ws.connect(WS_URL, {}, (socket) => {
    socket.on('open', () => {
      // Centrifugo JSON protocol: send the connect command with our JWT.
      socket.send(JSON.stringify({ connect: { token: data.token }, id: 1 }));
    });
    socket.on('message', (msg) => {
      try {
        const parsed = JSON.parse(msg);
        // A reply carrying our id (and no error) = a successful connect.
        if (parsed.id === 1 && !parsed.error) {
          connected = true;
          connectMs.add(Date.now() - start);
        }
      } catch (_e) {
        // ignore non-JSON frames (pings etc.)
      }
      socket.close();
    });
    socket.on('error', () => {
      socket.close();
    });
    // Bound each simulated session so a stuck socket doesn't pin a VU.
    socket.setTimeout(() => socket.close(), 3000);
  });

  check(res, { 'ws handshake 101': (r) => r && r.status === 101 });
  sessionSuccess.add(connected);
}
