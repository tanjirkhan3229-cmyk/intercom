# Zapier integration

Connect Relay to 6000+ apps via Zapier. Authentication is a Relay **API key** (`relaysk_…`) with the
appropriate scope; the key is created in Relay settings and pasted into Zapier.

## Auth

Zapier validates the connection against:

```http
GET /v0/zapier/auth/test        # requires a `read`-scoped key → { "ok": true, "workspace_id": "wrk_…" }
```

## Triggers (REST hooks)

Zapier subscribes/unsubscribes to a topic; Relay then delivers each matching event to Zapier's hook
URL using the same signed, retrying webhook pipeline as `POST /v0/webhooks` (so triggers inherit
retries, the circuit breaker, and HMAC signing for free).

```http
POST   /v0/zapier/subscriptions   { "topic": "conversation.created", "target_url": "https://hooks.zapier.com/…" }
DELETE /v0/zapier/subscriptions/{id}
```

Supported trigger topics: `conversation.created`, `contact.created` (any public webhook topic is
accepted). The `target_url` is SSRF-validated (public HTTPS only).

## Actions

Zapier actions call Relay's existing public API directly (no Zapier-specific endpoints):

| Action | Endpoint |
|---|---|
| Create/upsert a contact | `POST /v0/contacts` · `POST /v0/contacts/identify` |
| Reply to a conversation | `POST /v0/conversations/{id}/reply` |
| Create a conversation | `POST /v0/conversations` |

A `write`-scoped key is required for actions; `read` suffices for auth + trigger management is
`write`. All calls are rate-limited per workspace (standard `X-RateLimit-*` / `Retry-After` headers).
