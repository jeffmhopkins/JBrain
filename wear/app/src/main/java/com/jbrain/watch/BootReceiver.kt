package com.jbrain.watch

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Resume location tracking after a reboot if the user had it on (and still has the permission). */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED &&
            Tracking.isEnabled(context) && Tracking.hasForeground(context)
        ) {
            Tracking.start(context)
        }
    }
}
