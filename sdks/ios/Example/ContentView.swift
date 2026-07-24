import SwiftUI
import RelayMessenger

struct ContentView: View {
    // Owned by AppDelegate; observed here to present push deep-links.
    @ObservedObject private var router = DeepLinkRouter.shared
    @State private var state: BootState = .loading

    enum BootState { case loading, ready, failed(String) }

    var body: some View {
        content
            .task { await boot() }
            .sheet(item: $router.link) { link in
                NavigationView { ConversationView(conversationId: link.id) }
            }
    }

    @ViewBuilder private var content: some View {
        switch state {
        case .loading:
            ProgressView("Connecting…")
        case .ready:
            ConversationListView()
        case .failed(let message):
            VStack(spacing: 12) {
                Text("Couldn’t connect").font(.headline)
                Text(message).font(.caption).foregroundColor(.secondary).multilineTextAlignment(.center)
                Button("Retry") { Task { await boot() } }.buttonStyle(.borderedProminent)
            }
            .padding()
        }
    }

    private func boot() async {
        state = .loading
        do {
            // 1. Boot as an anonymous lead. Replace with YOUR workspace app_id (a wrk_… id —
            //    safe to embed in the binary). Point baseURL at your Relay deployment.
            try await Relay.shared.boot(
                appId: "wrk_REPLACE_ME",
                baseURL: URL(string: "https://api.relay.dev")!
            )

            // 2. (Optional) Identify a logged-in user. `userHash` MUST come from your backend —
            //    it is HMAC_SHA256(workspace_secret, externalId). The secret never ships here.
            // try await Relay.shared.login(externalId: "u_42", userHash: hashFromYourBackend)

            // 3. Ask for push permission + register with APNs (after boot, so the device-token
            //    callback has a session to register against).
            await PushManager.requestAuthorization()

            state = .ready
        } catch {
            state = .failed(error.localizedDescription)
        }
    }
}
