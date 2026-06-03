package com.jbrain.tracker

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Posts a batch of fixes to JBrain's /api/locations/bulk. The SERVER decides which to
 * keep (≥100 m moved OR ≥60 min elapsed, applied in order), so this just forwards the
 * buffer — duplicates are dropped server-side. URL + key come from Settings (editable
 * on-device, defaulting to BuildConfig).
 */
object LocationClient {
    private val client = OkHttpClient.Builder()
        .callTimeout(30, TimeUnit.SECONDS)
        .build()
    private val JSON = "application/json; charset=utf-8".toMediaType()

    /** POST the batch. Returns true on a 2xx, false on any HTTP/IO failure. */
    suspend fun sendBatch(ctx: Context, points: JSONArray): Boolean =
        withContext(Dispatchers.IO) {
            if (points.length() == 0) return@withContext true
            if (!Settings.isConfigured(ctx)) return@withContext false
            val url = Settings.serverUrl(ctx).trimEnd('/')
            val body = JSONObject().apply { put("points", points) }
            val request = Request.Builder()
                .url("$url/api/locations/bulk")
                .addHeader("Authorization", "Bearer ${Settings.key(ctx)}")
                .post(body.toString().toRequestBody(JSON))
                .build()
            try {
                client.newCall(request).execute().use { it.isSuccessful }
            } catch (e: Exception) {
                false
            }
        }
}
