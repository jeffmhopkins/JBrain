package com.jbrain.tracker

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.core.content.ContextCompat
import com.google.android.gms.location.ActivityTransition
import com.google.android.gms.location.ActivityTransitionResult
import com.google.android.gms.location.DetectedActivity

/**
 * Activity Recognition delivers "still ⇄ moving" transitions here; we relay the
 * verdict to the LocationService so it can wake the GPS when you start moving and
 * sleep it when you stop. Entering STILL = stop; exiting STILL (or entering any
 * moving activity) = go.
 */
class ActivityReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (!ActivityTransitionResult.hasResult(intent)) return
        val result = ActivityTransitionResult.extractResult(intent) ?: return
        var moving: Boolean? = null
        for (e in result.transitionEvents) {
            val enter = e.transitionType == ActivityTransition.ACTIVITY_TRANSITION_ENTER
            when (e.activityType) {
                DetectedActivity.STILL -> moving = !enter
                DetectedActivity.WALKING, DetectedActivity.RUNNING, DetectedActivity.ON_FOOT,
                DetectedActivity.ON_BICYCLE, DetectedActivity.IN_VEHICLE -> if (enter) moving = true
            }
        }
        if (moving != null && Tracking.isEnabled(ctx)) {
            ContextCompat.startForegroundService(
                ctx, Intent(ctx, LocationService::class.java).putExtra(LocationService.EXTRA_MOVING, moving),
            )
        }
    }
}
