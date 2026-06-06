package com.jbrain.tracker

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.Context
import android.content.Intent
import android.widget.RemoteViews

/**
 * Home-screen widgets that each open a one-tap capture: a tap fires the invisible
 * [CaptureActivity] in the right mode, which grabs the photo / dictation and uploads it
 * to JBrain via [UploadWorker]. Two thin subclasses share all the wiring; they differ
 * only in their layout (icon + label), capture mode, and PendingIntent request code.
 */
abstract class CaptureWidgetProvider : AppWidgetProvider() {
    protected abstract val layoutRes: Int
    protected abstract val mode: String
    protected abstract val requestCode: Int

    override fun onUpdate(ctx: Context, manager: AppWidgetManager, ids: IntArray) {
        // NEW_TASK because we launch from a non-activity (widget) context. No CLEAR_TASK:
        // it could tear down a capture that's mid-flight. No noHistory on the activity
        // either — that would finish it the moment the camera covers it and the result
        // (the whole point) would never come back.
        val intent = Intent(ctx, CaptureActivity::class.java)
            .putExtra(CaptureActivity.EXTRA_MODE, mode)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        val pending = PendingIntent.getActivity(
            ctx, requestCode, intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        for (id in ids) {
            val views = RemoteViews(ctx.packageName, layoutRes)
            views.setOnClickPendingIntent(R.id.widget_root, pending)
            manager.updateAppWidget(id, views)
        }
    }
}

/** Tap → camera → photo uploaded to JBrain (auto-described server-side). */
class PhotoWidgetProvider : CaptureWidgetProvider() {
    override val layoutRes = R.layout.widget_photo
    override val mode = UploadWorker.MODE_PHOTO
    override val requestCode = 1
}

/** Tap → dictate → transcript saved to JBrain as a note. */
class DictateWidgetProvider : CaptureWidgetProvider() {
    override val layoutRes = R.layout.widget_dictate
    override val mode = UploadWorker.MODE_DICTATE
    override val requestCode = 2
}
