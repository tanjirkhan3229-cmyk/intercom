# Centrifugo — realtime gateway (P0.4)

Relay *buys* its websocket tier (RFC-001 §6.1 gateway row): Centrifugo owns the connections and
pub/sub fan-out; the API only mints tokens and publishes.

`config.json` holds **channel topology only** — the two namespaces that match
`relay.core.realtime`:

- `conv:{cnv_id}` — one conversation's thread
- `inbox:{wrk_id}:{team}` — an inbox view (`team` is a team public id, or `all` = workspace
  firehose / `none` = unassigned)

Presence/join-leave are **off** on both namespaces: typing and presence are kept in Redis with a
TTL and relayed through Centrifugo, never held by the gateway or Postgres (RFC-002 §2 note).

## Secrets come from the environment, never this file (RFC-001 §13)

`docker-compose` injects them into the `centrifugo` service, sourced from the same `.env` values
the API reads, so minted JWTs verify:

| Centrifugo env var | Sourced from |
|---|---|
| `CENTRIFUGO_TOKEN_HMAC_SECRET_KEY` | `CENTRIFUGO_TOKEN_SECRET` (API: `centrifugo_token_secret`) |
| `CENTRIFUGO_SUBSCRIPTION_TOKEN_HMAC_SECRET_KEY` | `CENTRIFUGO_TOKEN_SECRET` |
| `CENTRIFUGO_API_KEY` | `CENTRIFUGO_API_KEY` (API: `centrifugo_api_key`) |
| `CENTRIFUGO_REDIS_ADDRESS` | `redis://redis-cache:6379` |

The gateway listens on container `:8000`, published to host `:8001` (the API already owns `:8000`).
Production topology (6–10 nodes, Redis engine) is stubbed in `infra/terraform/centrifugo.tf`.
