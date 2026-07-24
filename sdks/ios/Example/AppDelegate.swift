import UIKit
import UserNotifications
import RelayMessenger

/// Identifiable wrapper so a `conversation_id` can drive a SwiftUI `.sheet(item:)`.
struct ConversationLink: Identifiable { let id: String }

/// Shared bus: AppDelegate pushes deep-links here; ContentView observes and presents the thread.
final class DeepLinkRouter: ObservableObject {
    static let shared = DeepLinkRouter()
    @Published var link: ConversationLink?
}

final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        return true
    }

    // APNs handed us a device token. iOS calls this on every launch and whenever the token
    // rotates — forward it every time; the Relay server upserts on the token.
    func application(_ application: UIApplication,
                     didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        Task { try? await Relay.shared.registerForPushNotifications(deviceToken: deviceToken) }
    }

    func application(_ application: UIApplication,
                     didFailToRegisterForRemoteNotificationsWithError error: Error) {
        print("APNs registration failed: \(error.localizedDescription)")
    }

    // Show notifications while the app is foregrounded.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification) async
        -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }

    // Notification tapped → extract the deep-link conversation_id and route to it.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse) async {
        let userInfo = response.notification.request.content.userInfo
        if let id = Relay.shared.handleNotification(userInfo: userInfo) {
            await MainActor.run { DeepLinkRouter.shared.link = ConversationLink(id: id) }
        }
    }
}
