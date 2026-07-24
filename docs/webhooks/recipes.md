# Outbound webhook recipes

Relay delivers signed, at-least-once webhooks for the topics below. This is a task-oriented guide;
for the exact signature algorithm see [signature-verification.md](./signature-verification.md).

## Available topics

| Topic | Fires when | Key payload fields (`data`) |
|---|---|---|
| `conversation.created` | a new conversation opens | `conversation_id`, `contact_id`, `channel`, `state` |
| `conversation.part.created` | any new part (message/note/etc.) | `conversation_id`, `part_id`, `part_type`, `author_kind` |
| `contact.created` | a contact is first identified/created | `contact_id`, `email`, `external_id`, `kind` |
| `contact.updated` | a contact's fields/attributes change | `contact_id`, `email`, `external_id`, `kind` |

Every delivery is a JSON envelope: `{ "id", "topic", "created_at", "data" }`. `id` is stable across
retries — **dedupe on it** (delivery is at-least-once).

## Delivery & retry semantics

- **Signed:** `Relay-Signature: v1=<hmac>` + `Relay-Timestamp`; verify within a freshness window
  (see the signing doc). Reject anything that fails — it isn't from us.
- **At-least-once:** you may receive a delivery more than once; dedupe on `Relay-Event-Id`.
- **Retries:** non-2xx / timeout → exponential backoff with jitter up to 72h. A `2xx` is success.
- **Circuit breaker + auto-disable:** sustained failures open a per-endpoint breaker and eventually
  disable the subscription (you're notified). Return `2xx` fast (< 10s) and do slow work async.
- **Targets must be public HTTPS** (an SSRF guard rejects private/loopback/metadata addresses).

## Recipe: notify a chat channel

Subscribe to `conversation.created`; on receipt, post a summary to your channel. For Slack
specifically, prefer the managed **Slack integration** (below) — it threads replies and supports
reply-from-Slack, which a raw webhook can't.

## Recipe: sync contacts to your CRM

Subscribe to `contact.created` + `contact.updated`; upsert into your CRM keyed on `external_id`
(fall back to `email`). Idempotent because you dedupe on the event id.

## Recipe: reply/SLA analytics

Subscribe to `conversation.part.created`; stream parts into your warehouse, keyed by
`conversation_id` + `part_id`.
