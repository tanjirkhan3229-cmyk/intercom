package dev.relay.messenger

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.RemoteInput
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

internal const val CHANNEL_ID = "relay_messages"
internal const val REMOTE_INPUT_KEY = "relay_reply_text"
private const val ACTION_REPLY = "dev.relay.messenger.ACTION_REPLY"

/**
 * FCM entry point. Registered in the library manifest. Handles token rotation and inbound
 * `conversation.reply` pushes by posting a deep-linking, directly-repliable notification.
 */
class RelayMessagingService : FirebaseMessagingService() {

    override fun onNewToken(token: String) {
        // Token rotated by the OS — re-register. Server upserts on the token.
        Relay.attach(applicationContext)
        scope.launch { runCatching { Relay.registerPushToken(token) } }
    }

    override fun onMessageReceived(message: RemoteMessage) {
        // data payload: { conversation_id, type, body? }  (see API_CONTRACT.md §Push)
        Relay.handleRemoteMessage(applicationContext, message.data)
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
}

/** Builds + posts the reply notification: tap deep-links to the thread, action inlines a reply. */
internal fun postReplyNotification(context: Context, conversationId: String, body: String?) {
    ensureChannel(context)
    val notifId = conversationId.hashCode()

    // Tap → open the host's launcher activity with the conversation id extra.
    val contentIntent = context.packageManager
        .getLaunchIntentForPackage(context.packageName)
        ?.apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
            putExtra(Relay.KEY_CONVERSATION_ID, conversationId)
        }
    val contentPending = PendingIntent.getActivity(
        context, notifId, contentIntent ?: Intent(),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
    )

    // Direct-reply action (Android RemoteInput). The broadcast PendingIntent must be MUTABLE so
    // the system can inject the typed text.
    val replyIntent = Intent(context, ReplyReceiver::class.java).apply {
        action = ACTION_REPLY
        putExtra(Relay.KEY_CONVERSATION_ID, conversationId)
    }
    val replyPending = PendingIntent.getBroadcast(
        context, notifId, replyIntent,
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE,
    )
    val remoteInput = RemoteInput.Builder(REMOTE_INPUT_KEY).setLabel("Reply").build()
    val replyAction = NotificationCompat.Action.Builder(
        android.R.drawable.ic_menu_send, "Reply", replyPending,
    ).addRemoteInput(remoteInput).setAllowGeneratedReplies(true).build()

    val notif = NotificationCompat.Builder(context, CHANNEL_ID)
        .setSmallIcon(android.R.drawable.stat_notify_chat)
        .setContentTitle("New message")
        .setContentText(body ?: "You have a new reply")
        .setAutoCancel(true)
        .setContentIntent(contentPending)
        .addAction(replyAction)
        .build()

    runCatching { NotificationManagerCompat.from(context).notify(notifId, notif) }
}

private fun ensureChannel(context: Context) {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
    val mgr = context.getSystemService(NotificationManager::class.java)
    if (mgr.getNotificationChannel(CHANNEL_ID) == null) {
        mgr.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Messages", NotificationManager.IMPORTANCE_HIGH),
        )
    }
}

/** Receives the RemoteInput direct-reply text and sends it via the SDK. */
class ReplyReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val conversationId = intent.getStringExtra(Relay.KEY_CONVERSATION_ID) ?: return
        val text = RemoteInput.getResultsFromIntent(intent)
            ?.getCharSequence(REMOTE_INPUT_KEY)?.toString()?.trim()
        if (text.isNullOrEmpty()) return

        val notifId = conversationId.hashCode()
        val pending = goAsync() // keep the process alive for the async send
        CoroutineScope(Dispatchers.IO).launch {
            try {
                Relay.replyFromNotification(context, conversationId, text)
                // Confirm the send in-place (no reply action, shows what was sent).
                val sent = NotificationCompat.Builder(context, CHANNEL_ID)
                    .setSmallIcon(android.R.drawable.stat_notify_chat)
                    .setContentTitle("Sent")
                    .setContentText(text)
                    .setAutoCancel(true)
                    .build()
                runCatching { NotificationManagerCompat.from(context).notify(notifId, sent) }
            } catch (_: Exception) {
                // Leave the original notification so the user can retry from the app.
            } finally {
                pending.finish()
            }
        }
    }
}
