package dev.relay.messenger

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNamingStrategy
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.util.concurrent.TimeUnit

/** Thrown for any non-2xx response (or transport failure). */
class RelayException(
    val status: Int,
    message: String,
    val errorCode: String? = null,
    cause: Throwable? = null,
) : Exception(message, cause)

/**
 * Thin OkHttp wrapper over the Relay widget API. All calls are `suspend` and run on Dispatchers.IO.
 * Holds only the boot session token (bearer) — never the workspace identity secret.
 */
internal class RelayClient(
    baseUrl: String,
    private val http: OkHttpClient = defaultHttp(),
) {
    private val base = baseUrl.trimEnd('/').toHttpUrl()

    @OptIn(kotlinx.serialization.ExperimentalSerializationApi::class)
    private val json = Json {
        ignoreUnknownKeys = true
        explicitNulls = false          // omit nulls on encode (e.g. optional boot user)
        encodeDefaults = false
        namingStrategy = JsonNamingStrategy.SnakeCase
    }

    @Volatile
    var sessionToken: String? = null

    // --- endpoints ---

    suspend fun boot(req: BootRequest): BootResponse =
        post("v0/widget/boot", req, authed = false)

    suspend fun listConversations(cursor: String? = null, limit: Int? = null): Page<Conversation> =
        get("v0/widget/conversations") {
            cursor?.let { addQueryParameter("cursor", it) }
            limit?.let { addQueryParameter("limit", it.toString()) }
        }

    suspend fun createConversation(body: String, attachments: List<Attachment>? = null): Conversation =
        post("v0/widget/conversations", ReplyRequest(body, attachments))

    suspend fun listParts(
        conversationId: String,
        cursor: String? = null,
        after: String? = null,
        limit: Int? = null,
    ): Page<Part> =
        get("v0/widget/conversations/$conversationId/parts") {
            cursor?.let { addQueryParameter("cursor", it) }
            after?.let { addQueryParameter("after", it) }
            limit?.let { addQueryParameter("limit", it.toString()) }
        }

    suspend fun reply(
        conversationId: String,
        body: String,
        attachments: List<Attachment>? = null,
        idempotencyKey: String,
    ): Part =
        post(
            "v0/widget/conversations/$conversationId/reply",
            ReplyRequest(body, attachments),
            headers = mapOf("Idempotency-Key" to idempotencyKey),
        )

    suspend fun rate(conversationId: String, rating: Int, remark: String? = null): Part =
        post("v0/widget/conversations/$conversationId/rating", RatingRequest(rating, remark))

    suspend fun registerDevice(reg: DeviceRegister): DeviceResponse =
        post("v0/widget/devices", reg)

    suspend fun unregisterDevice(token: String) {
        val url = base.newBuilder().addPathSegments("v0/widget/devices")
            .addQueryParameter("token", token).build()
        exec(authedBuilder().url(url).delete().build()).close()
    }

    suspend fun presign(filename: String, contentType: String): PresignResponse =
        post("v0/widget/uploads/presign", PresignRequest(filename, contentType))

    suspend fun downloadUrl(key: String): String =
        get<DownloadUrlResponse>("v0/widget/uploads/download-url") {
            addQueryParameter("key", key)
        }.url

    /** Beta uses polling; this token is here for the future Centrifugo websocket upgrade. */
    suspend fun realtimeToken(conversationId: String): RealtimeToken =
        post<Unit, RealtimeToken>("v0/widget/conversations/$conversationId/realtime-token", body = null)

    /** Step 2 of upload: PUT raw bytes to the presigned S3 URL with the same content type. */
    suspend fun putBytes(uploadUrl: String, bytes: ByteArray, contentType: String) {
        val req = Request.Builder()
            .url(uploadUrl)
            .put(bytes.toRequestBody(contentType.toMediaType()))
            .build()
        exec(req).close()
    }

    // --- plumbing ---

    private fun authedBuilder(): Request.Builder {
        val b = Request.Builder()
        sessionToken?.let { b.header("Authorization", "Bearer $it") }
        return b
    }

    private suspend inline fun <reified T> get(
        path: String,
        query: okhttp3.HttpUrl.Builder.() -> Unit = {},
    ): T {
        val url = base.newBuilder().addPathSegments(path).apply(query).build()
        val text = exec(authedBuilder().url(url).get().build()).use { it.body!!.string() }
        return json.decodeFromString(text)
    }

    private suspend inline fun <reified Req, reified Res> post(
        path: String,
        body: Req?,
        authed: Boolean = true,
        headers: Map<String, String> = emptyMap(),
    ): Res {
        val url = base.newBuilder().addPathSegments(path).build()
        val rb: RequestBody = (body?.let { json.encodeToString(it) } ?: "{}")
            .toRequestBody(JSON_MEDIA)
        val builder = (if (authed) authedBuilder() else Request.Builder()).url(url).post(rb)
        headers.forEach { (k, v) -> builder.header(k, v) }
        val text = exec(builder.build()).use { it.body!!.string() }
        return json.decodeFromString(text)
    }

    private suspend fun exec(request: Request): Response = withContext(Dispatchers.IO) {
        val resp = try {
            http.newCall(request).execute()
        } catch (e: Exception) {
            throw RelayException(0, "network error: ${e.message}", cause = e)
        }
        if (!resp.isSuccessful) {
            val errText = resp.body?.string().orEmpty()
            resp.close()
            throw RelayException(resp.code, errText.ifBlank { "HTTP ${resp.code}" }, parseErrorCode(errText))
        }
        resp
    }

    private fun parseErrorCode(errBody: String): String? = runCatching {
        json.parseToJsonElement(errBody).let { el ->
            (el as? kotlinx.serialization.json.JsonObject)
                ?.get("error")?.let { it as? kotlinx.serialization.json.JsonObject }
                ?.get("code")?.let { (it as? kotlinx.serialization.json.JsonPrimitive)?.content }
        }
    }.getOrNull()

    companion object {
        private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()

        fun defaultHttp(): OkHttpClient = OkHttpClient.Builder()
            .callTimeout(30, TimeUnit.SECONDS)
            .connectTimeout(10, TimeUnit.SECONDS)
            .build()
    }
}
