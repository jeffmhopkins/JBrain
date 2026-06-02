# JBrain Watch — voice note capture for Wear OS

A tiny standalone **Wear OS** app (Pixel Watch and other Wear OS 3+ watches) that lets
you **speak a note and save it straight into JBrain**. Tap the app (or its tile), dictate,
and it POSTs the transcript to your server's existing capture endpoint
(`POST /api/notes/entry`). No JBrain server changes are needed.

> **Why a native app and not the PWA?** Wear OS can't install or run a PWA, and the
> Pixel Watch ships with no web browser — so a small native app is the only first-class
> way to get a one-tap voice-capture button on the wrist.

## What it does

- One big mic button → launches the watch's built-in speech recognizer → on a final
  transcript, POSTs `{ "text": "<what you said>" }` to `https://<your-domain>/api/notes/entry`
  with `Authorization: Bearer <your-key>`.
- The server files an untitled entry chronologically under `notes/daily/YYYY/MM/DD`, the
  same as the phone "make entry" flow.
- **One-tap tile**: a watch-face tile that opens the app and starts dictation immediately.
- **Offline-safe**: if the watch can't reach JBrain, the note is buffered on-device and
  replayed the next time you open the app/tile.

## One-time setup

1. Install **Android Studio** (Hedgehog or newer).
2. Open the `wear/` folder as a project. On first sync, Android Studio downloads Gradle
   8.10.2 (per `gradle/wrapper/`) and the Android SDK packages, and writes `sdk.dir` into
   `local.properties`.
3. Create your secrets file: copy `local.properties.example` → `local.properties` and set:
   ```properties
   JBRAIN_DOMAIN=https://your-brain.example.com
   JBRAIN_KEY=your-access-key            # the same key the JBrain PWA uses
   ```
   `local.properties` is gitignored — the key is baked into `BuildConfig` at build time and
   never committed, and you never have to type it on the watch.

   > Prefer not to use `local.properties`? You can also pass `JBRAIN_DOMAIN` / `JBRAIN_KEY`
   > as environment variables; the build reads those as a fallback.

## Validate the server first (optional but recommended)

Confirm your domain + key work before flashing the watch:

```bash
curl -X POST "https://<your-domain>/api/notes/entry" \
  -H "Authorization: Bearer <your-key>" \
  -H "Content-Type: application/json" \
  -d '{"text":"watch test"}'
```

Expect `200` with `{"id":...,"title":...,"slug":...}`, and the note should appear in your
JBrain PWA under today's daily entries.

## Build & install to the watch

1. On the watch: **Settings → Developer options → enable**, then **Wireless debugging → Pair
   new device** (or connect over ADB).
2. In Android Studio, pick the watch as the deployment target and **Run** the `app` module.
   (No Play Store needed — this is a personal sideload.)
3. Add the tile: long-press the watch face → **Tiles → +** → pick **JBrain Note** so capture
   is one tap from the wrist.

## Build the APK in CI (no Android Studio) → install with Wear Installer 2

The repo ships a GitHub Actions workflow (`.github/workflows/wear-apk.yml`) that
compiles the app and produces a ready-to-install **release APK** as a run artifact.

1. In the repo: **Settings → Secrets and variables → Actions** and add:
   - **Variable** `JBRAIN_DOMAIN` — `https://your-brain.example.com`
   - **Secret** `JBRAIN_KEY` — the same access key the JBrain PWA uses
   - *(Optional, for a stable signing key so updates install in place)* secrets
     `KEYSTORE_BASE64` (base64 of your `release.keystore`), `KEYSTORE_PASSWORD`,
     `KEY_ALIAS`, `KEY_PASSWORD`. Without these, the APK is signed with an ephemeral
     debug key — still installable, but you must uninstall before each update.
2. Run it: **Actions → Build Wear OS APK → Run workflow** (it also runs automatically
   on pushes that touch `wear/`).
3. Download the **`jbrain-watch-apk`** artifact from the finished run, unzip it to get
   `jbrain-watch.apk`, put it on your phone, and **install it with Wear Installer 2**
   (which pushes it to the paired watch).

> Generating a release keystore (one time), if you want stable signing:
> ```bash
> keytool -genkey -v -keystore release.keystore -alias jbrain \
>   -keyalg RSA -keysize 2048 -validity 10000
> base64 -w0 release.keystore   # paste output into the KEYSTORE_BASE64 secret
> ```

## How to use

- Open the app (or tap the tile) → speak → it shows **Saving…** then **Saved ✓** (with a
  short haptic). If offline, **Saved offline ✓** — it'll sync next launch.

## Background location trail (optional)

Tap **"Track my location"** in the app to log a location trail to JBrain even when the
app is closed. It runs a foreground service using `FusedLocationProvider` that emits a
fix **at most hourly, or as soon as you move 100 m**, and POSTs it to `/api/locations`
(bearer-authed). The **server** enforces the "≥100 m moved OR ≥60 min elapsed" rule, so
duplicates are dropped. Realities to expect:

- Android asks for location **twice** — first "while using", then **"Allow all the time"**
  (background) — both are required. On Android 13+ it also asks to post notifications.
- A persistent low-priority **"Logging your location trail"** notification is mandatory
  for a location foreground service; it stays until you toggle tracking off.
- "Hourly" is **best-effort** — Doze/battery optimization can stretch the interval; the
  100 m distance trigger is honored well. A phone tracks more reliably than a watch.
- It resumes after a reboot if it was on. The owner can read the trail at `GET /api/locations`.
- A **PWA cannot do this** (no background geolocation on the web) — that's why it lives
  in the native watch app.

## Project layout

```
wear/
  app/src/main/
    AndroidManifest.xml
    java/com/jbrain/watch/
      MainActivity.kt   # Compose UI + the speech-recognizer launcher
      NoteClient.kt     # the one OkHttp POST to /api/notes/entry
      NoteQueue.kt      # offline buffer (SharedPreferences), replayed on launch
      CaptureTile.kt    # one-tap tile that opens + auto-starts dictation
      theme/Theme.kt    # Wear Compose theme
    res/                # strings, icons, theme
  build.gradle.kts, settings.gradle.kts, gradle/  # Gradle + version catalog
  local.properties.example                         # copy to local.properties (gitignored)
```

## Notes

- **Connectivity**: the watch reaches JBrain over Wi-Fi, a tethered phone, or LTE. Your
  server is public HTTPS, so it's reachable; a LAN-only server would not be.
- **Recognizer**: uses the system speech recognizer present on Pixel Watch. If a specific
  watch lacks it, the app shows "No voice input."
- **Key handling**: `BuildConfig`-from-`local.properties` is ideal for a personal build.
  If you ever distribute the app, switch to an on-watch setup screen
  (`EncryptedSharedPreferences`) or pair the key from a phone companion via the Wear Data
  Layer API.
