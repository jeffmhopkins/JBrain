package com.jbrain.tracker

import android.content.Context
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Files a note dictated on the watch into JBrain: one authenticated POST to
 * /api/notes/entry, tagged source=watch for provenance. The watch relays the transcript
 * here over the Data Layer and the phone — which holds the server URL + access key from
 * its setup code — does the actual send.
 *
 * Returns a [Result] carrying a human-readable reason on failure so the relay can report
 * exactly what went wrong (back to the watch, in a notification, and to logcat).
 */
object NoteClient {
    private const val TAG = "JBrain"

    private val client = OkHttpClient.Builder()
        .callTimeout(20, TimeUnit.SECONDS)
        .build()

    private val JSON = "application/json; charset=utf-8".toMediaType()

    sealed class Result {
        object Ok : Result()
        data class Failed(val reason: String) : Result()
    }

    suspend fun createEntry(ctx: Context, text: String): Result = withContext(Dispatchers.IO) {
        if (!Settings.isConfigured(ctx)) {
            Log.w(TAG, "phone not configured — paste a setup code so it has a server + key")
            return@withContext Result.Failed("phone not configured (paste setup code)")
        }
        val domain = Settings.serverUrl(ctx).trimEnd('/')
        // Tag the provenance so the note's version history shows it was dictated on the
        // watch and relayed through the phone, not typed.
        val payload = JSONObject().put("text", text).put("source", "watch")
        val request = Request.Builder()
            .url("$domain/api/notes/entry")
            .addHeader("Authorization", "Bearer ${Settings.key(ctx)}")
            .post(payload.toString().toRequestBody(JSON))
            .build()
        try {
            client.newCall(request).execute().use {
                if (it.isSuccessful) {
                    Log.d(TAG, "saved watch note to JBrain (HTTP ${it.code})")
                    Result.Ok
                } else {
                    Log.w(TAG, "JBrain rejected watch note: HTTP ${it.code} from $domain")
                    Result.Failed("server HTTP ${it.code}")
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "couldn't reach JBrain at $domain", e)
            Result.Failed("network: ${e.message ?: "unreachable"}")
        }
    }
}
