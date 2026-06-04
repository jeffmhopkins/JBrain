package com.jbrain.watch.theme

import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.wear.compose.material.Colors
import androidx.wear.compose.material.MaterialTheme

private val JBrainColors = Colors(
    primary = Color(0xFF4C8DFF),
    onPrimary = Color(0xFF06121F),
    background = Color(0xFF000000),
    onBackground = Color(0xFFE6E6E6),
    surface = Color(0xFF15171B),
    onSurface = Color(0xFFE6E6E6),
)

@Composable
fun JBrainWatchTheme(content: @Composable () -> Unit) {
    MaterialTheme(colors = JBrainColors, content = content)
}
