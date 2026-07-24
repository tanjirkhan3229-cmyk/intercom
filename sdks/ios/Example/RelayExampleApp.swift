import SwiftUI
import RelayMessenger

@main
struct RelayExampleApp: App {
    // Bridges APNs callbacks (they live on UIApplicationDelegate) into SwiftUI.
    @UIApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}
