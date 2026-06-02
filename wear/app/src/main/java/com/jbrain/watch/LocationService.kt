package com.jbrain.watch

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
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

/**
 * Foreground service that streams location to JBrain in the background — it keeps
 * running when the app is closed (the only way a watch/phone can do this; a PWA
 * cannot). FusedLocationProvider emits a fix at most hourly OR as soon as we move
 * 100 m; the SERVER then applies the same rule authoritatively, so this just sends
 * whatever the OS hands us. A persistent low-priority notification is mandatory for
 * a location foreground service on modern Android.
 */
class LocationService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private lateinit var fused: FusedLocationProviderClient

    private val callback = object : LocationCallback() {
        override fun onLocationResult(result: LocationResult) {
            val loc = result.lastLocation ?: return
            scope.launch {
                LocationClient.send(
                    loc.latitude, loc.longitude,
                    if (loc.hasAccuracy()) loc.accuracy else null, loc.time,
                )
            }
        }
    }

    override fun onCreate() {
        super.onCreate()
        fused = LocationServices.getFusedLocationProviderClient(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startInForeground()
        requestUpdates()
        return START_STICKY   // the OS restarts us if it kills the service
    }

    private fun requestUpdates() {
        val req = LocationRequest.Builder(Priority.PRIORITY_BALANCED_POWER_ACCURACY, 60 * 60 * 1000L)
            .setMinUpdateIntervalMillis(5 * 60 * 1000L)   // never faster than every 5 min
            .setMinUpdateDistanceMeters(100f)             // …but do emit once we've moved 100 m
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
        val notif: Notification = Notification.Builder(this, CHANNEL)
            .setContentTitle("JBrain")
            .setContentText("Logging your location trail")
            .setSmallIcon(R.drawable.ic_mic)
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
