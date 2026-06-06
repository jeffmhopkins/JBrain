package com.jbrain.tracker

import android.content.Context
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * HTTP for the home-screen capture widgets. Two calls, both Bearer-authed with the
 * phone's configured key (same key the location trail + watch relay use):
 *
 *   1. [createEntry] → POST /api/notes/entry, returns the new note's slug.
 *   2. [uploadPhoto] → POST /api/notes/{slug}/attachments (multipart), so the server
 *      stores the image and (analyze=true) auto-describes it with the LLM.
 *
 * The dictation widget only needs step 1. The photo widget does both: a tiny carrier
 * note, then the image attached to it. Network/HTTP failures return null/false so the
 * [UploadWorker] can retry (WorkManager backoff) without losing the capture.
 */
object CaptureClient {
    private const val TAG = "JBrain"

    // Photos are larger than a JSON note, so allow longer than NoteClient's 20s.
    private val client = OkHttpClient.Builder()
        .callTimeout(60, TimeUnit.SECONDS)
        .build()
    private val JSON = "application/json; charset=utf-8".toMediaType()

    /** Create a note and return its slug (e.g. "notes-2026-06-06-01"), or null on failure. */
    suspend fun createEntry(ctx: Context, text: String, source: String): String? =
        withContext(Dispatchers.IO) {
            if (!Settings.isConfigured(ctx)) {
                Log.w(TAG, "capture: phone not configured — paste a setup code")
                return@withContext null
            }
            val url = Settings.serverUrl(ctx).trimEnd('/')
            val payload = JSONObject().put("text", text).put("source", source)
            val request = Request.Builder()
                .url("$url/api/notes/entry")
                .addHeader("Authorization", "Bearer ${Settings.key(ctx)}")
                .post(payload.toString().toRequestBody(JSON))
                .build()
            try {
                client.newCall(request).execute().use { resp ->
                    if (!resp.isSuccessful) {
                        Log.w(TAG, "capture: entry rejected HTTP ${resp.code}")
                        return@withContext null
                    }
                    JSONObject(resp.body?.string().orEmpty())
                        .optString("slug").takeIf { it.isNotBlank() }
                }
            } catch (e: Exception) {
                Log.w(TAG, "capture: couldn't reach $url", e)
                null
            }
        }

    /** Attach [file] (a JPEG) to the note at [slug]; analyze=true → server auto-describes it. */
    suspend fun uploadPhoto(ctx: Context, slug: String, file: File): Boolean =
        withContext(Dispatchers.IO) {
            if (!Settings.isConfigured(ctx)) return@withContext false
            val url = Settings.serverUrl(ctx).trimEnd('/')
            val body = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("file", file.name, file.asRequestBody("image/jpeg".toMediaType()))
                .addFormDataPart("analyze", "true")
                .build()
            val request = Request.Builder()
                .url("$url/api/notes/$slug/attachments")
                .addHeader("Authorization", "Bearer ${Settings.key(ctx)}")
                .post(body)
                .build()
            try {
                client.newCall(request).execute().use {
                    if (!it.isSuccessful) Log.w(TAG, "capture: attach rejected HTTP ${it.code}")
                    it.isSuccessful
                }
            } catch (e: Exception) {
                Log.w(TAG, "capture: couldn't upload photo to $url", e)
                false
            }
        }
}
