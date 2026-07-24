# Relay Android SDK (beta)

Native Kotlin/Jetpack Compose SDK for the Relay messenger. It's a thin client over the same
versioned public API the web widget uses (`/v0/widget/*`, see `../API_CONTRACT.md`).

- Module: `relay-messenger` (`com.android.library`, namespace `dev.relay.messenger`)
- minSdk 24 · compileSdk 34 · Kotlin 1.9.24 · Compose (compiler 1.5.14)
- Deps: OkHttp · kotlinx-serialization · kotlinx-coroutines · Compose · firebase-messaging

## Install (Maven / Gradle)

Published via `maven-publish`. In your app module:

```kotlin
dependencies {
    implementation("dev.relay:relay-messenger:0.1.0-beta01")
}
```

FCM support also needs the Google services plugin + a `google-services.json` (see **FCM setup**).

## Quickstart

```kotlin
// 1. Boot once at launch (anonymous "lead"). The app_id is a public wrk_… id, safe to embed.
Relay.boot(context, appId = "wrk_live_…")

// 2. (Optional) identify the user — see the HMAC note below.
Relay.login(externalId = "u_42", userHash = "<hex from your backend>")

// 3. Show the messenger (Compose). Themed from the boot config automatically.
ConversationListScreen(
    onOpen = { id -> /* navigate */ },
    onNewConversation = { scope.launch { val c = Relay.startConversation("Hi!"); /* open c.id */ } },
)
// … and the thread:
ConversationScreen(conversationId = id)
```

All API methods are `suspend` functions and throw `RelayException(status, message, errorCode)`
on non-2xx. Ids (`wrk_`/`usr_`/`cnv_`/`msg_`) are opaque strings.

## HMAC identity — the secret never ships in the app

For a logged-in user, your **backend** computes

```
user_hash = hex( HMAC_SHA256( workspace_identity_secret, external_id ) )
```

and hands `external_id` + `user_hash` to the app. The SDK only ever **forwards** the precomputed
`user_hash` in `login(...)`; it never holds or derives the workspace identity secret. Anonymous
visitors just call `boot(...)` with no user (a "lead").

## FCM push + notifications

1. **Setup:** apply the `com.google.gms.google-services` plugin and add your real
   `google-services.json`. The library already registers `RelayMessagingService` and
   `ReplyReceiver` in its manifest — no host wiring needed.
2. **Register:** after boot, fetch the token and call `Relay.registerPushToken(token)`. The
   server upserts on the token, so calling it every launch is safe. Rotation is automatic:
   `RelayMessagingService.onNewToken` re-registers.
3. **Unregister** on logout: `Relay.logout()` calls `unregisterPush()` for you.
4. **Deep link:** the backend sends a `data` payload `{ conversation_id, type: "conversation.reply" }`.
   Tapping the notification opens your launcher activity; read the target with
   `Relay.conversationIdFrom(intent)` in `onCreate`/`onNewIntent` and route to the thread.
5. **Reply from the notification (RemoteInput direct reply):** the posted notification carries a
   "Reply" action with an inline input. The user's text is delivered to `ReplyReceiver`, which
   calls `Relay.replyFromNotification(...)` — this works from a cold process (the SDK persists the
   session token and re-boots from the stored identity if needed). **No app code required.**

If you run your own `FirebaseMessagingService`, forward messages with
`Relay.handleRemoteMessage(context, remoteMessage.data)` and tokens with
`Relay.registerPushToken(token)`.

## Attachments

```kotlin
val ref = Relay.uploadAttachment(filename, contentType, bytes)   // presign → PUT to S3 → reference
Relay.reply(conversationId, "See attached", attachments = listOf(ref))
```

## Theming

Colors and launcher position come from the boot `config` and are overridable:

```kotlin
RelayTheme(colors = RelayColors(primary = Color(0xFF6D28D9))) {
    ConversationListScreen(...)
}
// or read config directly:
val side = launcherPosition()   // LauncherPosition.LEFT / RIGHT from config.launcher_position
```

`RelayTheme` seeds `primary` from `config.primary_color`; the `ConversationListScreen` /
`ConversationScreen` composables are already wrapped in it.

## Realtime (future)

Beta polls `parts?after=` for new messages. The API also exposes a Centrifugo realtime token
(`POST /v0/widget/conversations/{id}/realtime-token`, surfaced as `RelayClient.realtimeToken`);
wiring the websocket is a post-beta upgrade and is intentionally not implemented here.

## Beta & size budget

Target: **≤ 2.5 MB** added to the host app. The dependency set is deliberately minimal —
OkHttp, kotlinx-serialization, kotlinx-coroutines, Compose (already present in most modern
apps), and firebase-messaging — which keeps the SDK under budget. We avoid heavier options
(Retrofit/Moshi/Gson stacks, `material-icons-extended` ~9 MB, image loaders) on purpose; icons
are plain text and dates are kept as ISO8601 strings (no datetime library). R8/ProGuard rules
for the serializable models ship in `consumer-rules.pro`.

**Not built in this repo's CI** — this is a Python/TS monorepo with no Android/Gradle toolchain.
Open `sdks/android` in Android Studio to build the library and the `sample` app.
