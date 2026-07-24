# Slack integration (v0)

Relay posts conversation notifications into a Slack channel and lets your team **reply from that
Slack thread** — the reply lands in Relay as an agent message.

## What it does

- **Outbound:** a new conversation (and each subsequent *customer* reply) posts to your channel.
  The first post starts a thread; later posts thread under it, so one conversation = one Slack thread.
- **Inbound (reply-from-Slack):** a message you type in that thread is posted back into the Relay
  conversation as an admin reply — customers see it on their channel exactly like an in-app reply.

Agent-authored messages are never echoed back to Slack (no notify → reply → notify loop).

## Setup (v0 — paste a bot token)

1. Create a Slack app in your workspace (manual, one-time). Give it `chat:write` and add it to the
   target channel. Enable **Event Subscriptions** and subscribe to the `message.channels` event.
2. Point the Event Subscriptions **Request URL** at `https://<your-relay>/v0/integrations/slack/events`
   (Slack sends a one-time `url_verification` challenge, which Relay echoes).
3. In Relay, connect the integration (admin):

   ```http
   POST /v0/integrations/slack
   { "team_id": "T…", "channel_id": "C…", "channel_name": "#support",
     "bot_token": "xoxb-…", "signing_secret": "…" }
   ```

   The bot token + signing secret are encrypted at rest (never returned).

## Security

- Every inbound callback is verified against your app's **signing secret** (Slack's
  `v0:{timestamp}:{body}` HMAC-SHA256), within a 5-minute replay window. Unverified requests → 403.
- One Slack workspace maps to exactly one Relay workspace (enforced by a global-unique active
  `team_id`), so inbound events resolve a single tenant deterministically.

## Operating

- Run the dispatch consumer: `relay slack-dispatch` (outbox → Slack posts; the outbound HTTP call
  is on the worker, never on the request path).
- Pause/disconnect: `PATCH /v0/integrations/{id}/status` (`paused`/`disabled`) or
  `DELETE /v0/integrations/{id}`.
- Outbound notifications are **best-effort v0** (bounded Celery retries, no durable ledger). For
  guaranteed delivery to an external system, use signed webhooks.
