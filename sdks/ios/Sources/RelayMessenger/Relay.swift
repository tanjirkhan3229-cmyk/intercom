import Foundation

/// The public entry point to the Relay messenger. A single shared actor holds session state
/// (token, config, theme) and serialises access to the API client.
///
/// Typical lifecycle:
/// ```
/// try await Relay.shared.boot(appId: "wrk_…")                 // anonymous lead
/// try await Relay.shared.login(externalId: "u_42",            // upgrade to identified user;
///                              userHash: hashFromYourBackend)  // user_hash is HMAC'd server-side
/// ```
public actor Relay {
    public static let shared = Relay()
    private init() {}

    public static let defaultBaseURL = URL(string: "https://api.relay.dev")!

    private var client: RelayClient?
    private var appId: String?
    private var contactValue: Contact?
    private var configValue: MessengerConfig?
    private var themeValue = RelayTheme()
    private var hostTheme: RelayTheme?          // host override wins over boot config
    private var lastDeviceTokenHex: String?
    private var pushEnvironment = "production"
    private let bundleId = Bundle.main.bundleIdentifier

    // MARK: - Boot & identity

    /// Boots an anonymous ("lead") session. Resolves the workspace from the public `app_id`.
    @discardableResult
    public func boot(appId: String,
                     baseURL: URL = Relay.defaultBaseURL,
                     theme: RelayTheme? = nil) async throws -> Contact {
        self.appId = appId
        if let theme { hostTheme = theme }
        self.client = RelayClient(baseURL: baseURL)
        return try await performBoot(user: nil, userHash: nil)
    }

    /// Identifies the current session as a known user.
    /// - Parameter userHash: `hex(HMAC_SHA256(workspace_identity_secret, externalId))`, computed
    ///   by the integrator's **backend**. The workspace secret is NEVER shipped in the app — the
    ///   SDK only ever forwards this precomputed hash.
    @discardableResult
    public func login(externalId: String,
                      userHash: String,
                      email: String? = nil,
                      name: String? = nil) async throws -> Contact {
        guard appId != nil else { throw RelayError.notBooted }
        return try await performBoot(user: BootUser(externalId: externalId, email: email, name: name),
                                     userHash: userHash)
    }

    private func performBoot(user: BootUser?, userHash: String?) async throws -> Contact {
        guard let client, let appId else { throw RelayError.notBooted }
        let resp = try await client.boot(BootRequest(appId: appId, user: user, userHash: userHash))
        await client.setToken(resp.sessionToken)
        contactValue = resp.contact
        configValue = resp.config
        themeValue = hostTheme ?? RelayTheme.from(config: resp.config)
        return resp.contact
    }

    /// Clears the local session and retires this device's push token (best effort).
    /// Call `boot()` again afterwards for a fresh anonymous session.
    public func logout() async {
        if let hex = lastDeviceTokenHex, let client { try? await client.unregisterDevice(token: hex) }
        lastDeviceTokenHex = nil
        contactValue = nil
        await client?.setToken(nil)
    }

    // MARK: - State accessors

    public func contact() -> Contact? { contactValue }
    public func config() -> MessengerConfig? { configValue }
    public func theme() -> RelayTheme { themeValue }
    public func setTheme(_ theme: RelayTheme) { hostTheme = theme; themeValue = theme }

    // MARK: - Conversations

    public func conversations(cursor: String? = nil, limit: Int? = nil) async throws -> Page<Conversation> {
        try await requireClient().conversations(cursor: cursor, limit: limit)
    }

    public func parts(conversationId: String, after: String? = nil,
                      cursor: String? = nil, limit: Int? = nil) async throws -> Page<Part> {
        try await requireClient().parts(conversationId: conversationId, cursor: cursor, after: after, limit: limit)
    }

    public func startConversation(body: String, attachments: [Attachment]? = nil) async throws -> Conversation {
        try await requireClient().createConversation(body: body, attachments: attachments)
    }

    /// Sends a reply. The client attaches a fresh `Idempotency-Key` so retries are safe.
    public func reply(conversationId: String, body: String, attachments: [Attachment]? = nil) async throws -> Part {
        try await requireClient().reply(conversationId: conversationId, body: body, attachments: attachments)
    }

    public func rate(conversationId: String, rating: Int, remark: String? = nil) async throws -> Part {
        try await requireClient().rate(conversationId: conversationId, rating: rating, remark: remark)
    }

    // MARK: - Push

    /// Registers this device's APNs token. The server upserts on the token, so it is safe —
    /// and expected — to call this on every launch and again on every APNs token refresh.
    public func registerForPushNotifications(deviceToken: Data, environment: String = "production") async throws {
        let hex = PushManager.hexToken(from: deviceToken)
        lastDeviceTokenHex = hex
        pushEnvironment = environment
        _ = try await requireClient().registerDevice(
            DeviceRegister(token: hex, appId: bundleId, environment: environment)
        )
    }

    public func unregisterPush() async throws {
        guard let hex = lastDeviceTokenHex else { return }
        try await requireClient().unregisterDevice(token: hex)
        lastDeviceTokenHex = nil
    }

    /// Pulls the deep-link `conversation_id` out of a notification payload on tap. `nonisolated`
    /// so it can be called synchronously from an `AppDelegate`/`UNUserNotificationCenterDelegate`.
    public nonisolated func handleNotification(userInfo: [AnyHashable: Any]) -> String? {
        PushManager.conversationId(from: userInfo)
    }

    // MARK: - Attachments

    /// Uploads a file (presign → PUT to S3) and returns the `Attachment` reference to include
    /// in `reply`/`startConversation`.
    public func uploadAttachment(data: Data, filename: String, contentType: String) async throws -> Attachment {
        let client = try requireClient()
        let presign = try await client.presign(filename: filename, contentType: contentType)
        try await client.putFile(to: presign.uploadUrl, data: data, contentType: contentType)
        return Attachment(key: presign.key, filename: filename, contentType: contentType)
    }

    // MARK: -

    private func requireClient() throws -> RelayClient {
        guard let client else { throw RelayError.notBooted }
        return client
    }
}
