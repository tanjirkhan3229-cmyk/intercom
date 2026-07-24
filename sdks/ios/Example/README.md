# RelayMessenger — Example app

A minimal SwiftUI sample that boots the SDK, shows the conversation list, opens a thread, and
registers for push (with tap-to-deep-link). It is source only — wire these files into an Xcode
app target (there is no `.xcodeproj` checked in).

## Run it

1. **New Xcode project** → App (SwiftUI lifecycle), iOS 15+.
2. Delete the generated `App.swift`/`ContentView.swift` and add the files in this folder:
   `RelayExampleApp.swift`, `ContentView.swift`, `AppDelegate.swift`. Use this `Info.plist`
   (or merge its keys into yours).
3. **Add the package**: File → Add Package Dependencies → "Add Local…" → select `sdks/ios`
   (the folder with `Package.swift`) → add the `RelayMessenger` library to the app target.
4. In `ContentView.swift`, replace `wrk_REPLACE_ME` with your workspace `app_id` and point
   `baseURL` at your Relay deployment (default `https://api.relay.dev`).
5. Build & run on a **real device** (APNs does not work in the Simulator).

## Push setup (APNs)

- **Signing & Capabilities** → add **Push Notifications** and **Background Modes → Remote
  notifications** (already declared in `Info.plist`).
- Upload an **APNs auth key (.p8)** to your Relay workspace so the server can send to APNs.
- On launch the app calls `PushManager.requestAuthorization()` (after boot). When the user
  taps a `conversation.reply` notification, `AppDelegate` extracts the `conversation_id` via
  `Relay.shared.handleNotification(userInfo:)` and `ContentView` presents that thread.

## What to try

- Send a reply from the agent inbox → it appears in the open thread within a few seconds
  (beta polls `parts?after=`).
- Background the app and reply from the inbox → a push arrives; tapping it deep-links to the
  conversation.
