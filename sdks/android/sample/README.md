# Relay Android — sample app

A minimal Compose app that boots Relay, lists conversations, opens a thread, and registers for
FCM push.

## Run

1. Open `sdks/android` in Android Studio (it picks up `settings.gradle.kts`).
2. Set your workspace's public app id in `MainActivity.kt`:
   ```kotlin
   private const val RELAY_APP_ID = "wrk_REPLACE_ME"
   ```
3. **Firebase / FCM:** the checked-in `google-services.json` is a **placeholder**. Download the
   real one from the Firebase console (Project settings → Your apps → Android, package
   `dev.relay.sample`) and drop it in place of this file. Without it the app still compiles, but
   push registration will fail at runtime.
4. Run the `sample` configuration on a device/emulator with Google Play services.

## What it demonstrates

- `Relay.boot(context, appId)` on launch (anonymous lead). The commented `Relay.login(...)` line
  shows identifying a user with a backend-computed `user_hash`.
- Conversation list → tap → thread with send.
- FCM token fetch + `Relay.registerPushToken(token)`; rotation is handled by the SDK's
  `RelayMessagingService.onNewToken`.
- Deep-link handling: a notification tap re-enters `MainActivity` (singleTop) and
  `Relay.conversationIdFrom(intent)` routes straight to the thread.
- Direct reply from the notification (RemoteInput) works with no app code — it's entirely inside
  the library's `ReplyReceiver`.
