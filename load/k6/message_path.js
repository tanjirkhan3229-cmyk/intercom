// P0.12 LOAD — message-path SLO gate (RFC-001 §3, RFC-000 §4).
//
// Two scenarios exercise the proven message flow:
//   (a) `send`  — VUs create a conversation + reply, ramped to a constant 20 msg/s
//                 (2x the RFC-000 §4 phase-0 baseline of ~10 msg/s), tagged endpoint:send.
//   (b) `inbox` — VUs GET /v0/conversations (the agent inbox list), tagged endpoint:inbox.
//
// SLO thresholds (RFC-001 §3):
//   - message send persist+ack   p95 < 250ms   -> http_req_duration{endpoint:send}
//   - inbox view (list)          p95 < 300ms   -> http_req_duration{endpoint:inbox}
//   - error budget               < 1% failed   -> http_req_failed
//
// Auth/message shapes confirmed against apps/api routers + tests/integration:
//   POST /v0/auth/signup {workspace_name,email,password,name} -> 201 {access_token}
//   POST /v0/contacts/identify {external_id} (Bearer)         -> {id}
//   POST /v0/conversations {contact_id, body} (Bearer)        -> 201 {id}
//   POST /v0/conversations/{id}/reply {body} (Bearer)         -> 201 {id}
//   GET  /v0/conversations (Bearer)                           -> 200 {items, next_cursor}
//
// Run (against the local dev stack on the Docker host):
//   docker run --rm -i -e BASE_URL=http://host.docker.internal:8000 \
//       grafana/k6 run - < load/k6/message_path.js

import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://host.docker.internal:8000';

// Keep the smoke short so a single-instance local stack can finish it; override for staging.
const SEND_RATE = parseInt(__ENV.SEND_RATE || '20', 10); // msg/s (2x phase-0 baseline)
const DURATION = __ENV.DURATION || '30s';

export const options = {
  scenarios: {
    // (a) send — constant arrival rate of new messages, independent of response latency.
    send: {
      executor: 'constant-arrival-rate',
      rate: SEND_RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: 20,
      maxVUs: 100,
      exec: 'sendMessage',
      tags: { endpoint: 'send' },
    },
    // (b) inbox — read the inbox list in parallel.
    inbox: {
      executor: 'constant-arrival-rate',
      rate: 10,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: 10,
      maxVUs: 50,
      exec: 'readInbox',
      tags: { endpoint: 'inbox' },
    },
  },
  thresholds: {
    'http_req_duration{endpoint:send}': ['p(95)<250'],
    'http_req_duration{endpoint:inbox}': ['p(95)<300'],
    http_req_failed: ['rate<0.01'],
  },
};

function authHeaders(token) {
  return { headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' } };
}

// setup() runs once: sign up a workspace and identify a contact, returning the shared
// token + contact id used by every VU iteration.
export function setup() {
  const suffix = `${Date.now()}-${Math.floor(Math.random() * 1e9)}`;
  const signup = http.post(
    `${BASE_URL}/v0/auth/signup`,
    JSON.stringify({
      workspace_name: `load-${suffix}`,
      email: `load-${suffix}@example.com`,
      password: 'password123',
      name: 'Load Owner',
    }),
    { headers: { 'Content-Type': 'application/json' } },
  );
  check(signup, { 'signup 201': (r) => r.status === 201 });
  const token = signup.json('access_token');

  const contact = http.post(
    `${BASE_URL}/v0/contacts/identify`,
    JSON.stringify({ external_id: `load-contact-${suffix}` }),
    authHeaders(token),
  );
  check(contact, { 'identify 200': (r) => r.status === 200 });
  const contactId = contact.json('id');

  return { token, contactId };
}

// (a) send — one conversation + one reply = the full persist+ack path.
export function sendMessage(data) {
  const conv = http.post(
    `${BASE_URL}/v0/conversations`,
    JSON.stringify({ contact_id: data.contactId, body: 'load test message' }),
    { ...authHeaders(data.token), tags: { endpoint: 'send' } },
  );
  const ok = check(conv, { 'conversation 201': (r) => r.status === 201 });
  if (!ok) return;

  const convId = conv.json('id');
  const reply = http.post(
    `${BASE_URL}/v0/conversations/${convId}/reply`,
    JSON.stringify({ body: 'agent reply' }),
    { ...authHeaders(data.token), tags: { endpoint: 'send' } },
  );
  check(reply, { 'reply 201': (r) => r.status === 201 });
}

// (b) inbox — the keyset-paginated inbox list.
export function readInbox(data) {
  const res = http.get(`${BASE_URL}/v0/conversations`, {
    ...authHeaders(data.token),
    tags: { endpoint: 'inbox' },
  });
  check(res, { 'inbox 200': (r) => r.status === 200 });
}
