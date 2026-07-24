import SwiftUI

/// Messenger appearance. Seeded from the boot `config`, overridable by the host via
/// `Relay.shared.setTheme(_:)` (or the `theme:` argument to `boot`).
/// Color is stored as a hex string so the value stays `Sendable` and persistable.
public struct RelayTheme: Sendable, Equatable {
    public enum LauncherPosition: String, Sendable { case left, right }

    public var primaryColorHex: String
    public var launcherPosition: LauncherPosition

    public init(primaryColorHex: String = "#0057FF", launcherPosition: LauncherPosition = .right) {
        self.primaryColorHex = primaryColorHex
        self.launcherPosition = launcherPosition
    }

    public var primaryColor: Color { Color(hex: primaryColorHex) }

    static func from(config: MessengerConfig) -> RelayTheme {
        RelayTheme(
            primaryColorHex: config.primaryColor ?? "#0057FF",
            launcherPosition: LauncherPosition(rawValue: config.launcherPosition ?? "right") ?? .right
        )
    }
}

extension Color {
    /// Parses `#RRGGBB` (or `RRGGBB`). Falls back to system blue on malformed input.
    init(hex: String) {
        let s = hex.trimmingCharacters(in: CharacterSet(charactersIn: "# ")).uppercased()
        guard s.count == 6, let v = UInt64(s, radix: 16) else {
            self = .blue
            return
        }
        self = Color(
            red: Double((v & 0xFF0000) >> 16) / 255,
            green: Double((v & 0x00FF00) >> 8) / 255,
            blue: Double(v & 0x0000FF) / 255
        )
    }
}
