package com.jbrain.tracker

import android.util.Base64
import org.json.JSONObject

/**
 * A one-paste setup string minted by the JBrain People card: jbt1.<base64url(JSON)>
 * carrying { n: name, s: server, k: location-key }. Pasting it into the tracker fills
 * the Name, Server URL, and Access key in one shot.
 */
object SetupCode {
    data class Parsed(val name: String, val server: String, val key: String)

    fun parse(raw: String?): Parsed? {
        val s = (raw ?: "").trim()
        if (!s.startsWith("jbt1.")) return null
        return try {
            val bytes = Base64.decode(
                s.substring(5), Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP,
            )
            val o = JSONObject(String(bytes, Charsets.UTF_8))
            Parsed(o.optString("n"), o.optString("s"), o.optString("k"))
        } catch (e: Exception) {
            null
        }
    }
}
