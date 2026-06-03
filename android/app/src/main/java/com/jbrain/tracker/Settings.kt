package com.jbrain.tracker

import android.content.Context
import android.os.Build

/**
 * The whole config surface: the JBrain server URL, the access key, this device's
 * Name (sent as the trail's `source`, so family phones stay distinct), and the
 * on/off switch. URL + key default to whatever was baked into BuildConfig from
 * local.properties at build time, so a shared family APK only needs a Name; all four
 * remain editable on-device. Backed by SharedPreferences — four short values.
 */
object Settings {
    private const val PREFS = "jbrain_tracker"

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun serverUrl(ctx: Context): String =
        prefs(ctx).getString("server_url", null)?.takeIf { it.isNotBlank() }
            ?: BuildConfig.JBRAIN_DOMAIN

    fun key(ctx: Context): String =
        prefs(ctx).getString("key", null)?.takeIf { it.isNotBlank() }
            ?: BuildConfig.JBRAIN_KEY

    /** This device's label (max 32 to match the server's `source` column). */
    fun name(ctx: Context): String =
        (prefs(ctx).getString("name", null)?.takeIf { it.isNotBlank() } ?: Build.MODEL ?: "phone")
            .take(32)

    fun enabled(ctx: Context): Boolean = prefs(ctx).getBoolean("enabled", false)

    fun setServerUrl(ctx: Context, v: String) = prefs(ctx).edit().putString("server_url", v.trim()).apply()
    fun setKey(ctx: Context, v: String) = prefs(ctx).edit().putString("key", v.trim()).apply()
    fun setName(ctx: Context, v: String) = prefs(ctx).edit().putString("name", v.trim()).apply()
    fun setEnabled(ctx: Context, v: Boolean) = prefs(ctx).edit().putBoolean("enabled", v).apply()

    /** Configured enough to actually send? */
    fun isConfigured(ctx: Context): Boolean =
        serverUrl(ctx).startsWith("http") && key(ctx).isNotBlank()
}
