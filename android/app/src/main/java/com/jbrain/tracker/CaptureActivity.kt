package com.jbrain.tracker

import android.annotation.SuppressLint
import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Intent
import android.os.Bundle
import android.provider.MediaStore
import android.speech.RecognizerIntent
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts.StartActivityForResult
import androidx.core.content.FileProvider
import java.io.File

/**
 * The one-tap bridge behind both home-screen widgets. A widget tap launches this
 * invisible (translucent) activity, which immediately fires the system camera or the
 * speech recognizer, hands the result to [UploadWorker] for a durable background upload,
 * and finishes — no app UI in between.
 *
 * Capture uses system intents (ACTION_IMAGE_CAPTURE / ACTION_RECOGNIZE_SPEECH), so this
 * app needs neither CAMERA nor RECORD_AUDIO: the camera/recognizer apps own those grants.
 */
// We extend ComponentActivity (androidx.activity), which implements the ActivityResult
// contract correctly — the lint check only fires because there's no androidx.fragment
// dependency to verify against (we use no Fragments), so it's a false positive here.
@SuppressLint("InvalidFragmentVersionForActivityResult")
class CaptureActivity : ComponentActivity() {

    // Where the camera app writes the JPEG. Persisted across recreation (the camera runs
    // in its own process and ours can be killed while it's foreground).
    private var photoPath: String? = null

    private val photoLauncher = registerForActivityResult(StartActivityForResult()) { result ->
        val path = photoPath
        if (result.resultCode == Activity.RESULT_OK && path != null) {
            UploadWorker.enqueuePhoto(applicationContext, path)
        } else {
            path?.let { File(it).delete() }   // cancelled / failed — don't leave a 0-byte temp
        }
        finish()
    }

    private val speechLauncher = registerForActivityResult(StartActivityForResult()) { result ->
        val text = result.data
            ?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
            ?.firstOrNull()
            ?.trim()
        if (!text.isNullOrEmpty()) UploadWorker.enqueueText(applicationContext, text)
        finish()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // On recreation the registered launchers redeliver the pending result; just
        // restore where the photo lives and let that callback run — don't relaunch.
        if (savedInstanceState != null) {
            photoPath = savedInstanceState.getString(STATE_PHOTO_PATH)
            return
        }
        when (intent?.getStringExtra(EXTRA_MODE)) {
            UploadWorker.MODE_PHOTO -> startPhoto()
            UploadWorker.MODE_DICTATE -> startDictation()
            else -> finish()
        }
    }

    private fun startPhoto() {
        val dir = File(filesDir, "captures").apply { mkdirs() }
        val file = File(dir, "photo_${System.currentTimeMillis()}.jpg")
        photoPath = file.absolutePath
        val uri = FileProvider.getUriForFile(this, "$packageName.fileprovider", file)
        val intent = Intent(MediaStore.ACTION_IMAGE_CAPTURE)
            .putExtra(MediaStore.EXTRA_OUTPUT, uri)
            // Let the camera app write to (and read back) our file URI.
            .addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION or Intent.FLAG_GRANT_READ_URI_PERMISSION)
        try {
            photoLauncher.launch(intent)
        } catch (e: ActivityNotFoundException) {
            toast(getString(R.string.capture_no_camera))
            file.delete()
            finish()
        }
    }

    private fun startDictation() {
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            .putExtra(RecognizerIntent.EXTRA_PROMPT, getString(R.string.capture_speak_prompt))
        try {
            speechLauncher.launch(intent)
        } catch (e: ActivityNotFoundException) {
            toast(getString(R.string.capture_no_speech))
            finish()
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString(STATE_PHOTO_PATH, photoPath)
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_LONG).show()

    companion object {
        const val EXTRA_MODE = "com.jbrain.tracker.MODE"
        private const val STATE_PHOTO_PATH = "photo_path"
    }
}
