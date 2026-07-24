package dev.relay.sample

import android.Manifest
import android.content.Intent
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import com.google.firebase.messaging.FirebaseMessaging
import dev.relay.messenger.Relay
import dev.relay.messenger.ui.ConversationListScreen
import dev.relay.messenger.ui.ConversationScreen
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

// Replace with your workspace's public app id (a wrk_… id, safe to ship in the binary).
private const val RELAY_APP_ID = "wrk_REPLACE_ME"

class MainActivity : ComponentActivity() {

    // Deep-link target from a notification tap; also updated by onNewIntent.
    private var deepLink by mutableStateOf<String?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        deepLink = Relay.conversationIdFrom(intent)

        setContent {
            MaterialTheme {
                Surface {
                    var booted by remember { mutableStateOf(false) }
                    var error by remember { mutableStateOf<String?>(null) }
                    var current by remember { mutableStateOf(deepLink) }
                    val scope = rememberCoroutineScope()

                    // Android 13+ notification permission.
                    val perm = rememberLauncherForActivityResult(
                        ActivityResultContracts.RequestPermission(),
                    ) { /* ignored: notifications simply won't show if denied */ }

                    LaunchedEffect(Unit) {
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                            perm.launch(Manifest.permission.POST_NOTIFICATIONS)
                        }
                        runCatching {
                            Relay.boot(applicationContext, RELAY_APP_ID)
                            // For an identified user, your backend computes user_hash:
                            //   Relay.login(externalId = "u_42", userHash = "<hex from backend>")
                            registerPushToken()
                        }.onSuccess { booted = true }
                            .onFailure { error = it.message }
                    }

                    // Route deep-link taps that arrive while the activity is alive.
                    LaunchedEffect(deepLink) { deepLink?.let { current = it } }

                    when {
                        error != null -> Text("Boot failed: $error")
                        !booted -> Text("Connecting…")
                        current != null -> ConversationScreen(
                            conversationId = current!!,
                            onBack = { current = null },
                        )
                        else -> ConversationListScreen(
                            onOpen = { current = it },
                            onNewConversation = {
                                scope.launch {
                                    runCatching { Relay.startConversation("Hi, I need some help.") }
                                        .onSuccess { current = it.id }
                                }
                            },
                        )
                    }
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        deepLink = Relay.conversationIdFrom(intent)
    }

    /** Fetch the current FCM token and register it. The SDK handles rotation via onNewToken. */
    private fun registerPushToken() {
        FirebaseMessaging.getInstance().token.addOnSuccessListener { token ->
            CoroutineScope(Dispatchers.IO).launch {
                runCatching { Relay.registerPushToken(token) }
            }
        }
    }
}
