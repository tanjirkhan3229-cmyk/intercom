# Relay Mobile SDK — HTTP API contract (v0)

The iOS and Android SDKs are thin, native clients over the same versioned public API the web
messenger widget uses (`relay.modules.messaging` + `relay.modules.platform`). This is the
canonical contract both SDKs implement. All paths are prefixed `/v0`; base URL is configurable
(default `https://api.relay.dev`).

## Authentication

- **Boot is unauthenticated.** It resolves the workspace from the public `app_id` (a `wrk_…`
  id, safe to embed in an app binary).
- **Every other call** sends the widget session token from boot as
  `Authorization: Bearer <session_token>`. (The web widget uses an httpOnly cookie; native apps
  use the bearer header — the token is also returned in the boot JSON body for exactly this.)

### Identity verification (HMAC) — **the SDK never holds the secret**

For a *logged-in* user, the integrator's **backend** computes

```
user_hash = hex( HMAC_SHA256( workspace_identity_secret, external_id ) )
```

and hands `external_id` + `user_hash` to the app, which passes them to the SDK's `login(...)`.
The workspace identity secret **must never be shipped in the app** — the SDK only ever forwards a
precomputed `user_hash`. An anonymous visitor boots with no `user`/`user_hash` (a "lead").

## Endpoints

| Method | Path | Auth | Body → Response |
|---|---|---|---|
| POST | `/v0/widget/boot` | none | `BootRequest` → `BootResponse` (200; 403 if identity verification is on and `user_hash` is missing/wrong; 404 unknown `app_id`) |
| GET | `/v0/widget/conversations?cursor=&limit=` | bearer | → `Page<Conversation>` |
| POST | `/v0/widget/conversations` | bearer | `{ body, attachments? }` → `Conversation` (201) |
| GET | `/v0/widget/conversations/{id}/parts?cursor=&after=&limit=` | bearer | → `Page<Part>` |
| POST | `/v0/widget/conversations/{id}/reply` | bearer + `Idempotency-Key` | `{ body, attachments? }` → `Part` (201) |
| POST | `/v0/widget/conversations/{id}/rating` | bearer | `{ rating (1–5), remark? }` → `Part` (201) |
| POST | `/v0/widget/conversations/{id}/realtime-token` | bearer | → `{ token, ws_url }` (Centrifugo) |
| POST | `/v0/widget/devices` | bearer | `DeviceRegister` → `{ id, platform, status }` (200) |
| DELETE | `/v0/widget/devices?token=<token>` | bearer | → 204 |
| POST | `/v0/widget/uploads/presign` | bearer | `{ filename, content_type }` → `{ key, upload_url, method:"PUT" }` |
| GET | `/v0/widget/uploads/download-url?key=<key>` | bearer | → `{ url }` |

### Schemas

```jsonc
// BootRequest
{ "app_id": "wrk_…",
  "user":   { "external_id": "u_42", "email": "a@b.com", "name": "Ada" },   // optional
  "user_hash": "9f…hex",                                                     // optional (see above)
  "resume_token": "…" }                                                      // optional continuity

// BootResponse
{ "session_token": "eyJ…",
  "contact": { "id": "usr_…", "kind": "user" | "lead", "email": null, "name": null },
  "config":  { "primary_color": "#…", "launcher_position": "left"|"right", "greeting": null,
               "expected_reply_time": null, "office_hours": null,
               "identity_verification_enabled": false },
  "conversations": [ Conversation, … ] }

// DeviceRegister
{ "platform": "ios" | "android",
  "token": "<APNs hex / FCM registration token>",
  "app_id": "com.example.app",              // optional: APNs bundle id / Android package name
  "environment": "production" | "sandbox" } // APNs host; default "production"

// Conversation
{ "id": "cnv_…", "contact_id": "usr_…", "channel": "chat", "state": "open"|"snoozed"|"closed",
  "assignee_id": null, "team_id": null, "priority": false, "waiting_since": null,
  "snoozed_until": null, "last_part_at": "…ISO8601…", "first_contact_reply_at": null,
  "ai_status": null, "created_at": "…ISO8601…" }

// Part
{ "id": "msg_…", "conversation_id": "cnv_…", "author_kind": "contact"|"admin"|"ai_agent"|"system",
  "author_id": null, "part_type": "comment"|"note"|"rating"|"assignment"|"state_change",
  "body": "…", "attachments": [ { "key": "…", "filename": "…", "content_type": "…" } ],
  "meta": {}, "created_at": "…ISO8601…" }

// Page<T> (keyset pagination)
{ "items": [ T, … ], "next_cursor": "…" | null }
```

## Push notifications

1. The app obtains its APNs device token (iOS) / FCM registration token (Android).
2. After boot, register it: `POST /v0/widget/devices`. Re-register whenever the OS rotates the
   token (the server upserts on the token — safe to call every launch).
3. On logout/uninstall, `DELETE /v0/widget/devices?token=…`.
4. When an agent or the AI agent replies, the backend fans out a notification to the contact's
   active devices with a `data` payload `{ "conversation_id": "cnv_…", "type":
   "conversation.reply" }`. Tapping the notification deep-links to that conversation.

Dead tokens (APNs 410 / FCM `UNREGISTERED`) are retired server-side automatically.

## Attachments

1. `POST /v0/widget/uploads/presign` → `{ key, upload_url }`.
2. `PUT` the file bytes to `upload_url` with the `Content-Type` used in step 1 (direct to S3).
3. Reference the object in a reply: `attachments: [{ "key", "filename", "content_type" }]`.
