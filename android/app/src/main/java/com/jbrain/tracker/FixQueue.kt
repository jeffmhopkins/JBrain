package com.jbrain.tracker

import android.content.Context
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.json.JSONArray
import org.json.JSONObject

/**
 * Offline buffer of location fixes. Every fix the OS hands us is appended here, then
 * we try to flush the whole buffer to /api/locations/bulk in one batch. If there's no
 * connectivity (or the server is down) the fixes stay and are replayed next time, so a
 * trail is never lost. Backed by SharedPreferences (a JSON array, capped).
 *
 * Concurrency: enqueue runs on the location-callback thread while flush runs on IO, so
 * the synchronous read-modify-write of the prefs is guarded by the monitor; a Mutex
 * serialises flushes (never held across a network call's suspension via @Synchronized).
 */
object FixQueue {
    private const val PREFS = "jbrain_tracker_queue"
    private const val KEY = "pending"
    private const val CAP = 5000   // oldest dropped past this (server dedups anyway)

    private val flushLock = Mutex()

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    @Synchronized
    private fun read(ctx: Context): JSONArray = JSONArray(prefs(ctx).getString(KEY, "[]"))

    @Synchronized
    private fun append(ctx: Context, obj: JSONObject) {
        val arr = JSONArray(prefs(ctx).getString(KEY, "[]"))
        arr.put(obj)
        while (arr.length() > CAP) arr.remove(0)   // trim a long offline backlog from the front
        prefs(ctx).edit().putString(KEY, arr.toString()).apply()
    }

    /** Drop the first [sentCount] fixes (the ones just uploaded); keep any appended
     *  while the upload was in flight. Returns how many remain. */
    @Synchronized
    private fun dropPrefix(ctx: Context, sentCount: Int): Int {
        val current = JSONArray(prefs(ctx).getString(KEY, "[]"))
        val remaining = JSONArray()
        for (i in sentCount until current.length()) remaining.put(current.get(i))
        prefs(ctx).edit().putString(KEY, remaining.toString()).apply()
        return remaining.length()
    }

    fun enqueue(ctx: Context, lat: Double, lon: Double, accuracyM: Float?, recordedAt: String) {
        append(ctx, JSONObject().apply {
            put("lat", lat)
            put("lon", lon)
            accuracyM?.let { put("accuracy_m", it.toDouble()) }
            put("recorded_at", recordedAt)
        })
    }

    fun size(ctx: Context): Int = read(ctx).length()

    /**
     * Send the buffered fixes as one batch (tagged with this device's name as `source`).
     * On success drop exactly the fixes we sent. Returns true if the buffer is now empty.
     */
    suspend fun flush(ctx: Context): Boolean = flushLock.withLock {
        val snapshot = read(ctx)
        val count = snapshot.length()
        if (count == 0) return@withLock true
        val source = Settings.name(ctx)
        val points = JSONArray()
        for (i in 0 until count) {
            points.put(snapshot.getJSONObject(i).apply { put("source", source) })
        }
        if (!LocationClient.sendBatch(ctx, points)) return@withLock false
        dropPrefix(ctx, count) == 0
    }
}
