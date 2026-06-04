package com.jbrain.watch

import android.content.Context
import org.json.JSONArray

/**
 * Tiny offline buffer. If a capture can't reach the phone (out of Bluetooth/Wi-Fi
 * range, phone off), the transcript is kept here and replayed on the next app/tile
 * launch instead of being lost. Backed by SharedPreferences — a few short strings, no
 * schema needed.
 */
object NoteQueue {
    private const val PREFS = "jbrain_queue"
    private const val KEY = "pending"

    private fun prefs(ctx: Context) =
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun enqueue(ctx: Context, text: String) {
        val arr = JSONArray(prefs(ctx).getString(KEY, "[]"))
        arr.put(text)
        prefs(ctx).edit().putString(KEY, arr.toString()).apply()
    }

    fun size(ctx: Context): Int =
        JSONArray(prefs(ctx).getString(KEY, "[]")).length()

    /** Replay queued notes oldest-first; stop on the first failure to preserve order. */
    suspend fun flush(ctx: Context) {
        val arr = JSONArray(prefs(ctx).getString(KEY, "[]"))
        if (arr.length() == 0) return
        val remaining = JSONArray()
        var stopped = false
        for (i in 0 until arr.length()) {
            val text = arr.getString(i)
            if (stopped) {
                remaining.put(text)
                continue
            }
            // Keep replaying until one fails to even reach the phone; once handed off,
            // the phone owns the retry, so we drop our copy to avoid duplicate notes.
            if (!PhoneRelay.send(ctx, text).handedOff) {
                stopped = true
                remaining.put(text)
            }
        }
        prefs(ctx).edit().putString(KEY, remaining.toString()).apply()
    }
}
