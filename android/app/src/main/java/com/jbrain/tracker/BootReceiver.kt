package com.jbrain.tracker

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Resume location tracking after a reboot if it was on (and the permission survives). */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED &&
            Tracking.isEnabled(context) && Tracking.hasForeground(context)
        ) {
            Tracking.start(context)
        }
    }
}
