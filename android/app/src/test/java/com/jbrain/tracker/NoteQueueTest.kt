package com.jbrain.tracker

import androidx.test.core.app.ApplicationProvider
import org.json.JSONArray
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/**
 * Unit tests for the offline note buffer that backs the watch->phone relay. Robolectric
 * supplies a real SharedPreferences + org.json so the persistence + FIFO-ordering logic
 * runs off-device. The network-replay path (flush -> NoteClient) is out of scope here
 * (covered behaviourally elsewhere); these pin the durable-buffer invariants.
 */
@RunWith(RobolectricTestRunner::class)
class NoteQueueTest {
    private val ctx get() = ApplicationProvider.getApplicationContext<android.content.Context>()

    @Before
    fun clear() {
        // Reset the backing prefs between tests (object singleton + persisted state).
        ctx.getSharedPreferences("jbrain_note_queue", android.content.Context.MODE_PRIVATE)
            .edit().clear().commit()
    }

    @Test
    fun `starts empty`() {
        assertEquals(0, NoteQueue.size(ctx))
    }

    @Test
    fun `enqueue grows the queue and persists`() {
        NoteQueue.enqueue(ctx, "first")
        NoteQueue.enqueue(ctx, "second")
        assertEquals(2, NoteQueue.size(ctx))

        // Persisted to SharedPreferences (survives a fresh read of the store).
        val raw = ctx.getSharedPreferences("jbrain_note_queue", android.content.Context.MODE_PRIVATE)
            .getString("pending", "[]")
        val arr = JSONArray(raw)
        assertEquals(2, arr.length())
    }

    @Test
    fun `preserves FIFO order`() {
        listOf("a", "b", "c").forEach { NoteQueue.enqueue(ctx, it) }
        val arr = JSONArray(
            ctx.getSharedPreferences("jbrain_note_queue", android.content.Context.MODE_PRIVATE)
                .getString("pending", "[]"))
        assertEquals("a", arr.getString(0))
        assertEquals("b", arr.getString(1))
        assertEquals("c", arr.getString(2))
    }

    @Test
    fun `retains exact text including unicode and newlines`() {
        val text = "déjà vu —\nline two 🦜"
        NoteQueue.enqueue(ctx, text)
        val arr = JSONArray(
            ctx.getSharedPreferences("jbrain_note_queue", android.content.Context.MODE_PRIVATE)
                .getString("pending", "[]"))
        assertEquals(text, arr.getString(0))
        assertTrue(NoteQueue.size(ctx) == 1)
    }
}
