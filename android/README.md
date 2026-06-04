# JBrain Android app (phone/tablet + embedded Wear OS watch app)

One Gradle project, two modules, **one install**:

- **`:app`** — the phone/tablet app. Logs this device's **location trail** to your
  JBrain in the background. Its whole UI is **Name**, **Server URL**, **Access key**
  (set by pasting a setup code), and an **on/off switch**.
- **`:wear`** — a tiny **Wear OS** app: tap the mic (or its watch-face tile), dictate,
  and the note is saved into JBrain.

The watch app is **embedded inside the phone APK** (`app/build.gradle.kts`:
`wearApp(project(":wear"))`), so installing the phone app is all you do — the Wear OS
companion delivers the watch app to a paired watch. There's no separate watch APK,
keystore, or CI workflow to maintain.

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

## Build & install

The phone app's server URL + key default from `local.properties` (copy
`local.properties.example`), but they're also editable on-device via a pasted **setup
code**, so a shared family build just needs each phone to paste its code.

```
cd android && ./gradlew :app:assembleRelease
```

The APK (with the watch app embedded) lands in `app/build/outputs/apk/release/`. Or let
CI build it — see `.github/workflows/android-apk.yml` (download the artifact and
sideload). Install it on your (and family) phones; pair a watch and the companion app
delivers the watch app.

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
    src/main/res/values/wear.xml      # advertises the jbrain_note_relay capability
  wear/                               # Wear OS watch app (namespace com.jbrain.watch)
    src/main/java/com/jbrain/watch/
      MainActivity.kt                 # Compose UI + speech-recognizer launcher
      PhoneRelay.kt                   # relays the transcript to the phone (no key)
      NoteQueue.kt                    # offline buffer, replayed on launch
      CaptureTile.kt, theme/Theme.kt
```
