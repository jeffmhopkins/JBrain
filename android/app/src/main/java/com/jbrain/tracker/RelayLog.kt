package com.jbrain.tracker

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * A tiny rolling log of the last few watch-note relays, so the phone screen can show —
 * at a glance — what came in from the watch and whether it reached JBrain. Makes the
 * otherwise-invisible background relay observable without adb. Backed by SharedPreferences.
 */
object RelayLog {
    private const val PREFS = "jbrain_relay_log"
    private const val KEY = "events"
    private const val MAX = 8

    data class Event(val time: String, val ok: Boolean, val text: String, val detail: String)

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
    private val fmt = SimpleDateFormat("MMM d HH:mm:ss", Locale.getDefault())

    fun record(ctx: Context, ok: Boolean, text: String, detail: String) {
        val arr = JSONArray(prefs(ctx).getString(KEY, "[]"))
        val e = JSONObject()
            .put("t", fmt.format(Date()))
            .put("ok", ok)
            .put("x", text.take(80))
            .put("d", detail)
        // Newest first, capped.
        val out = JSONArray().put(e)
        for (i in 0 until minOf(arr.length(), MAX - 1)) out.put(arr.getJSONObject(i))
        prefs(ctx).edit().putString(KEY, out.toString()).apply()
    }

    fun recent(ctx: Context): List<Event> {
        val arr = JSONArray(prefs(ctx).getString(KEY, "[]"))
        return (0 until arr.length()).map {
            val o = arr.getJSONObject(it)
            Event(o.getString("t"), o.getBoolean("ok"), o.getString("x"), o.optString("d"))
        }
    }
}
