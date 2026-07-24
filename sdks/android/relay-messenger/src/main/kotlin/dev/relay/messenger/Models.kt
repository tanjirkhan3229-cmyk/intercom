package dev.relay.messenger

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

// Wire models. Property names are camelCase; the JSON layer (RelayClient) maps them to the
// contract's snake_case via JsonNamingStrategy.SnakeCase. IDs (usr_/cnv_/msg_/wrk_) are opaque
// strings; ISO8601 timestamps are kept as raw strings in beta (no datetime dependency).

@Serializable
data class BootUser(
    val externalId: String? = null,
    val email: String? = null,
    val name: String? = null,
)

@Serializable
data class BootRequest(
    val appId: String,
    val user: BootUser? = null,
    // Precomputed HMAC from the integrator's backend. The SDK NEVER computes or holds the secret.
    val userHash: String? = null,
    val resumeToken: String? = null,
)

@Serializable
data class Contact(
    val id: String,
    val kind: String,          // "user" | "lead"
    val email: String? = null,
    val name: String? = null,
)

@Serializable
data class MessengerConfig(
    val primaryColor: String? = null,          // "#RRGGBB"
    val launcherPosition: String? = null,      // "left" | "right"
    val greeting: String? = null,
    val expectedReplyTime: String? = null,
    val officeHours: JsonElement? = null,
    val identityVerificationEnabled: Boolean = false,
)

@Serializable
data class BootResponse(
    val sessionToken: String,
    val contact: Contact,
    val config: MessengerConfig,
    val conversations: List<Conversation> = emptyList(),
)

@Serializable
data class Conversation(
    val id: String,
    val contactId: String,
    val channel: String = "chat",
    val state: String,                          // "open" | "snoozed" | "closed"
    val assigneeId: String? = null,
    val teamId: String? = null,
    val priority: Boolean = false,
    val waitingSince: String? = null,
    val snoozedUntil: String? = null,
    val lastPartAt: String? = null,
    val firstContactReplyAt: String? = null,
    val aiStatus: String? = null,
    val createdAt: String,
)

@Serializable
data class Attachment(
    val key: String,
    val filename: String,
    val contentType: String,
)

@Serializable
data class Part(
    val id: String,
    val conversationId: String,
    val authorKind: String,                     // "contact" | "admin" | "ai_agent" | "system"
    val authorId: String? = null,
    val partType: String = "comment",           // "comment" | "note" | "rating" | "assignment" | "state_change"
    val body: String? = null,
    val attachments: List<Attachment> = emptyList(),
    val meta: JsonObject? = null,
    val createdAt: String,
)

@Serializable
data class Page<T>(
    val items: List<T> = emptyList(),
    val nextCursor: String? = null,
)

// --- request bodies ---

@Serializable
data class ReplyRequest(
    val body: String,
    val attachments: List<Attachment>? = null,
)

@Serializable
data class RatingRequest(
    val rating: Int,                            // 1..5
    val remark: String? = null,
)

@Serializable
data class DeviceRegister(
    val platform: String,                       // "ios" | "android"
    val token: String,
    val appId: String? = null,                  // Android package name
    val environment: String = "production",
)

@Serializable
data class DeviceResponse(
    val id: String,
    val platform: String,
    val status: String,
)

@Serializable
data class PresignRequest(
    val filename: String,
    val contentType: String,
)

@Serializable
data class PresignResponse(
    val key: String,
    val uploadUrl: String,
    val method: String = "PUT",
)

@Serializable
data class DownloadUrlResponse(
    val url: String,
)

@Serializable
data class RealtimeToken(
    val token: String,
    val wsUrl: String,
)
