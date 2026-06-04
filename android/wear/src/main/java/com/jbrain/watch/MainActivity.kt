package com.jbrain.watch

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
import androidx.activity.result.contract.ActivityResultContracts.StartActivityForResult
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

@Composable
private fun CaptureScreen(autoStart: Boolean) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var busy by remember { mutableStateOf(false) }
    var status by remember { mutableStateOf(context.getString(R.string.status_idle)) }

    val speechLauncher = rememberLauncherForActivityResult(StartActivityForResult()) { result ->
        val spoken = result.data
            ?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
            ?.firstOrNull()
            ?.trim()
        if (spoken.isNullOrEmpty()) {
            status = context.getString(R.string.status_idle)
            return@rememberLauncherForActivityResult
        }
        busy = true
        status = context.getString(R.string.status_saving)
        scope.launch {
            // Map each relay outcome to a distinct on-wrist message so a failure is never
            // silent — you can see exactly which hop broke. Detail also goes to logcat.
            status = when (val r = PhoneRelay.send(context, spoken)) {
                is PhoneRelay.Result.Saved -> {
                    vibrate(context); context.getString(R.string.status_saved)
                }
                is PhoneRelay.Result.DeliveredNoAck -> {
                    vibrate(context); context.getString(R.string.status_sent_unconfirmed)
                }
                is PhoneRelay.Result.PhoneError ->
                    context.getString(R.string.status_phone_error, r.reason)
                is PhoneRelay.Result.NoPhone -> {
                    NoteQueue.enqueue(context, spoken); context.getString(R.string.status_no_phone)
                }
                is PhoneRelay.Result.SendFailed -> {
                    NoteQueue.enqueue(context, spoken); context.getString(R.string.status_unreachable)
                }
            }
            busy = false
            delay(3500)
            if (!busy) status = context.getString(R.string.status_idle)
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
            status = context.getString(R.string.status_error)
        }
    }

    // On open: replay anything queued while the phone was unreachable, then (if we were
    // launched from the Tile) jump straight into dictation.
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
                    enabled = !busy,
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
                    text = status,
                    textAlign = TextAlign.Center,
                    color = MaterialTheme.colors.onBackground,
                    style = MaterialTheme.typography.caption1,
                )
            }
        }
    }
}

private fun vibrate(ctx: Context) {
    val vibrator = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
        (ctx.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as VibratorManager).defaultVibrator
    } else {
        @Suppress("DEPRECATION")
        ctx.getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
    }
    vibrator.vibrate(VibrationEffect.createOneShot(40, VibrationEffect.DEFAULT_AMPLITUDE))
}
