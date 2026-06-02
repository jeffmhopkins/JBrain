package com.jbrain.watch

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * The entire JBrain integration: one authenticated POST to /api/notes/entry.
 *
 * The server files an untitled entry chronologically under notes/daily/YYYY/MM/DD,
 * so the watch only needs to send the dictated text (and, optionally, coordinates).
 * The domain + access key are baked into BuildConfig from local.properties at build
 * time (see app/build.gradle.kts), so nothing has to be typed on the watch.
 */
object NoteClient {
    private val client = OkHttpClient.Builder()
        .callTimeout(20, TimeUnit.SECONDS)
        .build()

    private val JSON = "application/json; charset=utf-8".toMediaType()

    /** POST the note. Returns true on a 2xx, false on any HTTP/IO failure. */
    suspend fun createEntry(text: String, lat: Double? = null, lon: Double? = null): Boolean =
        withContext(Dispatchers.IO) {
            val domain = BuildConfig.JBRAIN_DOMAIN.trimEnd('/')
            val payload = JSONObject().apply {
                put("text", text)
                lat?.let { put("lat", it) }
                lon?.let { put("lon", it) }
            }
            val request = Request.Builder()
                .url("$domain/api/notes/entry")
                .addHeader("Authorization", "Bearer ${BuildConfig.JBRAIN_KEY}")
                .post(payload.toString().toRequestBody(JSON))
                .build()
            try {
                client.newCall(request).execute().use { it.isSuccessful }
            } catch (e: Exception) {
                false
            }
        }
}
