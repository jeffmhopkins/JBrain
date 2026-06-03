# JBrain Tracker (Android phone app)

A tiny, single-purpose Android app: it logs this phone's **location trail** to your
JBrain in the background and does nothing else. The whole UI is four things —
**Name**, **Server URL**, **Access key**, and an **on/off switch**.

It's the phone counterpart to the `wear/` watch app, built so you can install it on
your own and your **family's phones** and see everyone's trail in your JBrain (each
device's fixes carry its **Name** as the `source`, so they stay distinct on the map).

## How it works

A foreground service uses `FusedLocationProvider` to get a fix once you've moved
~100 m (as often as every 30 s while moving, ~hourly when still). Fixes are buffered
on-device and flushed in batches to:

```
POST https://<your-server>/api/locations/bulk
Authorization: Bearer <access-key>
{ "points": [ { "lat":…, "lon":…, "accuracy_m":…, "recorded_at":"…Z", "source":"<Name>" }, … ] }
```

The **server** decides which fixes to keep (≥100 m moved OR ≥60 min elapsed), so the
app just forwards everything and offline bursts dedupe correctly. Nothing is lost with
no signal — the buffer replays when connectivity returns. No JBrain server changes are
needed beyond the `/api/locations/bulk` endpoint (already shipped).

## Build & install

1. **Configure** — copy `local.properties.example` to `local.properties` and set
   `JBRAIN_DOMAIN` + `JBRAIN_KEY`. These become the *prefilled defaults* in the app, so
   a family member just installs and types a Name.
2. **Build** — open the `android/` folder in Android Studio and Run on your phone, or
   build an APK:
   ```
   cd android && ./gradlew :app:assembleRelease
   ```
   The APK lands in `app/build/outputs/apk/release/`. Or let CI build it — see
   `.github/workflows/android-apk.yml` (download the artifact and sideload).
3. **Install on family phones** — send them the APK; they enable "install unknown
   apps", open it, set a **Name**, and flip the switch.

## On-device setup (per phone)

- Open the app → set **Name** (e.g. "Mom's Pixel"). Server URL + key are prefilled
  from the build; override them if needed.
- Flip **Track my location** on → grant location, then choose **Allow all the time**
  (Android 11+ asks for background separately) and allow notifications.
- For reliability, also set this app to **unrestricted battery** in system settings, so
  Android (and aggressive OEM skins) don't pause it.

## Permissions

`ACCESS_FINE/COARSE_LOCATION` + `ACCESS_BACKGROUND_LOCATION` (to keep logging when the
app is closed), a `location` foreground service (mandatory persistent notification),
`POST_NOTIFICATIONS` (Android 13+), and `RECEIVE_BOOT_COMPLETED` to resume after a
reboot. Sideloaded personal builds skip Play Store review, so the background-location
justification process doesn't apply.
