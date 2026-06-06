# Watch Audio Research Tile — "Ask JBrain" (Implementation Plan)

Tap a tile on the watch → speak a question → hear the **research-mode** answer spoken back
from the wrist (with the text on screen). This plan has been through three parallel review
lanes — development/architecture, security red-team, and Wear OS platform feasibility — and
the findings are folded in below.

---

## 1. Verdict

**Feasible, no platform showstoppers**, on the existing `minSdk 30 / targetSdk 34` setup. But
this is **not** "a small extension of the note relay." Two things make it materially
different and must be designed up front:

1. **It's a brain-wide READ channel.** The note path is deliberately write-only and
   content-blind; this feature reads the whole brain (health, labs, GPS) and speaks it
   aloud. That changes the trust, privacy, and auth model.
2. **It's long-running (10–40s).** The note relay's synchronous 12s ack model and
   `runBlocking`-in-the-listener pattern do not fit a multi-second LLM turn.

---

## 2. End-to-end flow (target design)

```
[Watch]  AskTile tap → AskActivity
            │  RecognizerIntent (system mic UI) → question text (fires immediately)
            ▼  MessageClient → /jbrain/research/ask     (tiny payload)
[Phone]  ResearchRelayService.onMessageReceived
            │  fast ack → /jbrain/research/ack ("working…")   [watch shows "Thinking"]
            │  start FOREGROUND SERVICE (type=dataSync)
            │     └ POST /api/chat/ask  (Bearer full key, callTimeout 90s)
            │        forwards architect "tool" events → /jbrain/research/progress
            ▼  MessageClient → /jbrain/research/result  { answer, voice_answer, truncated }
[Watch]  AskActivity
            │  render full answer (ScalingLazyColumn, scrollable)
            ▼  if speech enabled & audio out available → TextToSpeech.speak(voice_answer)
               else → text + haptic (never silent)
[Server] POST /api/chat/ask  { text, conversation_id? }
            reuse/create "⌚ Ask" session conversation (bounded history, idle reset)
            architect.run(mode="research", location=None)
            concat token events → answer; strip markdown/wikilinks + cap → voice_answer
```

---

## 3. Decisions baked in (with rationale)

| # | Decision | Why |
|---|----------|-----|
| D1 | **New server endpoint `POST /api/chat/ask`** (non-streaming), NOT phone-side SSE parsing | Keeps Android dumb; lets the server clean/cap text for speech; reuses 100% of `architect.run`. |
| D2 | **Session "⌚ Ask" conversation with bounded context + idle reset**, `location=None` | *Decided: short follow-up memory.* Reuse one conversation so "what about last year?" works, but cap history sent to the model (e.g. last ~6 turns) and start a fresh conversation after an idle timeout (e.g. 30 min) to bound cost/latency and kill stale carryover. Watch has no location permission. |
| D3 | **Speech cleaning + length cap on the SERVER**, reusing `speechText()` from `web/src/pages/Chat.tsx:30-41` (port to Python) | One tested implementation; strips `[[wiki]]`, markdown, code; cap ~600 chars at a sentence boundary. Returns both `answer` (screen) and `voice_answer` (TTS). |
| D4 | **Two-phase async relay**, NOT a longer single ack: fast "received" ack + separate result message + progress pings | A 40s synchronous round trip over BT/doze is brittle. Decouples "delivered" from "answered". |
| D5 | **Phone runs the LLM turn in a foreground service**, not in `onMessageReceived` | `WearableListenerService` can be killed mid-call; `runBlocking` for 40s risks ANR/teardown/doze. |
| D6 | **Separate `AskActivity` + `AskTile`**, NOT a mode flag on `MainActivity` | The note `CaptureScreen` is tightly coupled to note `Result` types + `NoteQueue`; bolting on a 2nd state machine risks the offline-queue firing on questions. |
| D7 | **Text-first; speak is opt-in / best-effort**; respect DND/Theater; fall back to text+haptic | Privacy (health spoken to bystanders) + platform reality (no speaker on many watches; TTS engine only assured on Wear OS 4+). |
| D8 | **Ask path fails fast — never enqueues** (no `NoteQueue` reuse) | A stale question replayed and answered aloud hours later is wrong and billable. |
| D9 | **`/api/chat/ask` gated by the full access key (owner phone only)** for v0; documented | *Decided: owner phone only.* Research reads the entire brain; the per-person location key (write-only by design) must not escalate to brain-read. |
| D10 | **Validate `sourceNodeId`** before processing a research request | The research path is a read oracle; unlike notes, any paired node triggering it is a real exfiltration/denial-of-wallet risk. |
| D11 | **Fire immediately on STT result (no confirm step)** | *Decided: fastest path.* No transcript confirmation. Because there's no human gate against misheard/accidental queries, the **rate limit (D-rate) and watch tap-debounce become required, not optional** — they are the only backstop against cost/abuse. |

---

## 4. Critical fixes the reviews surfaced (don't miss)

- **🔴 Manifest path filter bug.** `android/app/src/main/AndroidManifest.xml:67` registers
  `NoteRelayService` with `pathPrefix="/jbrain/note"`. Messages on `/jbrain/research/*` will
  **never be delivered** until you add a `<data … pathPrefix="/jbrain/research"/>` (or a
  second service). Easy to miss → silent failure.
- **🔴 OkHttp timeout.** `NoteClient` uses `callTimeout(20s)` (`NoteClient.kt:27`); a research
  turn exceeds it and aborts while the server keeps burning tokens **and persists an answer
  the watch never sees**. The ask path needs its own client with `callTimeout ≈ 90s`.
- **🔴 `architect.run` is not side-effect-free.** It persists the user + final assistant turn
  and requires a real `conversation_id` (`architect.py:1952-1956, 2035-2039`). The endpoint
  must create a conversation row first (mirror `chat.py:30-35`).
- **🟠 Rate limiting.** `/api/chat` has none. Each tap = up to 8 LLM iterations / 60k-token
  budget. Add a per-day turn budget / min interval (reuse the `share.rate_limited` pattern)
  and debounce taps on the watch.
- **🟠 TTS is best-effort.** Engine only guaranteed on Wear OS 4+; ~10s cold-start after boot;
  init is async (`OnInitListener`) and **must** `shutdown()` (use `DisposableEffect`); detect
  audio output via `PackageManager.FEATURE_AUDIO_OUTPUT` / `AudioManager.getDevices`; set
  `AudioAttributes(USAGE_ASSISTANT)`; Theater/DND can mute it.
- **🟠 Never fail silently.** Define a result taxonomy mirroring `PhoneRelay.Result`:
  `Answered / AnsweredTextOnly / Timeout / PhoneError / TtsUnavailable / Truncated /
  Unreachable` — each with wrist status + logcat + `RelayLog`. "Produced an answer but
  couldn't deliver/speak it" is a first-class visible outcome.
- **🟡 Truncation must be announced.** Data Layer payload cap ~100KB; a truncated medical
  answer spoken without warning is misleading. Server sets a `truncated` flag; watch says
  "answer shortened — open phone for full".
- **🟡 Sanitize answer before TTS** (control chars / SSML) — treat answer text as untrusted
  (prompt-injection from notes can reach the audio channel).
- **🟡 STT confirmation.** Misheard health questions ("stop" vs "start my warfarin") answered
  aloud confidently is a real harm. Showing the transcript before running also kills
  accidental/double-tap queries.

**Confirmed safe (no action):** Research mode read-only is genuinely enforced — `_run_tool`
fails closed for non-mode tools (`architect.py:1796`) and `query_sql` uses a read-only
connection + authorizer that blocks `meta`/secrets (`sqlsafe.py:16-51`). `RecognizerIntent`
via `StartActivityForResult` needs no `RECORD_AUDIO` (proven by the existing note flow).

---

## 5. Work breakdown (parallelizable lanes)

### Lane A — Server (`server/`)
- `POST /api/chat/ask` in `routers/chat.py` (under existing `CurrentUser`): reuse/create the
  "⌚ Ask" session conversation (accept optional `conversation_id`; bound history; reset after
  idle), run `architect.run(mode="research", location=None)`, concat `token` events, map
  `error`→5xx. Return `{ answer, voice_answer, truncated, conversation_id }`.
- New `services/speech.py` `to_speech(md, cap=600)` ported from `Chat.tsx speechText()`.
- Rate limit / daily turn budget on the endpoint.
- Tests in `server/tests/test_api.py` (+ pure-logic test for the stripper like
  `test_wikilinks.py`): creates conversation, returns both fields, strips wiki/markdown,
  caps length, rejects empty (422), error→5xx.

### Lane B — Phone (`android/app/`)
- Add `/jbrain/research` to the manifest `<data>` path filter (or new service entry).
- `ResearchRelayService` (or extend `NoteRelayService`): validate `sourceNodeId`, fast-ack,
  launch a **foreground service** (`dataSync`; add `FOREGROUND_SERVICE_DATA_SYNC`).
- `ResearchClient` (own OkHttp, `callTimeout 90s`) → `/api/chat/ask`; forward progress.
- Mirror "never fail silently": notification + `RelayLog` + logcat.

### Lane C — Watch (`android/wear/`)
- `AskTile` (clone `CaptureTile.kt`, point at `AskActivity`, new label/strings) + manifest
  `<service>` entry; bump `versionCode`.
- `AskActivity` + Compose `idle→listening→thinking→speaking→error` (sealed state).
- Relay client: send `/jbrain/research/ask` (+ remembered `conversation_id`), await
  `/jbrain/research/result` (~60s), render progress pings; fail fast (no `NoteQueue`);
  debounce taps (required backstop, see D11).
- `TextToSpeech` holder via `DisposableEffect`; audio-output detection; replay button;
  scrollable text; text+haptic fallback. Opt-in speech setting.
- Strings for all states; `EXTRA_*` consts mirrored.

### Lane D — Integration / docs
- Keep relay path constants in sync with "must match" comments (mirror
  `PhoneRelay.kt:23-26` ↔ `NoteRelayService.kt:105-107`).
- README/ROADMAP note; build via `cd android && ./gradlew :app:assembleRelease` (embeds wear).

**Order:** A and the watch UI shell (C) can start immediately in parallel; B depends on A's
contract; final integration test needs A+B+C.

---

## 6. Decisions made (locked for v0)

1. **Auth/trust tier:** ✅ Owner phone only — `/api/chat/ask` behind the full access key (D9).
2. **Speech default:** ✅ Text-first, speak opt-in (D7).
3. **STT confirmation:** ✅ Fire immediately, no confirm step (D11) → rate-limit + tap-debounce
   are now **required backstops**.
4. **Conversation memory:** ✅ Short follow-up memory — one "⌚ Ask" session conversation with
   bounded history + idle reset (D2).
