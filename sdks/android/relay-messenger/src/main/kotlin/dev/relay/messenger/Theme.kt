package dev.relay.messenger

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color

/** Theming hooks. Seeded from the boot [MessengerConfig]; every field is overridable by the host. */
data class RelayColors(
    val primary: Color = Color(0xFF3B82F6),
    val onPrimary: Color = Color.White,
    val incomingBubble: Color = Color(0xFFEFF1F4),
    val onIncomingBubble: Color = Color(0xFF14181F),
)

enum class LauncherPosition { LEFT, RIGHT }

val LocalRelayColors = staticCompositionLocalOf { RelayColors() }

object RelayTheme {
    val colors: RelayColors
        @Composable get() = LocalRelayColors.current
}

/** Wrap messenger UI in this. Pass [colors] to override anything derived from [config]. */
@Composable
fun RelayTheme(
    config: MessengerConfig? = Relay.config,
    colors: RelayColors? = null,
    content: @Composable () -> Unit,
) {
    val resolved = colors ?: RelayColors(
        primary = parseHexColor(config?.primaryColor) ?: RelayColors().primary,
    )
    MaterialTheme(
        colorScheme = lightColorScheme(primary = resolved.primary, onPrimary = resolved.onPrimary),
    ) {
        CompositionLocalProvider(LocalRelayColors provides resolved, content = content)
    }
}

/** Launcher alignment from config (host decides where to place its FAB). */
fun launcherPosition(config: MessengerConfig? = Relay.config): LauncherPosition =
    if (config?.launcherPosition == "left") LauncherPosition.LEFT else LauncherPosition.RIGHT

internal fun parseHexColor(hex: String?): Color? =
    hex?.let { runCatching { Color(android.graphics.Color.parseColor(it)) }.getOrNull() }
