package com.jbrain.tracker

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.os.Build
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.Data
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Uploads a widget capture to JBrain in the background. Driven by WorkManager so it
 * survives the trampoline [CaptureActivity] finishing, process death, and reboots, and
 * runs only once there's a network (NetworkType.CONNECTED) — retrying with backoff if
 * the server is unreachable or the phone isn't configured yet. The capture therefore is
 * never lost: it sits in the WorkManager queue (text in input data; the JPEG on disk)
 * until it lands.
 *
 *   - dictate: createEntry(text) — a plain dated note, like a watch relay but source=user.
 *   - photo:   createEntry("📷 Photo") to get a slug, then attach the JPEG. The created
 *              slug is cached per-work-id so a retry after a half-done upload re-attaches
 *              to the SAME note instead of spawning a duplicate.
 */
class UploadWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    override suspend fun doWork(): Result {
        val ctx = applicationContext
        return when (inputData.getString(KEY_MODE)) {
            MODE_DICTATE -> {
                val text = inputData.getString(KEY_TEXT).orEmpty().trim()
                if (text.isEmpty()) return Result.success()  // nothing said
                if (CaptureClient.createEntry(ctx, text, "user") != null) {
                    notifyDone(ctx, "Note saved to JBrain", text.take(60))
                    Result.success()
                } else {
                    notifyPending(ctx, "Note waiting to reach JBrain — will retry.")
                    Result.retry()
                }
            }
            MODE_PHOTO -> {
                val path = inputData.getString(KEY_FILE) ?: return Result.failure()
                val file = File(path)
                if (!file.exists()) return Result.success()  // already uploaded on an earlier run
                // Reuse the slug from a previous attempt so a retry doesn't create a 2nd note.
                val slug = cachedSlug(ctx)
                    ?: CaptureClient.createEntry(ctx, PHOTO_TEXT, "user")?.also { cacheSlug(ctx, it) }
                    ?: run {
                        notifyPending(ctx, "Photo waiting to reach JBrain — will retry.")
                        return Result.retry()
                    }
                if (CaptureClient.uploadPhoto(ctx, slug, file)) {
                    file.delete()
                    clearSlug(ctx)
                    notifyDone(ctx, "Photo saved to JBrain", null)
                    Result.success()
                } else {
                    notifyPending(ctx, "Photo waiting to reach JBrain — will retry.")
                    Result.retry()
                }
            }
            else -> Result.failure()
        }
    }

    // --- per-work-id slug cache: keep the photo's carrier note stable across retries ---
    private fun slugKey() = "slug_${id}"
    private fun prefs(ctx: Context) = ctx.getSharedPreferences(WORK_PREFS, Context.MODE_PRIVATE)
    private fun cachedSlug(ctx: Context) = prefs(ctx).getString(slugKey(), null)
    private fun cacheSlug(ctx: Context, slug: String) =
        prefs(ctx).edit().putString(slugKey(), slug).apply()
    private fun clearSlug(ctx: Context) = prefs(ctx).edit().remove(slugKey()).apply()

    private fun notifyDone(ctx: Context, title: String, body: String?) =
        notify(ctx, NOTIF_DONE, title, body, autoCancel = true)

    /** One stable id so repeated retries replace rather than stack up. */
    private fun notifyPending(ctx: Context, body: String) =
        notify(ctx, NOTIF_PENDING, "JBrain", body, autoCancel = false)

    private fun notify(ctx: Context, id: Int, title: String, body: String?, autoCancel: Boolean) {
        val mgr = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            mgr.createNotificationChannel(
                NotificationChannel(CHANNEL, "Captures", NotificationManager.IMPORTANCE_LOW).apply {
                    description = "Result of photos and notes sent from the home-screen widgets"
                }
            )
        }
        val n = NotificationCompat.Builder(ctx, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_menu_camera)
            .setContentTitle(title)
            .apply { body?.let { setContentText(it); setStyle(NotificationCompat.BigTextStyle().bigText(it)) } }
            .setAutoCancel(autoCancel)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
        try {
            NotificationManagerCompat.from(ctx).notify(id, n)
        } catch (e: SecurityException) {
            Log.w(TAG, "capture: no notification permission; see logcat for the result")
        }
    }

    companion object {
        private const val TAG = "JBrain"
        private const val CHANNEL = "captures"
        private const val NOTIF_DONE = 4301
        private const val NOTIF_PENDING = 4302
        private const val WORK_PREFS = "jbrain_capture_work"

        const val MODE_PHOTO = "photo"
        const val MODE_DICTATE = "dictate"
        private const val KEY_MODE = "mode"
        private const val KEY_TEXT = "text"
        private const val KEY_FILE = "file"

        /** Carrier-note body for a photo capture; the server's image analysis enriches it. */
        const val PHOTO_TEXT = "📷 Photo"

        fun enqueuePhoto(ctx: Context, filePath: String) =
            enqueue(ctx, Data.Builder().putString(KEY_MODE, MODE_PHOTO).putString(KEY_FILE, filePath).build())

        fun enqueueText(ctx: Context, text: String) =
            enqueue(ctx, Data.Builder().putString(KEY_MODE, MODE_DICTATE).putString(KEY_TEXT, text).build())

        private fun enqueue(ctx: Context, data: Data) {
            val request = OneTimeWorkRequestBuilder<UploadWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setInputData(data)
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(ctx.applicationContext).enqueue(request)
        }
    }
}
