import Foundation

// Codable models mirroring sdks/API_CONTRACT.md (v0). Snake_case ↔ camelCase is handled
// by the encoder/decoder key strategies in RelayClient, so no CodingKeys are needed here.
// ponytail: `office_hours` (Conversation config) and Part.`meta` are omitted — both are
// null/opaque in v0 and JSONDecoder ignores unknown keys. Add typed structs when the
// server defines their shape.

// MARK: - Enums (forward-compatible: unknown server values decode to `.unknown`)

public enum AuthorKind: String, Codable, Sendable {
    case contact, admin, system, unknown
    case aiAgent = "ai_agent"
    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = AuthorKind(rawValue: raw) ?? .unknown
    }
}

public enum PartType: String, Codable, Sendable {
    case comment, note, rating, assignment, unknown
    case stateChange = "state_change"
    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = PartType(rawValue: raw) ?? .unknown
    }
}

// MARK: - Boot / identify

public struct BootUser: Encodable, Sendable {
    public var externalId: String
    public var email: String?
    public var name: String?
    public init(externalId: String, email: String? = nil, name: String? = nil) {
        self.externalId = externalId; self.email = email; self.name = name
    }
}

public struct BootRequest: Encodable, Sendable {
    public var appId: String
    public var user: BootUser?
    public var userHash: String?
    public var resumeToken: String?
    public init(appId: String, user: BootUser? = nil, userHash: String? = nil, resumeToken: String? = nil) {
        self.appId = appId; self.user = user; self.userHash = userHash; self.resumeToken = resumeToken
    }
}

public struct Contact: Codable, Sendable, Identifiable {
    public let id: String
    public let kind: String   // "user" | "lead"
    public let email: String?
    public let name: String?
}

public struct MessengerConfig: Codable, Sendable {
    public let primaryColor: String?
    public let launcherPosition: String?   // "left" | "right"
    public let greeting: String?
    public let expectedReplyTime: String?
    public let identityVerificationEnabled: Bool
    public init(primaryColor: String?, launcherPosition: String?, greeting: String?,
                expectedReplyTime: String?, identityVerificationEnabled: Bool) {
        self.primaryColor = primaryColor; self.launcherPosition = launcherPosition
        self.greeting = greeting; self.expectedReplyTime = expectedReplyTime
        self.identityVerificationEnabled = identityVerificationEnabled
    }
}

public struct BootResponse: Codable, Sendable {
    public let sessionToken: String
    public let contact: Contact
    public let config: MessengerConfig
    public let conversations: [Conversation]
}

// MARK: - Conversations & parts

public struct Conversation: Codable, Sendable, Identifiable {
    public let id: String
    public let contactId: String
    public let channel: String
    public let state: String          // open | snoozed | closed
    public let assigneeId: String?
    public let teamId: String?
    public let priority: Bool
    public let waitingSince: Date?
    public let snoozedUntil: Date?
    public let lastPartAt: Date?
    public let firstContactReplyAt: Date?
    public let aiStatus: String?
    public let createdAt: Date
}

public struct Attachment: Codable, Sendable, Hashable {
    public let key: String
    public let filename: String
    public let contentType: String
    public init(key: String, filename: String, contentType: String) {
        self.key = key; self.filename = filename; self.contentType = contentType
    }
}

public struct Part: Codable, Sendable, Identifiable {
    public let id: String
    public let conversationId: String
    public let authorKind: AuthorKind
    public let authorId: String?
    public let partType: PartType
    public let body: String?
    public let attachments: [Attachment]?   // optional for parts (e.g. ratings) that carry none
    public let createdAt: Date
}

// MARK: - Pagination (keyset)

public struct Page<T: Codable & Sendable>: Codable, Sendable {
    public let items: [T]
    public let nextCursor: String?
}

// MARK: - Devices & uploads

public struct DeviceRegister: Encodable, Sendable {
    public var platform: String        // "ios"
    public var token: String           // APNs token, hex
    public var appId: String?          // bundle id
    public var environment: String     // "production" | "sandbox"
    public init(platform: String = "ios", token: String, appId: String? = nil, environment: String = "production") {
        self.platform = platform; self.token = token; self.appId = appId; self.environment = environment
    }
}

public struct DeviceRegisterResponse: Codable, Sendable {
    public let id: String
    public let platform: String
    public let status: String
}

public struct PresignResponse: Codable, Sendable {
    public let key: String
    public let uploadUrl: String
    public let method: String   // "PUT"
}

// MARK: - Request bodies (internal — integrators go through the Relay facade)

struct MessageRequest: Encodable {
    let body: String
    let attachments: [Attachment]?
}

struct RatingRequest: Encodable {
    let rating: Int
    let remark: String?
}

struct PresignRequest: Encodable {
    let filename: String
    let contentType: String
}
