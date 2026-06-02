package com.jbrain.watch

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.TimeUnit

/**
 * Posts a location fix to JBrain's /api/locations. The SERVER decides whether to
 * keep it (>=100 m moved OR >=60 min elapsed), so this client just forwards every
 * fix the OS hands us — duplicates are dropped server-side.
 */
object LocationClient {
    private val client = OkHttpClient.Builder()
        .callTimeout(20, TimeUnit.SECONDS)
        .build()
    private val JSON = "application/json; charset=utf-8".toMediaType()

    private val iso = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US)
        .apply { timeZone = TimeZone.getTimeZone("UTC") }

    /** POST one fix. Returns true on a 2xx, false on any HTTP/IO failure. */
    suspend fun send(lat: Double, lon: Double, accuracyM: Float?, timeMs: Long): Boolean =
        withContext(Dispatchers.IO) {
            val domain = BuildConfig.JBRAIN_DOMAIN.trimEnd('/')
            val payload = JSONObject().apply {
                put("lat", lat)
                put("lon", lon)
                accuracyM?.let { put("accuracy_m", it.toDouble()) }
                put("recorded_at", iso.format(Date(timeMs)))
                put("source", "wear")
            }
            val request = Request.Builder()
                .url("$domain/api/locations")
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
