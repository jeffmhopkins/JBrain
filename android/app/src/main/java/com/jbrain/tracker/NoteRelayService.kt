package com.jbrain.tracker

import com.google.android.gms.wearable.MessageEvent
import com.google.android.gms.wearable.WearableListenerService
import kotlinx.coroutines.runBlocking

/**
 * Receives a note dictated on the watch (relayed over the Wear Data Layer at
 * PhoneRelay.NOTE_PATH) and forwards it to JBrain using this phone's configured server
 * + key. Google Play Services starts this service on demand, so it works even when the
 * phone app isn't open.
 *
 * To preserve order, we enqueue the new note and then drain the queue oldest-first:
 * anything that failed earlier sends before this one, and if JBrain is unreachable the
 * note stays queued for the next attempt.
 */
class NoteRelayService : WearableListenerService() {

    override fun onMessageReceived(event: MessageEvent) {
        if (event.path != NOTE_PATH) return
        val text = String(event.data, Charsets.UTF_8).trim()
        if (text.isEmpty()) return
        runBlocking {
            NoteQueue.enqueue(applicationContext, text)
            NoteQueue.flush(applicationContext)
        }
    }

    companion object {
        /** Must match PhoneRelay.NOTE_PATH in the watch module. */
        const val NOTE_PATH = "/jbrain/note"
    }
}
