// swift-tools-version:5.9
import PackageDescription

// RelayMessenger — beta iOS SDK for Relay.
// Zero third-party dependencies: URLSession + SwiftUI + Apple system frameworks only.
// This is how the ≤3 MB binary budget is met — nothing to vendor, nothing to audit.
let package = Package(
    name: "RelayMessenger",
    platforms: [.iOS(.v15)],
    products: [
        .library(name: "RelayMessenger", targets: ["RelayMessenger"])
    ],
    targets: [
        .target(name: "RelayMessenger"),
        .testTarget(name: "RelayMessengerTests", dependencies: ["RelayMessenger"])
    ]
)
