import Foundation
import UserNotifications
#if canImport(UIKit)
import UIKit
#endif

/// APNs helpers: token → hex, notification-tap → conversation deep-link, and the
/// authorization/registration dance. All Apple system frameworks — no third-party deps.
public enum PushManager {

    /// APNs delivers the device token as raw `Data`; the server expects it as a hex string.
    public static func hexToken(from deviceToken: Data) -> String {
        deviceToken.map { String(format: "%02x", $0) }.joined()
    }

    /// Extracts the deep-link `conversation_id` from a notification payload.
    /// The backend fans out `{ "conversation_id": "cnv_…", "type": "conversation.reply" }`.
    /// APNs custom keys land at the top level; we also check a nested `data` dict for parity
    /// with the Android/FCM payload shape.
    public static func conversationId(from userInfo: [AnyHashable: Any]) -> String? {
        if let id = userInfo["conversation_id"] as? String { return id }
        if let data = userInfo["data"] as? [AnyHashable: Any],
           let id = data["conversation_id"] as? String { return id }
        return nil
    }

    /// Requests notification authorization and, if granted, registers for remote (APNs)
    /// notifications. Call this AFTER `Relay.shared.boot(...)` so the resulting device-token
    /// callback has a booted session to register the token against.
    @MainActor
    @discardableResult
    public static func requestAuthorization(
        options: UNAuthorizationOptions = [.alert, .sound, .badge]
    ) async -> Bool {
        let granted = (try? await UNUserNotificationCenter.current().requestAuthorization(options: options)) ?? false
        #if canImport(UIKit)
        if granted { UIApplication.shared.registerForRemoteNotifications() }
        #endif
        return granted
    }
}
