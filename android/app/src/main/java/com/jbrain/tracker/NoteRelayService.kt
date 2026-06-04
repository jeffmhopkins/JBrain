package com.jbrain.tracker

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.os.Build
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.google.android.gms.tasks.Tasks
import com.google.android.gms.wearable.MessageEvent
import com.google.android.gms.wearable.Wearable
import com.google.android.gms.wearable.WearableListenerService
import kotlinx.coroutines.runBlocking

/**
 * Receives a note dictated on the watch (relayed over the Wear Data Layer at
 * [NOTE_PATH]) and forwards it to JBrain using this phone's configured server + key.
 * Google Play Services starts this service on demand, so it works even when the phone
 * app isn't open.
 *
 * Nothing here fails silently. Every relay:
 *   - acks the watch on [RESULT_PATH] with "ok" or "err:<reason>" (drives the wrist UI),
 *   - posts a phone notification with the outcome (visible with the app closed),
 *   - records a [RelayLog] entry (shown on the phone screen),
 *   - logs to `adb logcat -s JBrain`.
 */
class NoteRelayService : WearableListenerService() {

    override fun onMessageReceived(event: MessageEvent) {
        if (event.path != NOTE_PATH) return
        val text = String(event.data, Charsets.UTF_8).trim()
        Log.d(TAG, "received watch note: ${text.length} chars from ${event.sourceNodeId}")
        if (text.isEmpty()) {
            ack(event.sourceNodeId, "err:empty note")
            return
        }

        // Forward now; on failure keep it queued on the phone and drain the backlog when
        // a later note succeeds. NoteClient reports WHY it failed so we can surface it.
        val result = runBlocking {
            val r = NoteClient.createEntry(applicationContext, text)
            if (r is NoteClient.Result.Ok) NoteQueue.flush(applicationContext)
            else NoteQueue.enqueue(applicationContext, text)
            r
        }

        when (result) {
            is NoteClient.Result.Ok -> {
                Log.d(TAG, "forwarded watch note to JBrain ✓")
                ack(event.sourceNodeId, "ok")
                RelayLog.record(applicationContext, ok = true, text = text, detail = "saved to JBrain")
                notify(true, "Saved watch note", text)
            }
            is NoteClient.Result.Failed -> {
                Log.w(TAG, "watch note NOT saved: ${result.reason} (kept in phone queue)")
                ack(event.sourceNodeId, "err:${result.reason}")
                RelayLog.record(applicationContext, ok = false, text = text, detail = result.reason)
                notify(false, "Watch note failed", "${result.reason} — “${text.take(40)}”")
            }
        }
    }

    /** Reply to the watch so it can show a real end-to-end result instead of a blind send. */
    private fun ack(nodeId: String, payload: String) {
        try {
            Tasks.await(Wearable.getMessageClient(this).sendMessage(nodeId, RESULT_PATH, payload.toByteArray()))
        } catch (e: Exception) {
            Log.w(TAG, "couldn't ack the watch ($nodeId)", e)
        }
    }

    private fun notify(ok: Boolean, title: String, body: String) {
        val mgr = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            mgr.createNotificationChannel(
                NotificationChannel(CHANNEL, "Watch notes", NotificationManager.IMPORTANCE_DEFAULT).apply {
                    description = "Result of notes dictated on the watch"
                }
            )
        }
        val n = NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_menu_edit)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .build()
        // No-ops on Android 13+ if the user hasn't granted POST_NOTIFICATIONS; the
        // on-screen RelayLog + logcat still tell the story.
        try {
            NotificationManagerCompat.from(this).notify(if (ok) NOTIF_OK else NOTIF_ERR, n)
        } catch (e: SecurityException) {
            Log.w(TAG, "notification permission not granted; see the app screen instead")
        }
    }

    companion object {
        private const val TAG = "JBrain"
        private const val CHANNEL = "watch_relay"
        private const val NOTIF_OK = 4201
        private const val NOTIF_ERR = 4202

        /** Must match PhoneRelay.NOTE_PATH / RESULT_PATH in the watch module. */
        const val NOTE_PATH = "/jbrain/note"
        const val RESULT_PATH = "/jbrain/note/result"
    }
}
