package com.jbrain.tracker

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.Looper
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationCallback
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.LocationResult
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/**
 * Foreground service that streams location to JBrain in the background — it keeps
 * running when the app is closed (the only way a phone can do this; a PWA cannot).
 * FusedLocationProvider emits a fix once we move 100 m (as fast as every 30 s while
 * moving, else ~hourly when still); each fix is buffered offline and the whole buffer
 * is flushed to /api/locations/bulk. The SERVER applies the keep-rule authoritatively,
 * so we just forward. A persistent low-priority notification is mandatory for a
 * location foreground service on modern Android.
 */
class LocationService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private lateinit var fused: FusedLocationProviderClient

    private val iso = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US)
        .apply { timeZone = TimeZone.getTimeZone("UTC") }

    private val callback = object : LocationCallback() {
        override fun onLocationResult(result: LocationResult) {
            val loc = result.lastLocation ?: return
            val acc = if (loc.hasAccuracy()) loc.accuracy else null
            FixQueue.enqueue(applicationContext, loc.latitude, loc.longitude, acc, iso.format(Date(loc.time)))
            scope.launch { FixQueue.flush(applicationContext) }
        }
    }

    override fun onCreate() {
        super.onCreate()
        fused = LocationServices.getFusedLocationProviderClient(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startInForeground()
        requestUpdates()
        // Opportunistically flush anything buffered from a previous offline stretch.
        scope.launch { FixQueue.flush(applicationContext) }
        return START_STICKY   // the OS restarts us if it kills the service
    }

    private fun requestUpdates() {
        val req = LocationRequest.Builder(Priority.PRIORITY_BALANCED_POWER_ACCURACY, 60 * 60 * 1000L)
            .setMinUpdateIntervalMillis(30 * 1000L)       // as fast as every 30 s while moving…
            .setMinUpdateDistanceMeters(100f)             // …but only emit once we've moved 100 m
            .build()
        try {
            fused.requestLocationUpdates(req, callback, Looper.getMainLooper())
        } catch (e: SecurityException) {
            stopSelf()   // location permission was revoked
        }
    }

    private fun startInForeground() {
        val mgr = getSystemService(NotificationManager::class.java)
        mgr.createNotificationChannel(
            NotificationChannel(CHANNEL, "Location trail", NotificationManager.IMPORTANCE_LOW),
        )
        val tap = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val notif: Notification = Notification.Builder(this, CHANNEL)
            .setContentTitle("JBrain Tracker")
            .setContentText("Logging your location trail")
            .setSmallIcon(android.R.drawable.ic_menu_mylocation)
            .setContentIntent(tap)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {   // API 34+
            startForeground(NOTIF_ID, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION)
        } else {
            startForeground(NOTIF_ID, notif)
        }
    }

    override fun onDestroy() {
        fused.removeLocationUpdates(callback)
        scope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        private const val CHANNEL = "location"
        private const val NOTIF_ID = 42
    }
}
