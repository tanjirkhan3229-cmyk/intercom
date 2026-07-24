package dev.relay.messenger.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import dev.relay.messenger.Conversation
import dev.relay.messenger.Relay
import dev.relay.messenger.RelayTheme

/** Native conversation list. Tap opens the thread; FAB requests a new conversation. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ConversationListScreen(
    onOpen: (String) -> Unit,
    onNewConversation: () -> Unit,
) = RelayTheme {
    var conversations by remember { mutableStateOf<List<Conversation>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(Unit) {
        runCatching { Relay.conversations(limit = 50) }
            .onSuccess { conversations = it.items }
            .onFailure { error = it.message }
    }

    Scaffold(
        topBar = { TopAppBar(title = { Text("Messages") }) },
        floatingActionButton = {
            FloatingActionButton(onClick = onNewConversation) { Text("+") }
        },
    ) { pad ->
        Column(Modifier.fillMaxSize().padding(pad)) {
            error?.let { Text("Couldn't load: $it", Modifier.padding(16.dp)) }
            LazyColumn {
                items(conversations, key = { it.id }) { c ->
                    Column(
                        Modifier
                            .fillMaxWidth()
                            .clickable { onOpen(c.id) }
                            .padding(16.dp),
                    ) {
                        Text(c.id, fontWeight = FontWeight.SemiBold)
                        Text(
                            "${c.state} · updated ${c.lastPartAt ?: c.createdAt}",
                        )
                    }
                    HorizontalDivider()
                }
            }
        }
    }
}
