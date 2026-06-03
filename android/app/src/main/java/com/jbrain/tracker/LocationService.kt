package com.jbrain.tracker

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.location.Location
import android.os.Build
import android.os.IBinder
import android.os.Looper
import androidx.core.content.ContextCompat
import com.google.android.gms.location.ActivityRecognition
import com.google.android.gms.location.ActivityTransition
import com.google.android.gms.location.ActivityTransitionRequest
import com.google.android.gms.location.DetectedActivity
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationCallback
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.LocationResult
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/**
 * Foreground service that streams location to JBrain in the background — it keeps
 * running when the app is closed (a PWA can't). SMART POLLING: it asks Activity
 * Recognition for "still ⇄ moving" transitions and only runs continuous GPS while
 * you're MOVING; when STILL it shuts the GPS off and just sends a low-frequency
 * heartbeat, waking the moment you move again. (No activity permission → it falls
 * back to always-on continuous fixes.) Each fix is buffered offline and flushed to
 * /api/locations/bulk; the SERVER applies the keep-rule, so we just forward.
 */
class LocationService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private lateinit var fused: FusedLocationProviderClient

    private var updatesActive = false
    private var moving = true
    private var heartbeat: Job? = null

    private val iso = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US)
        .apply { timeZone = TimeZone.getTimeZone("UTC") }

    private val callback = object : LocationCallback() {
        override fun onLocationResult(result: LocationResult) {
            result.lastLocation?.let { record(it) }
        }
    }

    override fun onCreate() {
        super.onCreate()
        fused = LocationServices.getFusedLocationProviderClient(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startInForeground()
        if (intent != null && intent.hasExtra(EXTRA_MOVING)) {
            setMoving(intent.getBooleanExtra(EXTRA_MOVING, true))
        } else {
            // Fresh start: register transitions and assume moving until told otherwise.
            registerActivityTransitions()
            setMoving(true)
            scope.launch { FixQueue.flush(applicationContext) }   // drain any offline backlog
        }
        return START_STICKY   // the OS restarts us if it kills the service
    }

    /** Switch between continuous GPS (moving) and GPS-off + heartbeat (still). */
    private fun setMoving(m: Boolean) {
        moving = m
        if (m) {
            heartbeat?.cancel(); heartbeat = null
            startUpdates()
        } else {
            stopUpdates()
            grabSingleFix()            // mark where you stopped
            startHeartbeat()           // …then just an occasional ping while parked
        }
        updateNotification()
    }

    private fun startUpdates() {
        if (updatesActive) return
        val req = LocationRequest.Builder(Priority.PRIORITY_BALANCED_POWER_ACCURACY, 30 * 1000L)
            .setMinUpdateIntervalMillis(20 * 1000L)       // as fast as every 20 s while moving…
            .setMinUpdateDistanceMeters(60f)              // …but only once we've moved ~60 m
            .build()
        try {
            fused.requestLocationUpdates(req, callback, Looper.getMainLooper())
            updatesActive = true
        } catch (e: SecurityException) {
            stopSelf()   // location permission revoked
        }
    }

    private fun stopUpdates() {
        if (!updatesActive) return
        fused.removeLocationUpdates(callback)
        updatesActive = false
    }

    /** While still, ping ~every 20 min so "last seen" stays fresh without polling GPS. */
    private fun startHeartbeat() {
        heartbeat?.cancel()
        heartbeat = scope.launch {
            while (isActive && !moving) {
                delay(20 * 60 * 1000L)
                if (!moving) grabSingleFix()
            }
        }
    }

    private fun grabSingleFix() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
            != PackageManager.PERMISSION_GRANTED &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_COARSE_LOCATION)
            != PackageManager.PERMISSION_GRANTED) return
        try {
            fused.getCurrentLocation(Priority.PRIORITY_BALANCED_POWER_ACCURACY, null)
                .addOnSuccessListener { loc -> loc?.let { record(it) } }
        } catch (e: SecurityException) { /* permission revoked; ignore */ }
    }

    private fun record(loc: Location) {
        val acc = if (loc.hasAccuracy()) loc.accuracy else null
        FixQueue.enqueue(applicationContext, loc.latitude, loc.longitude, acc, iso.format(Date(loc.time)))
        scope.launch { FixQueue.flush(applicationContext) }
    }

    private fun registerActivityTransitions() {
        if (!hasActivityPermission()) return   // fall back to always-on continuous fixes
        val moves = intArrayOf(
            DetectedActivity.STILL, DetectedActivity.WALKING, DetectedActivity.RUNNING,
            DetectedActivity.ON_FOOT, DetectedActivity.ON_BICYCLE, DetectedActivity.IN_VEHICLE,
        )
        val transitions = mutableListOf<ActivityTransition>()
        for (a in moves) {
            transitions.add(buildTransition(a, ActivityTransition.ACTIVITY_TRANSITION_ENTER))
            if (a == DetectedActivity.STILL) {
                transitions.add(buildTransition(a, ActivityTransition.ACTIVITY_TRANSITION_EXIT))
            }
        }
        try {
            ActivityRecognition.getClient(this)
                .requestActivityTransitionUpdates(ActivityTransitionRequest(transitions), activityPI())
        } catch (e: SecurityException) { /* ignore — stay in continuous mode */ }
    }

    private fun buildTransition(activityType: Int, transitionType: Int): ActivityTransition =
        ActivityTransition.Builder()
            .setActivityType(activityType)
            .setActivityTransition(transitionType)   // 21.x AAR name (docs say …TransitionType)
            .build()

    private fun activityPI(): PendingIntent {
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or
            (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) PendingIntent.FLAG_MUTABLE else 0)
        return PendingIntent.getBroadcast(this, 7, Intent(this, ActivityReceiver::class.java), flags)
    }

    private fun hasActivityPermission(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.Q ||
        ContextCompat.checkSelfPermission(this, Manifest.permission.ACTIVITY_RECOGNITION) == PackageManager.PERMISSION_GRANTED

    private fun startInForeground() {
        val mgr = getSystemService(NotificationManager::class.java)
        mgr.createNotificationChannel(
            NotificationChannel(CHANNEL, "Location trail", NotificationManager.IMPORTANCE_LOW),
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {   // API 34+
            startForeground(NOTIF_ID, buildNotification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION)
        } else {
            startForeground(NOTIF_ID, buildNotification())
        }
    }

    private fun updateNotification() {
        getSystemService(NotificationManager::class.java).notify(NOTIF_ID, buildNotification())
    }

    private fun buildNotification(): Notification {
        val tap = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return Notification.Builder(this, CHANNEL)
            .setContentTitle("JBrain Tracker")
            .setContentText(if (moving) "Logging your location trail" else "Parked — sleeping until you move")
            .setSmallIcon(android.R.drawable.ic_menu_mylocation)
            .setContentIntent(tap)
            .setOngoing(true)
            .build()
    }

    override fun onDestroy() {
        stopUpdates()
        heartbeat?.cancel()
        if (hasActivityPermission()) {
            try { ActivityRecognition.getClient(this).removeActivityTransitionUpdates(activityPI()) } catch (e: SecurityException) { /* ignore */ }
        }
        scope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        const val EXTRA_MOVING = "moving"
        private const val CHANNEL = "location"
        private const val NOTIF_ID = 42
    }
}
