package com.jbrain.tracker

import android.content.Context
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
 * /api/notes/entry, exactly the flow the watch used to do itself. Now the watch relays
 * the transcript here over the Data Layer and the phone — which already holds the server
 * URL + access key from its setup code (see Settings) — does the actual send.
 *
 * The server files an untitled entry chronologically under notes/daily/YYYY/MM/DD, so
 * only the text is required.
 */
object NoteClient {
    private val client = OkHttpClient.Builder()
        .callTimeout(20, TimeUnit.SECONDS)
        .build()

    private val JSON = "application/json; charset=utf-8".toMediaType()

    /** POST the note using the phone's configured server + key. False on any failure. */
    suspend fun createEntry(ctx: Context, text: String): Boolean = withContext(Dispatchers.IO) {
        if (!Settings.isConfigured(ctx)) return@withContext false
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
            client.newCall(request).execute().use { it.isSuccessful }
        } catch (e: Exception) {
            false
        }
    }
}
