# RelayMessenger (iOS) — beta

Native Swift SDK for the Relay messenger: boot/identify, a conversation list + thread UI, send
replies, APNs push with deep-linking, and attachment uploads. It is a thin native client over
the same versioned public API the web widget uses (`../API_CONTRACT.md`).

- **iOS 15+**, Swift, SwiftUI.
- **Zero third-party dependencies** — URLSession + SwiftUI + Apple system frameworks only.
- Ships one library product: `RelayMessenger`.

## Install (Swift Package Manager)

Xcode → File → Add Package Dependencies → **Add Local…** → select the `sdks/ios` folder, or add
to a `Package.swift`:

```swift
.package(path: "../sdks/ios")            // or a git URL once published
// target dependency:
.product(name: "RelayMessenger", package: "RelayMessenger")
```

## Quickstart

```swift
import RelayMessenger

// 1. Boot (anonymous "lead"). app_id is a public wrk_… id, safe to embed.
try await Relay.shared.boot(appId: "wrk_123",
                            baseURL: URL(string: "https://api.relay.dev")!)

// 2. (Optional) Identify a logged-in user — see HMAC note below.
try await Relay.shared.login(externalId: "u_42", userHash: hashFromYourBackend)

// 3. Show the messenger — drop the native views anywhere.
ConversationListView()                        // list + "new message"
ConversationView(conversationId: "cnv_…")     // a single thread (also used for deep-links)
```

Programmatic access (if you build your own UI):

```swift
let page = try await Relay.shared.conversations()
let convo = try await Relay.shared.startConversation(body: "Hi!")
let parts = try await Relay.shared.parts(conversationId: convo.id)
let reply = try await Relay.shared.reply(conversationId: convo.id, body: "Following up")
_ = try await Relay.shared.rate(conversationId: convo.id, rating: 5, remark: "Great help")
```

## Identity verification (HMAC) — the SDK never holds the secret

For a logged-in user, your **backend** computes:

```
user_hash = hex( HMAC_SHA256( workspace_identity_secret, external_id ) )
```

and returns `external_id` + `user_hash` to the app. The app passes them to `login(...)`. The
**workspace identity secret must never be shipped in the app** — the SDK only ever forwards the
precomputed `user_hash`. Anonymous visitors simply `boot()` with no user (a "lead").

## Push notifications (APNs)

1. Enable **Push Notifications** + **Background Modes → Remote notifications** on your app
   target; upload an APNs `.p8` key to your Relay workspace.
2. After `boot()`, request permission and register:

   ```swift
   await PushManager.requestAuthorization()   // prompts, then registerForRemoteNotifications()
   ```

3. In your `AppDelegate`, forward the token and route taps:

   ```swift
   func application(_ app: UIApplication,
                    didRegisterForRemoteNotificationsWithDeviceToken token: Data) {
       Task { try? await Relay.shared.registerForPushNotifications(deviceToken: token) }
   }

   func userNotificationCenter(_ c: UNUserNotificationCenter,
                               didReceive response: UNNotificationResponse) async {
       let info = response.notification.request.content.userInfo
       if let cnv = Relay.shared.handleNotification(userInfo: info) {
           // present ConversationView(conversationId: cnv)
       }
   }
   ```

**Token rotation:** iOS calls `didRegister…` on every launch and whenever the token refreshes.
Forward it every time — the server upserts on the token, so re-registering is safe and expected.
On logout, `Relay.shared.unregisterPush()` (also called by `logout()`) retires the token.

The push payload is `{ "conversation_id": "cnv_…", "type": "conversation.reply" }`;
`handleNotification` returns the `conversation_id` to deep-link to.

## Attachments

```swift
let att = try await Relay.shared.uploadAttachment(
    data: imageData, filename: "photo.jpg", contentType: "image/jpeg")
_ = try await Relay.shared.reply(conversationId: cnv, body: "Here you go", attachments: [att])
```

Under the hood: presign → `PUT` the bytes directly to S3 → reference the object `key` in the reply.

## Theming

The launcher color and position come from the boot `config` and seed `RelayTheme`. Override any
time — the host value wins:

```swift
// Override at boot…
try await Relay.shared.boot(appId: "wrk_123",
    theme: RelayTheme(primaryColorHex: "#7C3AED", launcherPosition: .left))
// …or later.
await Relay.shared.setTheme(RelayTheme(primaryColorHex: "#0057FF"))
```

The bundled `ConversationListView` / `ConversationView` read the theme automatically.

## Realtime (future)

Beta polls `GET …/parts?after=` for new messages (a few-second cadence). The realtime upgrade is
the `POST …/{id}/realtime-token` endpoint + a Centrifugo websocket subscription — deliberately
not implemented in beta.

## Beta / size budget

- **Budget: the compiled SDK stays ≤ 3 MB.** This is met by having **zero third-party
  dependencies** — it uses only URLSession, SwiftUI, Foundation, and UserNotifications (all Apple
  system frameworks, which add no weight to your app binary). Nothing to vendor, nothing to audit.
- **Not built in this repo's CI** — this is a Python/TypeScript monorepo with no mobile
  toolchain. Open `sdks/ios` in Xcode and build/test against an iOS destination (the UI uses
  iOS-only SwiftUI APIs, so a plain macOS `swift build` won't compile).
- Beta scope is intentionally lean: no offline cache, no websocket realtime, in-memory session
  token (persist to Keychain for cross-launch resume — see the note in `RelayClient.swift`).
