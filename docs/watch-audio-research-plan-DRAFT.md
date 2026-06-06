# DRAFT — Watch Audio Research Tile ("Ask JBrain") — pre-review

> Status: DRAFT for adversarial review. Not final. Reviewers: poke holes.

## Goal
A Wear OS tile that lets the user tap a button, ask a spoken question, and hear the
JBrain **research-mode** answer spoken back **from the watch** over audio. One-tap from
the wrist: ask → hear answer.

## Existing architecture (verified facts)
- **Watch is thin & keyless.** `android/wear` dictates via `RecognizerIntent` and relays
  the transcript to the phone over the Wear Data Layer (`PhoneRelay.send`, path
  `/jbrain/note`, result `/jbrain/note/result`, **ACK_TIMEOUT_MS = 12_000**). No INTERNET
  permission, no server key on the watch. Capability `jbrain_note_relay` advertised by the
  phone (`android/app/.../res/values/wear.xml`).
- **Phone holds the key.** `NoteRelayService` (a `WearableListenerService`) receives the
  note and `NoteClient` POSTs `/api/notes/entry` with `Authorization: Bearer <key>` from
  `Settings`. Acks the watch "ok"/"err:<reason>", posts a notification, records `RelayLog`.
- **Research mode** = `architect.run(conversation_id, text, location, mode="research")`,
  an async generator yielding SSE events: `token` (text deltas → the answer), `tool`,
  `staging`, `chart`, `applied`, `error`, `done`. Exposed ONLY via SSE streaming endpoint
  `POST /api/chat/conversations/{id}/message` (`server/app/routers/chat.py`). It loads
  history from `messages`, **persists** the user turn and final assistant turn, needs a
  conversation row. Research mode is read-only (no wiki writes).
- **Auth**: single shared Bearer access key over HTTPS (`server/app/auth.py`).
- **TTS today**: browser-only Web Speech API (`web/src/hooks.ts useTts`). No server TTS.
  No TTS anywhere on the watch yet. Server STT is batch Whisper for attachments only.
- Watch & phone share `applicationId = com.jbrain.tracker` and signing key (required for
  embedded-wear + Data Layer routing).

## Proposed design (v0 — under review)

### Flow
1. New **AskTile** in the watch carousel → launches `MainActivity` with `EXTRA_MODE="ask"`
   (or a dedicated `AskActivity`), auto-starting speech capture.
2. Watch captures the question with `RecognizerIntent` (reuse existing pattern).
3. Watch relays the question to the phone over the Data Layer on a **new path**
   `/jbrain/research` and waits for `/jbrain/research/result`.
4. Phone `ResearchRelayService` (new `WearableListenerService` path, or extend the
   existing one) calls a **new server endpoint** and gets the final answer text.
5. Phone relays the answer text back to the watch.
6. Watch **speaks the answer** via Android `android.speech.tts.TextToSpeech`, and also
   shows it on screen (scrollable).

### Server change
Add a **non-streaming** convenience endpoint for voice clients:
`POST /api/chat/ask` → body `{ "text": "...", "voice": true }`, returns
`{ "answer": "...", "conversation_id": N }`. Internally creates (or reuses a dedicated
"watch" conversation), drives `architect.run(mode="research")`, concatenates `token`
events, returns the final text. Optionally a `voice_answer` trimmed/spoken-friendly
variant (strip `[[wikilinks]]`, markdown, charts) capped to ~600 chars for speech.

### Watch TTS
Use on-device Android TTS (`TextToSpeech`), speak through the watch speaker or a paired
BT audio device. Show text + a replay button. No new dangerous permissions for TTS.

### Why these choices
- Mirrors the proven note-relay path → keyless watch, phone owns the key.
- Non-streaming server endpoint avoids SSE parsing on Android and lets us cap/clean text
  for speech.
- On-device TTS avoids shipping audio bytes over the Data Layer.

## Known open questions / risks (seed for reviewers)
- **Latency vs ACK timeout**: a research turn (LLM + tools) can take 10–40s. The existing
  12s ack window is too short. Need a longer/asynchronous relay protocol (progress pings,
  resumable, or a foreground-ish wait). What's the right pattern?
- **TTS availability on Wear**: not all watches have a speaker / installed TTS engine /
  audio route. Fallback to on-screen text. Volume, Do Not Disturb, BT routing.
- **Answer length & format for speech**: markdown, citations `[[Title]]`, charts, tables
  are unspeakable. Need a voice-cleaned, length-capped variant.
- **Conversation state**: ephemeral per-question vs a persistent "watch" conversation
  (context carryover vs unbounded growth/privacy). Default?
- **Data Layer message size** (~100KB cap) for long answers; truncation policy.
- **Privacy**: spoken answers may include sensitive health data, audible to bystanders.
  Consent/affordance? Default to text + opt-in speech?
- **Permissions**: `RecognizerIntent` handles its own mic; confirm no new RECORD_AUDIO.
- **Offline**: queueing stale questions is pointless — fail fast instead.
- **Cost/abuse**: each tap = an LLM turn. Rate limiting / budget (architect already has a
  token budget backstop). Accidental taps.
- **Error surfacing**: mirror the note flow's "never fail silently" (wrist status,
  notification, RelayLog, logcat).
