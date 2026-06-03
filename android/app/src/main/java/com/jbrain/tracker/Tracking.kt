package com.jbrain.tracker

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.content.ContextCompat

/** Reads the enabled flag (in Settings) and starts/stops the foreground service. */
object Tracking {
    fun isEnabled(ctx: Context): Boolean = Settings.enabled(ctx)

    fun setEnabled(ctx: Context, on: Boolean) {
        Settings.setEnabled(ctx, on)
        if (on) start(ctx) else stop(ctx)
    }

    fun hasForeground(ctx: Context): Boolean =
        ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED ||
        ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED

    fun hasBackground(ctx: Context): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.Q ||
        ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_BACKGROUND_LOCATION) == PackageManager.PERMISSION_GRANTED

    fun start(ctx: Context) {
        ContextCompat.startForegroundService(ctx, Intent(ctx, LocationService::class.java))
    }

    fun stop(ctx: Context) {
        ctx.stopService(Intent(ctx, LocationService::class.java))
    }
}
