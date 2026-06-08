# JBrain Android app (phone/tablet + embedded Wear OS watch app)

One Gradle project, two modules, **one install**:

- **`:app`** — the phone/tablet app. Logs this device's **location trail** to your
  JBrain in the background. Its whole UI is **Name**, **Server URL**, **Access key**
  (set by pasting a setup code), and an **on/off switch**. It also ships two
  **home-screen widgets** — one-tap **Photo** and **Dictate** — that capture straight to
  your brain (see below).
- **`:wear`** — a tiny **Wear OS** app: tap the mic (or its watch-face tile), dictate,
  and the note is saved into JBrain.

The watch app is **embedded inside the phone APK** (`app/build.gradle.kts`:
`wearApp(project(":wear"))`), so on a paired watch the Wear OS companion can deliver it
straight from the phone install. CI **also** builds a **standalone watch APK**
(`jbrain-watch.apk`) signed with the same key, for the cases where auto-delivery
doesn't apply (Wear OS 3+ — see the note under *Build & install*) and you sideload it to
the watch directly. Same single keystore for both; no per-watch configuration.

## How the watch works (no keys on the watch)

The watch holds **no server URL and no access key**. When you dictate, it relays the
transcript to the phone over the **Wear Data Layer** (`MessageClient`, targeting the
phone via the `jbrain_note_relay` capability). The phone's `NoteRelayService` receives
it and POSTs to your server with the key it already has:

```
POST https://<your-server>/api/notes/entry
Authorization: Bearer <access-key>
{ "text": "<what you said>" }
```

So the watch never needs configuring or rebuilding when the key changes — that's the
whole point of the relay. If the phone is briefly unreachable, the watch buffers the
note and replays it on the next launch; if the phone is offline when it forwards, the
phone buffers and retries. The watch needs the phone reachable (Bluetooth/Wi-Fi); it no
longer does location.

> Both modules share `applicationId = com.jbrain.tracker` and the same signing key —
> required both for the Data Layer to route messages between them and for the watch APK
> to be embedded in and delivered by the phone app.

## How the phone tracker works

A foreground service tracks **smartly**: Activity Recognition detects "still ⇄ moving",
running continuous GPS only while you're **moving** and sleeping it when **still** (a
~20-min heartbeat keeps "last seen" fresh), waking the instant you move. Fixes are
buffered and flushed in batches to `POST /api/locations/bulk` (Bearer auth). The
**server** decides which fixes to keep (≥100 m moved OR ≥60 min elapsed), so offline
bursts dedupe correctly and nothing is lost with no signal.

## Home-screen capture widgets (one tap → into your brain)

The phone app provides two widgets you can drop on the home screen:

- **Photo** — tap → the system camera opens → take the shot → it uploads to JBrain and
  the server **auto-describes it** with the LLM. No app UI in between.
- **Dictate** — tap → the system speech recognizer opens → speak → the transcript is
  saved as a dated note (exactly like a watch dictation, but `source=user`).

A tap launches a tiny invisible `CaptureActivity` that fires the system camera /
recognizer, hands the result to a WorkManager job, and finishes. The upload runs in the
background and is **durable**: it only runs once there's a network, retries with backoff
if the server is unreachable, and survives the app closing, process death, and reboots —
so a capture taken with no signal (or before you've pasted a setup code) lands later
rather than being lost. You get a quiet notification with the result.

```
POST /api/notes/entry                       → { slug }          (the carrier note)
POST /api/notes/<slug>/attachments  (photo) → image + analyze=true
```

Because capture goes through the **system** camera/recognizer, the app needs **neither
`CAMERA` nor `RECORD_AUDIO`** — those apps own the grants. (Notifications use the
existing `POST_NOTIFICATIONS`.) The widgets use the same configured server URL + key as
the tracker, so pasting a setup code once is all the setup they need.

## Build & install

The phone app's server URL + key default from `local.properties` (copy
`local.properties.example`), but they're also editable on-device via a pasted **setup
code**, so a shared family build just needs each phone to paste its code.

```
cd android && ./gradlew :app:assembleRelease
```

The APK (with the watch app embedded) lands in `app/build/outputs/apk/release/`. Or let
CI build both — see `.github/workflows/android-apk.yml`, which produces a single
artifact (`jbrain-apks`) containing **`jbrain-tracker.apk`** (sideload to the phone) and
**`jbrain-watch.apk`** (sideload to the watch over ADB Wi-Fi). Install the phone APK on
your (and family) phones; pair a watch and the companion app delivers the embedded watch
app, or sideload the standalone watch APK directly (next note).

> **Note on auto-delivery:** the embedded-app mechanism reliably auto-installs the watch
> app from the *old Android Wear companion*. On **Wear OS 3+**, Google moved auto-delivery
> to the Play Store, so from a *sideloaded* phone APK a modern watch may not auto-install
> it — in that case install the watch app once via Android Studio or Wear Installer 2.
> Either way it's keyless, so you never rebuild or reconfigure it again.

## On-device setup (per phone)

- Open the app → **Paste setup code** (copy it in JBrain → People). This fills Name +
  Server + key.
- Flip **Track my location** on → grant location, then **Allow all the time** (Android
  11+ asks for background separately) and allow notifications.
- For reliability, set this app to **unrestricted battery** so Android doesn't pause it.

## Project layout

```
android/
  settings.gradle.kts                 # includes :app and :wear
  gradle/libs.versions.toml           # shared version catalog (phone + wear)
  app/                                # phone/tablet (com.jbrain.tracker)
    src/main/java/com/jbrain/tracker/
      MainActivity.kt, Settings.kt, SetupCode.kt
      LocationService.kt, LocationClient.kt, FixQueue.kt, Tracking.kt, …
      NoteClient.kt                   # POST a relayed note to /api/notes/entry
      NoteQueue.kt                    # offline buffer for relayed notes
      NoteRelayService.kt             # WearableListenerService — receives from the watch
      CaptureWidgets.kt               # Photo + Dictate home-screen AppWidgetProviders
      CaptureActivity.kt              # invisible trampoline: fires camera / recognizer
      CaptureClient.kt                # POST note + multipart attachment upload
      UploadWorker.kt                 # durable WorkManager upload (retries offline)
    src/main/res/values/wear.xml      # advertises the jbrain_note_relay capability
    src/main/res/xml/                 # appwidget-provider infos + FileProvider paths
  wear/                               # Wear OS watch app (namespace com.jbrain.watch)
    src/main/java/com/jbrain/watch/
      MainActivity.kt                 # Compose UI + speech-recognizer launcher
      PhoneRelay.kt                   # relays the transcript to the phone (no key)
      NoteQueue.kt                    # offline buffer, replayed on launch
      CaptureTile.kt, theme/Theme.kt
```
