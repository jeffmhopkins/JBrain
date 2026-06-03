package com.jbrain.tracker

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Auto-resume tracking after a reboot if it was on. Requires BACKGROUND location:
 *  boot is a background start, and Android 14 only allows starting a location
 *  foreground service from the background when that permission is held. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED &&
            Tracking.isEnabled(context) && Tracking.hasForeground(context) && Tracking.hasBackground(context)
        ) {
            Tracking.start(context)
        }
    }
}
