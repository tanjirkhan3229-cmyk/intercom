package dev.relay.messenger

import android.content.Context
import android.content.Intent
import java.util.UUID

/**
 * Public entry point for the Relay messenger SDK. Singleton; all network calls are `suspend`.
 *
 * Lifecycle: [boot] once at launch (anonymous "lead"), optionally [login] to identify the user
 * with a backend-computed `user_hash`. The SDK never holds the workspace identity secret.
 *
 * Session token + last identity are persisted so a cold-started [ReplyReceiver] (direct reply
 * from a notification) can re-attach without a full app launch.
 */
object Relay {

    const val DEFAULT_BASE_URL = "https://api.relay.dev"

    /** Deep-link + push payload keys, matching the API contract's `data` payload. */
    const val KEY_CONVERSATION_ID = "conversation_id"
    const val KEY_PUSH_TYPE = "type"
    const val PUSH_TYPE_REPLY = "conversation.reply"

    private const val PREFS = "relay_messenger"
    private const val K_APP_ID = "app_id"
    private const val K_BASE = "base_url"
    private const val K_TOKEN = "session_token"
    private const val K_EXT_ID = "external_id"
    private const val K_HASH = "user_hash"
    private const val K_EMAIL = "email"
    private const val K_NAME = "name"
    private const val K_PUSH = "push_token"

    private lateinit var appContext: Context
    private lateinit var client: RelayClient

    /** Last boot config (theming source of truth) and resolved contact. */
    @Volatile var config: MessengerConfig? = null; private set
    @Volatile var contact: Contact? = null; private set

    val isBooted: Boolean get() = ::client.isInitialized && client.sessionToken != null

    private fun prefs() = appContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    // --- boot / identity ---

    /** Boot as an anonymous lead. Call once at app start with your public `wrk_…` app id. */
    suspend fun boot(
        context: Context,
        appId: String,
        baseUrl: String = DEFAULT_BASE_URL,
    ): BootResponse {
        appContext = context.applicationContext
        prefs().edit().putString(K_APP_ID, appId).putString(K_BASE, baseUrl).apply()
        client = RelayClient(baseUrl)
        return doBoot(externalId = null, userHash = null, email = null, name = null)
    }

    /**
     * Identify the current user. [userHash] is `HMAC_SHA256(secret, externalId)` computed by the
     * integrator's backend — the secret is never in the app.
     */
    suspend fun login(
        externalId: String,
        userHash: String,
        email: String? = null,
        name: String? = null,
    ): BootResponse {
        requireBooted()
        prefs().edit()
            .putString(K_EXT_ID, externalId)
            .putString(K_HASH, userHash)
            .putString(K_EMAIL, email)
            .putString(K_NAME, name)
            .apply()
        return doBoot(externalId, userHash, email, name)
    }

    /** Drop the identified session; unregisters the push token. Keeps app id + base url. */
    suspend fun logout() {
        requireBooted()
        prefs().getString(K_PUSH, null)?.let { runCatching { client.unregisterDevice(it) } }
        prefs().edit()
            .remove(K_TOKEN).remove(K_EXT_ID).remove(K_HASH)
            .remove(K_EMAIL).remove(K_NAME).remove(K_PUSH)
            .apply()
        client.sessionToken = null
        contact = null
    }

    private suspend fun doBoot(
        externalId: String?, userHash: String?, email: String?, name: String?,
    ): BootResponse {
        val user = if (externalId != null || email != null || name != null)
            BootUser(externalId, email, name) else null
        val resp = client.boot(
            BootRequest(appId = prefs().getString(K_APP_ID, null)!!, user = user, userHash = userHash),
        )
        client.sessionToken = resp.sessionToken
        contact = resp.contact
        config = resp.config
        prefs().edit().putString(K_TOKEN, resp.sessionToken).apply()
        // Re-register a cached push token against the (possibly new) contact.
        prefs().getString(K_PUSH, null)?.let { runCatching { registerPushToken(it) } }
        return resp
    }

    // --- conversations ---

    suspend fun conversations(cursor: String? = null, limit: Int? = null): Page<Conversation> =
        client.listConversations(cursor, limit)

    suspend fun parts(
        conversationId: String, after: String? = null, cursor: String? = null, limit: Int? = null,
    ): Page<Part> = client.listParts(conversationId, cursor, after, limit)

    suspend fun startConversation(body: String, attachments: List<Attachment>? = null): Conversation =
        client.createConversation(body, attachments)

    suspend fun reply(
        conversationId: String,
        body: String,
        attachments: List<Attachment>? = null,
        idempotencyKey: String = UUID.randomUUID().toString(),
    ): Part = client.reply(conversationId, body, attachments, idempotencyKey)

    suspend fun rate(conversationId: String, rating: Int, remark: String? = null): Part =
        client.rate(conversationId, rating, remark)

    // --- attachments ---

    /** Presign → PUT bytes → return the [Attachment] reference to include in a reply. */
    suspend fun uploadAttachment(filename: String, contentType: String, bytes: ByteArray): Attachment {
        val p = client.presign(filename, contentType)
        client.putBytes(p.uploadUrl, bytes, contentType)
        return Attachment(key = p.key, filename = filename, contentType = contentType)
    }

    // --- push ---

    /** Register/refresh the FCM token. Server upserts on the token, so this is safe every launch. */
    suspend fun registerPushToken(token: String) {
        requireBooted()
        prefs().edit().putString(K_PUSH, token).apply()
        client.registerDevice(
            DeviceRegister(platform = "android", token = token, appId = appContext.packageName),
        )
    }

    suspend fun unregisterPush(token: String? = null) {
        requireBooted()
        val t = token ?: prefs().getString(K_PUSH, null) ?: return
        client.unregisterDevice(t)
        prefs().edit().remove(K_PUSH).apply()
    }

    // --- push handling / deep link ---

    /**
     * Handle a Relay push `data` payload. Returns true if it was a Relay message (and a
     * notification was posted). Call from your own FirebaseMessagingService if you don't use
     * [RelayMessagingService].
     */
    fun handleRemoteMessage(context: Context, data: Map<String, String>): Boolean {
        val conversationId = data[KEY_CONVERSATION_ID] ?: return false
        postReplyNotification(context, conversationId, body = data["body"])
        return true
    }

    /** Extract the conversation id from a deep-link intent (notification tap). */
    fun conversationIdFrom(intent: Intent?): String? =
        intent?.getStringExtra(KEY_CONVERSATION_ID)

    /**
     * Send a reply on behalf of a (possibly cold-started) process, e.g. from [ReplyReceiver].
     * Re-attaches persisted state and re-boots from stored identity if the token is gone.
     */
    suspend fun replyFromNotification(context: Context, conversationId: String, text: String): Part {
        attach(context)
        if (client.sessionToken == null) reBootFromStored()
        return reply(conversationId, text)
    }

    /** Rebuild the client from persisted prefs (base url + token). Safe from any process. */
    @Synchronized
    fun attach(context: Context) {
        if (!::appContext.isInitialized) appContext = context.applicationContext
        if (!::client.isInitialized) {
            client = RelayClient(prefs().getString(K_BASE, DEFAULT_BASE_URL)!!)
        }
        client.sessionToken = prefs().getString(K_TOKEN, null)
    }

    private suspend fun reBootFromStored() {
        val extId = prefs().getString(K_EXT_ID, null)
        val hash = prefs().getString(K_HASH, null)
        doBoot(extId, hash, prefs().getString(K_EMAIL, null), prefs().getString(K_NAME, null))
    }

    private fun requireBooted() = check(::client.isInitialized) { "Relay.boot(...) must be called first" }
}
