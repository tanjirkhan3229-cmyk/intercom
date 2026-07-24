package dev.relay.messenger.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import dev.relay.messenger.Part
import dev.relay.messenger.Relay
import dev.relay.messenger.RelayTheme
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

private const val POLL_MS = 3_000L // ponytail: beta polls; realtime-token + Centrifugo ws is the upgrade path.

/** Native thread view: message bubbles + composer. Polls `parts?after=` for new parts. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ConversationScreen(
    conversationId: String,
    onBack: () -> Unit = {},
) = RelayTheme {
    val parts = remember { mutableStateListOf<Part>() }
    var input by remember { mutableStateOf("") }
    var sending by remember { mutableStateOf(false) }
    val listState = rememberLazyListState()
    val scope = rememberCoroutineScope()

    fun merge(incoming: List<Part>) {
        val known = parts.mapTo(HashSet()) { it.id }
        parts.addAll(incoming.filter { it.id !in known })
    }

    // Initial load + poll loop for new parts.
    LaunchedEffect(conversationId) {
        runCatching { Relay.parts(conversationId, limit = 100) }
            .onSuccess { merge(it.items) }
        while (true) {
            delay(POLL_MS)
            val after = parts.lastOrNull()?.id ?: continue // `after` = last seen part id
            runCatching { Relay.parts(conversationId, after = after) }
                .onSuccess { merge(it.items) }
        }
    }

    LaunchedEffect(parts.size) {
        if (parts.isNotEmpty()) listState.animateScrollToItem(parts.lastIndex)
    }

    Scaffold(topBar = { TopAppBar(title = { Text("Conversation") }) }) { pad ->
        Column(Modifier.fillMaxSize().padding(pad)) {
            LazyColumn(
                state = listState,
                modifier = Modifier.weight(1f).fillMaxWidth(),
                contentPadding = androidx.compose.foundation.layout.PaddingValues(12.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                items(parts.filter { !it.body.isNullOrBlank() }, key = { it.id }) { p ->
                    MessageBubble(p)
                }
            }
            Row(
                Modifier.fillMaxWidth().padding(8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                OutlinedTextField(
                    value = input,
                    onValueChange = { input = it },
                    modifier = Modifier.weight(1f),
                    placeholder = { Text("Message…") },
                    enabled = !sending,
                )
                TextButton(
                    enabled = input.isNotBlank() && !sending,
                    onClick = {
                        val text = input.trim()
                        input = ""
                        sending = true
                        scope.launch {
                            runCatching { Relay.reply(conversationId, text) }
                                .onSuccess { merge(listOf(it)) }
                            sending = false
                        }
                    },
                ) { Text("Send") }
            }
        }
    }
}

@Composable
private fun MessageBubble(part: Part) {
    val mine = part.authorKind == "contact"
    val colors = RelayTheme.colors
    Box(Modifier.fillMaxWidth()) {
        Column(
            Modifier
                .align(if (mine) Alignment.CenterEnd else Alignment.CenterStart)
                .widthIn(max = 280.dp)
                .clip(RoundedCornerShape(14.dp))
                .background(if (mine) colors.primary else colors.incomingBubble)
                .padding(horizontal = 12.dp, vertical = 8.dp),
        ) {
            Text(
                text = part.body.orEmpty(),
                color = if (mine) colors.onPrimary else colors.onIncomingBubble,
            )
        }
    }
}
