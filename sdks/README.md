# Relay Mobile SDKs (beta — P1.10)

Native messenger SDKs for end-user apps, thin clients over the same public API the web widget
uses (`/v0/widget/*`). Booted with a public `app_id`, authenticated per-user with the same HMAC
identity scheme, with native conversation UI and push notifications.

| SDK | Path | Distribution | Push | Size budget |
|---|---|---|---|---|
| iOS (Swift) | [`ios/`](./ios) | Swift Package Manager | APNs | ≤ 3 MB |
| Android (Kotlin) | [`android/`](./android) | Maven / Gradle | FCM (+ notification direct-reply) | ≤ 2.5 MB |

- **[`API_CONTRACT.md`](./API_CONTRACT.md)** — the canonical HTTP contract both SDKs implement.
- Backend: device-token registry + push fan-out worker live in the `messaging` module
  (`device_tokens` / `push_receipts` tables, `relay push-dispatch` consumer → `messaging.send_push`
  on the `send.channels` queue → APNs/FCM). See RFC-000 §2.1, RFC-002 §5.6.

**Beta status / verification:** this repo is a Python + TypeScript monorepo with no mobile
toolchain, so the SDKs are **not compiled or size-measured in CI here** — they are built and
published from a mobile CI. The backend they depend on (registration, fan-out, dedupe, token
rotation, tenancy) is fully tested in `apps/api` (`tests/*/test_*push*`).

**Identity verification:** the workspace identity secret is never shipped in an app. The
integrator's backend computes `user_hash = hex(HMAC_SHA256(secret, external_id))` and the app
passes only that to the SDK's `login(...)`.
