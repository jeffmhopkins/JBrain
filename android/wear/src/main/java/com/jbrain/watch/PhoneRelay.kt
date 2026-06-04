package com.jbrain.watch

import android.content.Context
import com.google.android.gms.tasks.Tasks
import com.google.android.gms.wearable.CapabilityClient
import com.google.android.gms.wearable.Wearable
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * The whole "send a note" path: hand the dictated text to the JBrain phone app over the
 * Wear Data Layer. The phone holds the server domain + access key (from its setup code)
 * and does the actual POST to /api/notes/entry — so the watch carries no secrets and
 * never has to be rebuilt when the key changes.
 *
 * We target the phone via a declared capability (RELAY_CAPABILITY) rather than blindly
 * messaging every connected node, so we only deliver to a phone that actually has the
 * JBrain app installed, and never double-send.
 */
object PhoneRelay {
    /** Message path the phone's WearableListenerService filters on. */
    const val NOTE_PATH = "/jbrain/note"

    /** Capability the phone app advertises (see phone res/values/wear.xml). */
    private const val RELAY_CAPABILITY = "jbrain_note_relay"

    /**
     * Relay [text] to the phone. Returns true once it's been delivered to the phone
     * (the phone then forwards it to JBrain); false if no JBrain phone is reachable or
     * the send fails — in which case the caller queues it for the next attempt.
     */
    suspend fun send(context: Context, text: String): Boolean = withContext(Dispatchers.IO) {
        try {
            val capabilityClient = Wearable.getCapabilityClient(context)
            val info = Tasks.await(
                capabilityClient.getCapability(RELAY_CAPABILITY, CapabilityClient.FILTER_REACHABLE)
            )
            // Prefer a nearby node (the directly-paired phone) over a cloud-relayed one.
            val node = info.nodes.firstOrNull { it.isNearby } ?: info.nodes.firstOrNull()
                ?: return@withContext false

            val messageClient = Wearable.getMessageClient(context)
            Tasks.await(messageClient.sendMessage(node.id, NOTE_PATH, text.toByteArray(Charsets.UTF_8)))
            true
        } catch (e: Exception) {
            false
        }
    }
}
