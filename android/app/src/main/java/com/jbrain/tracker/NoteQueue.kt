package com.jbrain.tracker

import android.content.Context
import org.json.JSONArray

/**
 * Offline buffer for watch-relayed notes. If a relayed note can't reach JBrain (no
 * signal, server down, or the phone isn't configured yet), the transcript is kept here
 * and replayed — on the next relayed note and when the app is opened — so nothing
 * dictated on the wrist is lost. Backed by SharedPreferences; a few short strings.
 */
object NoteQueue {
    private const val PREFS = "jbrain_note_queue"
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
            if (!NoteClient.createEntry(ctx, text)) {
                stopped = true
                remaining.put(text)
            }
        }
        prefs(ctx).edit().putString(KEY, remaining.toString()).apply()
    }
}
