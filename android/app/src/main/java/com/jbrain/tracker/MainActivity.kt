package com.jbrain.tracker

import android.Manifest
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay

// JBrain's flat-dark palette (mirrors the PWA's CSS variables).
private val JBrainDark = darkColorScheme(
    primary = Color(0xFF7F9AA6),
    onPrimary = Color(0xFF0E1416),
    secondary = Color(0xFF7F9AA6),
    background = Color(0xFF111315),
    onBackground = Color(0xFFD8DCDE),
    surface = Color(0xFF161A1C),
    onSurface = Color(0xFFD8DCDE),
    surfaceVariant = Color(0xFF1C2123),
    onSurfaceVariant = Color(0xFF828A8E),
    outline = Color(0xFF2A2F31),
    error = Color(0xFFC08585),
)

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme(colorScheme = JBrainDark) {
                Scaffold { pad -> TrackerScreen(Modifier.padding(pad)) }
            }
        }
    }
}

@Composable
private fun TrackerScreen(modifier: Modifier = Modifier) {
    val ctx = LocalContext.current
    var serverUrl by remember { mutableStateOf(Settings.serverUrl(ctx)) }
    var key by remember { mutableStateOf(Settings.key(ctx)) }
    var name by remember { mutableStateOf(Settings.name(ctx)) }
    var enabled by remember { mutableStateOf(Settings.enabled(ctx)) }
    var status by remember { mutableStateOf("") }
    var queued by remember { mutableStateOf(FixQueue.size(ctx)) }

    // Keep the queued-fixes count live so you can watch the buffer drain after a sync.
    LaunchedEffect(Unit) {
        while (true) { queued = FixQueue.size(ctx); delay(3000) }
    }

    fun turnOn() {
        Tracking.setEnabled(ctx, true)
        enabled = true
        status = if (Tracking.hasBackground(ctx)) "Tracking — runs in the background."
        else "Tracking. For when the app is closed, set location to “Allow all the time” in system settings."
    }

    // Background location must be requested AFTER foreground is granted (a second prompt
    // on Android 11+, or the system settings page). We chain the two launchers.
    val bgLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { _ -> turnOn() }

    val fgLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { result ->
        val fine = result[Manifest.permission.ACCESS_FINE_LOCATION] == true ||
            result[Manifest.permission.ACCESS_COARSE_LOCATION] == true
        if (!fine) {
            enabled = false
            status = "Location permission denied — can't track without it."
        } else if (!Tracking.hasBackground(ctx) && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            bgLauncher.launch(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
        } else {
            turnOn()
        }
    }

    fun requestEnable() {
        if (!Settings.isConfigured(ctx)) {
            status = "Set the server URL and access key first."
            enabled = false
            return
        }
        if (!Tracking.hasForeground(ctx)) {
            val perms = mutableListOf(
                Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION,
            )
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                perms.add(Manifest.permission.POST_NOTIFICATIONS)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                perms.add(Manifest.permission.ACTIVITY_RECOGNITION)   // smart polling (sleep GPS when still)
            }
            fgLauncher.launch(perms.toTypedArray())
        } else if (!Tracking.hasBackground(ctx) && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            bgLauncher.launch(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
        } else {
            turnOn()
        }
    }

    Column(
        modifier = modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Row(verticalAlignment = Alignment.Bottom) {
            Text("JBrain", style = MaterialTheme.typography.headlineSmall)
            Text(".", style = MaterialTheme.typography.headlineSmall, color = MaterialTheme.colorScheme.primary)
            Text("  Tracker", style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Text(
            "Logs this device's location trail to your JBrain in the background — smart polling sleeps the GPS while you're still.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        OutlinedTextField(
            value = name, onValueChange = { name = it; Settings.setName(ctx, it) },
            label = { Text("Name (this device)") }, singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = serverUrl, onValueChange = { serverUrl = it; Settings.setServerUrl(ctx, it) },
            label = { Text("Server URL") }, singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = key, onValueChange = { key = it; Settings.setKey(ctx, it) },
            label = { Text("Access key") }, singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            modifier = Modifier.fillMaxWidth(),
        )

        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Text("Track my location", style = MaterialTheme.typography.titleMedium)
            Switch(
                checked = enabled,
                onCheckedChange = { on ->
                    if (on) { enabled = true; requestEnable() }
                    else { Tracking.setEnabled(ctx, false); enabled = false; status = "Stopped." }
                },
            )
        }

        if (status.isNotEmpty()) {
            Text(status, style = MaterialTheme.typography.bodySmall)
        }
        if (queued > 0) {
            Text("$queued fix(es) queued to send.", style = MaterialTheme.typography.bodySmall)
        }

        Spacer(Modifier.height(8.dp))
        Text(
            "Tip: also allow unrestricted battery for this app so Android doesn't pause it.",
            style = MaterialTheme.typography.bodySmall,
            textAlign = TextAlign.Start,
        )

        if (!Tracking.hasBackground(ctx)) {
            Button(onClick = { requestEnable() }, modifier = Modifier.fillMaxWidth()) {
                Text("Grant location permission")
            }
        }
    }
}
