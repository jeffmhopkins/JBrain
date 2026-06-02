package com.jbrain.watch

import android.Manifest
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.speech.RecognizerIntent
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts.RequestMultiplePermissions
import androidx.activity.result.contract.ActivityResultContracts.RequestPermission
import androidx.activity.result.contract.ActivityResultContracts.StartActivityForResult
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.wear.compose.material.Button
import androidx.wear.compose.material.Icon
import androidx.wear.compose.material.MaterialTheme
import androidx.wear.compose.material.Scaffold
import androidx.wear.compose.material.Text
import androidx.wear.compose.material.TimeText
import com.jbrain.watch.theme.JBrainWatchTheme
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/** Extra set by the Tile so launching from the watch face starts dictation instantly. */
const val EXTRA_AUTO_START = "auto_start"

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val autoStart = intent?.getBooleanExtra(EXTRA_AUTO_START, false) == true
        setContent {
            JBrainWatchTheme {
                CaptureScreen(autoStart = autoStart)
            }
        }
    }
}

private enum class Phase { IDLE, SAVING, SAVED, OFFLINE, ERROR }

@Composable
private fun CaptureScreen(autoStart: Boolean) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var phase by remember { mutableStateOf(Phase.IDLE) }

    val speechLauncher = rememberLauncherForActivityResult(StartActivityForResult()) { result ->
        val spoken = result.data
            ?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
            ?.firstOrNull()
            ?.trim()
        if (spoken.isNullOrEmpty()) {
            phase = Phase.IDLE
            return@rememberLauncherForActivityResult
        }
        phase = Phase.SAVING
        scope.launch {
            if (NoteClient.createEntry(spoken)) {
                vibrate(context)
                phase = Phase.SAVED
            } else {
                NoteQueue.enqueue(context, spoken)
                phase = Phase.OFFLINE
            }
            delay(1500)
            phase = Phase.IDLE
        }
    }

    fun startListening() {
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_PROMPT, context.getString(R.string.speak_prompt))
        }
        try {
            speechLauncher.launch(intent)
        } catch (e: ActivityNotFoundException) {
            phase = Phase.ERROR
        }
    }

    // Background location trail toggle. Background location needs a two-step grant
    // (foreground first, then "Allow all the time" separately on Android 10+).
    var tracking by remember { mutableStateOf(Tracking.isEnabled(context)) }
    val bgLauncher = rememberLauncherForActivityResult(RequestPermission()) { _ ->
        if (Tracking.hasBackground(context)) { Tracking.setEnabled(context, true); tracking = true }
    }
    val fgLauncher = rememberLauncherForActivityResult(RequestMultiplePermissions()) { _ ->
        if (!Tracking.hasForeground(context)) return@rememberLauncherForActivityResult
        if (Tracking.hasBackground(context)) { Tracking.setEnabled(context, true); tracking = true }
        else bgLauncher.launch(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
    }
    fun toggleTracking() {
        if (tracking) { Tracking.setEnabled(context, false); tracking = false; return }
        when {
            !Tracking.hasForeground(context) -> {
                val perms = mutableListOf(
                    Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION,
                )
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    perms.add(Manifest.permission.POST_NOTIFICATIONS)
                }
                fgLauncher.launch(perms.toTypedArray())
            }
            !Tracking.hasBackground(context) -> bgLauncher.launch(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
            else -> { Tracking.setEnabled(context, true); tracking = true }
        }
    }

    // On open: replay anything queued from a previous offline capture, then (if we
    // were launched from the Tile) jump straight into dictation.
    LaunchedEffect(Unit) {
        NoteQueue.flush(context)
        if (autoStart) startListening()
    }

    Scaffold(timeText = { TimeText() }) {
        Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.Center,
                modifier = Modifier
                    .fillMaxSize()
                    .padding(horizontal = 24.dp),
            ) {
                Button(
                    onClick = { startListening() },
                    enabled = phase == Phase.IDLE || phase == Phase.ERROR,
                    modifier = Modifier.size(72.dp),
                ) {
                    Icon(
                        painter = painterResource(R.drawable.ic_mic),
                        contentDescription = stringResource(R.string.speak_prompt),
                        modifier = Modifier.size(32.dp),
                    )
                }
                Spacer(modifier = Modifier.height(10.dp))
                Text(
                    text = statusFor(phase),
                    textAlign = TextAlign.Center,
                    color = MaterialTheme.colors.onBackground,
                    style = MaterialTheme.typography.caption1,
                )
                Spacer(modifier = Modifier.height(14.dp))
                Text(
                    text = if (tracking) "Location: on — tap to stop" else "Track my location",
                    textAlign = TextAlign.Center,
                    color = MaterialTheme.colors.primary,
                    style = MaterialTheme.typography.caption2,
                    modifier = Modifier
                        .clickable { toggleTracking() }
                        .padding(6.dp),
                )
            }
        }
    }
}

@Composable
private fun statusFor(phase: Phase): String = stringResource(
    when (phase) {
        Phase.IDLE -> R.string.status_idle
        Phase.SAVING -> R.string.status_saving
        Phase.SAVED -> R.string.status_saved
        Phase.OFFLINE -> R.string.status_offline
        Phase.ERROR -> R.string.status_error
    }
)

private fun vibrate(ctx: Context) {
    val vibrator = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
        (ctx.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as VibratorManager).defaultVibrator
    } else {
        @Suppress("DEPRECATION")
        ctx.getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
    }
    vibrator.vibrate(VibrationEffect.createOneShot(40, VibrationEffect.DEFAULT_AMPLITUDE))
}
