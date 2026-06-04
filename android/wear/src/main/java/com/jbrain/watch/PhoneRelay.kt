package com.jbrain.watch

import android.content.Context
import android.util.Log
import com.google.android.gms.tasks.Tasks
import com.google.android.gms.wearable.CapabilityClient
import com.google.android.gms.wearable.MessageClient
import com.google.android.gms.wearable.Wearable
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Relays a dictated note to the JBrain phone app over the Wear Data Layer and waits for
 * the phone to report back whether it actually saved to JBrain — so the watch can show a
 * real end-to-end result instead of a blind "sent". The phone holds the server key (from
 * its setup code) and does the POST; the watch carries no secrets.
 *
 * Trace the whole path with:  adb logcat -s JBrain
 */
object PhoneRelay {
    /** Watch → phone: the dictated text. Mirrors NoteRelayService.NOTE_PATH on the phone. */
    const val NOTE_PATH = "/jbrain/note"
    /** Phone → watch: "ok" or "err:<reason>". Mirrors NoteRelayService.RESULT_PATH. */
    const val RESULT_PATH = "/jbrain/note/result"
    /** Capability the phone advertises (phone res/values/wear.xml). */
    private const val RELAY_CAPABILITY = "jbrain_note_relay"
    private const val TAG = "JBrain"
    private const val ACK_TIMEOUT_MS = 12_000L

    /** The outcome of a relay attempt — each value pinpoints a different failure stage. */
    sealed class Result {
        /** Phone confirmed the note is saved in JBrain. */
        object Saved : Result()
        /** Phone received it but the save failed (it queued it and will retry). */
        data class PhoneError(val reason: String) : Result()
        /** Delivered to the phone, but no confirmation came back in time. */
        object DeliveredNoAck : Result()
        /** No paired phone running JBrain was reachable (pairing / signature / app missing). */
        object NoPhone : Result()
        /** Found the phone but couldn't deliver the message. */
        data class SendFailed(val reason: String) : Result()

        /** Did it reach the phone? If so the watch hands off — the phone owns retries. */
        val handedOff: Boolean get() = this is Saved || this is PhoneError || this is DeliveredNoAck
    }

    suspend fun send(context: Context, text: String): Result = withContext(Dispatchers.IO) {
        // 1. Find a paired phone that runs the JBrain relay.
        val node = try {
            val info = Tasks.await(
                Wearable.getCapabilityClient(context)
                    .getCapability(RELAY_CAPABILITY, CapabilityClient.FILTER_REACHABLE)
            )
            info.nodes.firstOrNull { it.isNearby } ?: info.nodes.firstOrNull()
        } catch (e: Exception) {
            Log.w(TAG, "capability lookup failed", e)
            null
        }
        if (node == null) {
            Log.w(TAG, "no JBrain phone reachable — capability '$RELAY_CAPABILITY' resolved no nodes " +
                "(check: watch paired to this phone, both apps from the SAME build, phone app installed)")
            return@withContext Result.NoPhone
        }
        Log.d(TAG, "relaying note → ${node.displayName} (${node.id}) nearby=${node.isNearby} chars=${text.length}")

        // 2. Register an ack listener BEFORE sending so we can't miss a fast reply.
        val messageClient = Wearable.getMessageClient(context)
        val ack = CompletableDeferred<String>()
        val listener = MessageClient.OnMessageReceivedListener { ev ->
            if (ev.path == RESULT_PATH) ack.complete(String(ev.data, Charsets.UTF_8))
        }
        Tasks.await(messageClient.addListener(listener))
        try {
            try {
                Tasks.await(messageClient.sendMessage(node.id, NOTE_PATH, text.toByteArray(Charsets.UTF_8)))
            } catch (e: Exception) {
                Log.w(TAG, "sendMessage to ${node.id} failed", e)
                return@withContext Result.SendFailed(e.message ?: "send error")
            }

            // 3. Wait (bounded) for the phone to report whether JBrain accepted it.
            val reply = withTimeoutOrNull(ACK_TIMEOUT_MS) { ack.await() }
            return@withContext when {
                reply == null -> { Log.w(TAG, "delivered but no ack within ${ACK_TIMEOUT_MS}ms"); Result.DeliveredNoAck }
                reply == "ok" -> { Log.d(TAG, "phone confirmed save ✓"); Result.Saved }
                reply.startsWith("err:") -> {
                    val why = reply.removePrefix("err:")
                    Log.w(TAG, "phone could not save: $why")
                    Result.PhoneError(why)
                }
                else -> Result.DeliveredNoAck
            }
        } finally {
            messageClient.removeListener(listener)
        }
    }
}
