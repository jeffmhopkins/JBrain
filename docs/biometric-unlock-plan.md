# Biometric (and PIN) App Lock for the PWA — Build Spec (v1, draft)

Status: **DRAFT FOR REVIEW.** No code written yet. This spec proposes a
client-only **app lock** that gates JBrain behind the device's biometric
(Touch ID / Face ID / Windows Hello / Android fingerprint) — and a PIN fallback —
and, crucially, **encrypts the access key (and the offline cache) at rest** so the
lock is real, not cosmetic. Grounding citations are `file:line` against the tree at
writing.

## 0. Goal & position

Make the access key **unreadable on disk** and require a biometric/PIN to bring it
back into memory, with an auto-lock policy so an unlocked-and-left device doesn't
stay open. No backend changes for the core feature.

### What we're protecting today

The access key is the entire trust boundary on the client. It is a ~256-bit
bearer token (`server/app/auth.py` generates it via `secrets.token_urlsafe(32)`)
stored **in cleartext** in `localStorage` and held in a module variable:

- `web/src/api.ts:3-5` — `let accessKey = localStorage.getItem("jbrain_access_key")`
- `web/src/api.ts:10-13` — `setAccessKey()` writes it to `localStorage`
- `web/src/api.ts:34-38` — every request sends `Authorization: Bearer <key>`

The offline cache is also cleartext on disk: the service worker `NetworkFirst`-caches
note/graph/search/medical/attachment-metadata responses into the `jbrain-api` Cache
Storage bucket (`web/vite.config.ts:47-66`).

So anyone who can open the installed PWA — or read `localStorage`/Cache Storage from
DevTools or the profile directory on a shared, unlocked device — has full access to
the brain. There is no lock, timeout, or PIN today (`web/src/App.tsx:96-110` simply
re-verifies the stored key on load).

### Decisions locked for v1 (owner-confirmed)

| Question | Decision |
|---|---|
| Security level | **Real encryption** — WebAuthn PRF derives a key that encrypts the access key; no plaintext key on disk |
| Fallback when PRF unsupported | **PIN fallback** — PBKDF2 over a user PIN derives the encryption key; works on every browser |
| Auto-lock policy | **Launch + idle timeout** — lock on cold start/reload AND after inactivity in the background |
| Idle timeout length | **Configurable** (default 5 min) |
| Offline cache at rest | **Encrypt the cache** — cached API responses are encrypted with the same vault key; offline reading survives the lock |
| Deliverable | **This design doc first**, for review before code |

### Position relative to existing systems

This is a **client-only** addition. It does not touch `server/app/auth.py`, the DB,
or the bearer-token protocol — the server keeps seeing a normal `Authorization`
header once the app is unlocked. The feature is entirely about *where the key lives
on the device* and *what it takes to surface it*.

The one real tension to state honestly: **JBrain's split-hosting mode** (PWA on
GitHub Pages talking to a remote API, `web/src/api.ts:6-8`, `web/vite.config.ts:8-10`)
means the WebAuthn Relying Party ID is the **Pages origin**, not the brain server.
That's fine (credentials are per-origin, which is what we want) but it must be
tested, and it means a credential enrolled on `pages.github.io/JBrain` is distinct
from one enrolled on a self-hosted `brain.example.com`. Each device + origin enrolls
independently. This is correct behavior, not a bug, but worth documenting for users.

## 1. Threat model — what this does and does not stop

**In scope (v1 stops these):**

- A shared/borrowed **unlocked** device where someone opens the installed PWA — they
  hit the lock screen, and the key/cache ciphertext on disk is useless without the
  biometric/PIN.
- Casual inspection of `localStorage` / Cache Storage / DevTools — only ciphertext is
  present.
- A device left open and walked away from — idle timeout re-locks it.

**Out of scope (be honest):**

- A **locked, stolen** device is the OS's job (full-disk encryption + device
  passcode). We layer on top; we don't replace it.
- A compromised device with a keylogger / malicious extension / root — it can scrape
  the key from memory after unlock. No web app can defend this.
- The **server-side** copy of the key (`/data/access-key.txt`) and the unencrypted
  SQLite DB — out of scope; that's the VM owner's full-disk-encryption responsibility.
- Memory: while unlocked, the key lives in the `api.ts` module variable in plaintext
  (it must, to sign requests). We minimize lifetime, not eliminate it.

## 2. Crypto design

### 2.1 The vault

A single **vault key** (AES-GCM 256) encrypts:

1. the access key, and
2. each cached API response body (§4).

The vault key is **never persisted**. It is re-derived on every unlock from either
the biometric (PRF) or the PIN, decrypts a stored **vault blob**, and lives only in
memory while unlocked.

Two-layer key wrapping (so PIN and biometric can both unlock the same vault, and so
re-enrolling biometric doesn't force re-encrypting the cache):

- A random **master key** `MK` (AES-GCM 256) is generated once at enrollment. `MK`
  encrypts the access key and the cache.
- `MK` is **wrapped** (encrypted) separately by each enrolled unlock method:
  - `wrap_prf = AESGCM(Kbio).encrypt(MK)` where `Kbio = HKDF(prf_output)`
  - `wrap_pin = AESGCM(Kpin).encrypt(MK)` where `Kpin = PBKDF2(pin, salt, iters)`
- Unlock = produce `Kbio` or `Kpin` → unwrap `MK` → decrypt the access key.

This means: enable biometric *and* PIN both; lose/rotate one without touching the
other; and the cache never needs re-encrypting when an unlock method changes.

### 2.2 Biometric path — WebAuthn + PRF extension

- **Enroll:** `navigator.credentials.create()` with `authenticatorSelection:
  { authenticatorAttachment: "platform", userVerification: "required",
  residentKey: "required" }` and the **`prf`** extension. Store the returned
  `credentialId`.
- **Unlock:** `navigator.credentials.get()` with `allowCredentials: [credentialId]`,
  `userVerification: "required"`, and `extensions.prf.eval.first = <fixed 32-byte
  salt>`. The biometric prompt fires; on success the authenticator returns a stable
  `prf.results.first` secret. `Kbio = HKDF-SHA256(prf_output, info="jbrain-vault")`.
- PRF gives a deterministic, high-entropy secret bound to that credential +
  user-verification, which is exactly what we need to wrap `MK`.

**Capability detection:** PRF is **not universal.** Solid on Chrome/Edge (Android +
desktop platform authenticators) and Safari/iOS **18+ / macOS Sequoia+**; absent on
older iOS. We must:
1. Feature-detect `PublicKeyCredential` and platform-authenticator availability
   (`isUserVerifyingPlatformAuthenticatorAvailable()`).
2. At enroll time, **verify PRF actually returned a value** (create can succeed while
   `prf.enabled` is false) — if it didn't, fall back to PIN and tell the user
   biometric isn't available here.

### 2.3 PIN path — PBKDF2

- 6+ digit PIN (configurable min length; allow alphanumeric passphrase too).
- `Kpin = PBKDF2-SHA256(pin, random 16-byte salt, ≥600k iterations)` →
  AES-GCM-wraps `MK`.
- **Throttling:** track failed attempts in the vault metadata; exponential backoff
  and, after N (e.g. 10) failures, offer "forget device" (wipe vault → re-enter full
  access key). PIN brute-force is the weak point, so the iteration count + throttle
  matter.

### 2.4 Storage layout (IndexedDB, new `jbrain-vault` DB)

```
vault: {
  v: 1,
  enrolled: { prf: bool, pin: bool },
  credentialId?: base64,            // WebAuthn
  prfSalt: base64,                  // fixed per-install salt for prf.eval.first
  wrap_prf?: { iv, ct },            // AESGCM(Kbio) -> MK
  pin?: { salt, iters, wrap: {iv, ct} },  // AESGCM(Kpin) -> MK
  accessKey: { iv, ct },           // AESGCM(MK) -> access key
  pinFailCount, lockoutUntil,
  idleTimeoutMs,                   // configurable; default 300000
}
```

The plaintext access key is **removed from `localStorage`** once enrolled
(`web/src/api.ts:12`). `localStorage` keeps only non-secret items (`jbrain_server`,
etc.).

## 3. Client architecture & code touch-points

### 3.1 New module: `web/src/lock.ts`

Owns all crypto + WebAuthn + IndexedDB:

- `isBiometricAvailable(): Promise<boolean>`
- `enrollBiometric()`, `enrollPin(pin)`, `removeMethod(...)`
- `unlockWithBiometric(): Promise<CryptoKey /*MK*/>`, `unlockWithPin(pin)`
- `isEnrolled()`, `wipeVault()`
- `encryptForCache(bytes, MK)`, `decryptFromCache(...)` (§4)
- Holds `MK` in a module variable while unlocked; `lock()` zeroes it.

### 3.2 `web/src/api.ts` changes

- `accessKey` is no longer seeded from `localStorage` when a vault exists; it's
  populated by the unlock flow (a new `setAccessKeyInMemory(key)` that does **not**
  persist plaintext when enrolled).
- `clearAccessKey()` also locks the vault (drops `MK`).
- Keep the existing cleartext path when no vault is enrolled (backward compatible —
  enrollment is strictly opt-in).

### 3.3 `web/src/App.tsx` (the `useAuth` gate)

Today: `loading ? … : !authed ? <KeyEntry/> : …` (`web/src/App.tsx:124-127`).

Add a lock state between "authed" and "show app":

- On mount, if a vault is enrolled, state starts **locked** → render new
  `<LockScreen/>` instead of the app, even though a (ciphertext) key exists.
- `LockScreen` calls `unlockWithBiometric()` (auto-prompt on mount) with a "Use PIN"
  affordance; on success it loads the access key into memory, runs the existing
  `/api/auth/verify` path, and flips to unlocked.
- New context fields: `locked`, `lock()`, `unlock()`, `lockEnrolled`.

### 3.4 Auto-lock controller

- **Launch/reload:** vault starts locked on every full page load (the `MK` is never
  persisted, so a reload is inherently locked — this is free).
- **Idle:** a timer (configurable, default 5 min) armed on
  `visibilitychange`→hidden / `blur`; on expiry → `lock()`. Reset on
  `visibilitychange`→visible + user activity. Because SW-claimed reloads happen
  (`web/src/main.tsx:22-28`), a reload naturally re-locks too.
- The timeout value is user-configurable (Settings, §5) and stored in the vault meta.

### 3.5 New UI

- `web/src/pages/LockScreen.tsx` — biometric auto-prompt, "Use PIN" entry, "Forget
  this device" escape hatch.
- Enrollment lives in **Settings/System** (`web/src/pages/SystemPage.tsx`): "App
  lock" section — enable biometric, set/Change PIN, idle-timeout selector, disable.
- First-run **offer** after `KeyEntry` connects: a one-time prompt "Lock this device
  with Face ID / a PIN?" (opt-in, dismissible).

## 4. Encrypting the offline cache

This is the bigger lift and the reason for the two-layer key. The existing Workbox
`runtimeCaching` (`web/vite.config.ts:47-66`) stores **plaintext** response bodies.
With the vault locked, those bodies are still readable on disk — so to make the lock
honest we must encrypt them.

**Approach:** replace the declarative `NetworkFirst` rule with a **custom Workbox
plugin** (in `web/public/push-sw.js`, which is already `importScripts`-ed into the
generated SW, `web/vite.config.ts:42-44`) that:

- On `cacheWillUpdate`: encrypt the response body with `MK` before storing.
- On `cachedResponseWillBeUsed`: decrypt before returning to the page.

**The hard part — the SW has no `MK`.** The service worker can't run WebAuthn and
doesn't share the page's memory. Options (to settle in review):

1. **Page-side encryption, SW stores opaque blobs (recommended).** Move offline
   caching out of Workbox's automatic layer: the page (which *has* `MK` while
   unlocked) reads through `api.ts`, and on success writes an **encrypted** copy into
   an IndexedDB-backed offline store via `lock.ts`. Offline reads go through the same
   path and decrypt in-page. The SW keeps doing the app-shell precache only. This
   keeps all crypto on the page where `MK` lives, at the cost of reimplementing the
   "serve last-seen notes offline" behavior in app code instead of Workbox.
2. **Hand `MK` to the SW via `postMessage` on unlock**, keep Workbox. Simpler to keep
   the existing caching, but it widens `MK`'s exposure (lives in the SW too) and the
   SW outlives the page — we'd have to message a `lock` to wipe it. Weaker.

**Recommendation:** Option 1 — keep `MK` exclusively in the page. Net effect: when
locked, the offline store holds only ciphertext; when unlocked, offline reading works
exactly as today. Attachment **downloads** are already excluded from cache
(`web/vite.config.ts:58`), so no large-blob crypto in v1.

This sub-feature is the natural seam if we ever phase the work: ship §2–3 (key lock)
first, add §4 (cache encryption) second. Owner chose to include it in v1.

## 5. Settings & recovery UX

- **Settings → App lock** (in `SystemPage.tsx`): toggle biometric, set/change/remove
  PIN, **idle timeout** picker (1 / 5 / 15 min / Never), "Lock now".
- **Recovery:** losing the credential/PIN is non-fatal — "Forget this device" wipes
  the vault and returns to `KeyEntry` (`web/src/pages/KeyEntry.tsx`), where the user
  re-pastes the access key (still on the server at `/data/access-key.txt`) and
  re-enrolls. No server-side credential registry, so nothing to clean up remotely.
- **Disconnect** (`web/src/App.tsx:89-94`) must also `wipeVault()` and clear the
  encrypted offline store, so a shared device truly leaves nothing behind.

## 6. Edge cases & risks

1. **PRF false-positive at enroll** — `create()` succeeds but PRF disabled. Verify a
   PRF value actually comes back before declaring biometric enrolled; else fall to
   PIN. (§2.2)
2. **iOS installed-PWA WebAuthn** — works on modern iOS but historically the flaky
   target; test on an actual home-screen install, both enroll and unlock, including
   after the SW-triggered reload (`web/src/main.tsx:22-28`).
3. **Split-hosting RP ID** — credential is bound to the Pages origin, not the brain
   server (§0). Document; ensure enrollment is offered per origin.
4. **SW update reload mid-session** — the auto-update reload re-locks (no persisted
   `MK`); ensure the unlock screen comes back cleanly and doesn't loop.
5. **Multiple tabs** — `MK` is per-document; locking one tab doesn't lock another.
   Acceptable for v1 (PWA is single-window); note it.
6. **PIN brute-force** — mitigated by PBKDF2 cost + throttle + lockout (§2.3); the
   weakest link, so don't allow trivially short PINs.
7. **Backward compatibility** — feature is opt-in; users who never enroll keep the
   exact current behavior (cleartext key in `localStorage`). No forced migration.
8. **`getAccessKey()` consumers** — audit all callers (push subscription, share
   links, etc.) to ensure they run only while unlocked.

## 7. Test matrix

| Surface | Enroll | Unlock (bio) | Unlock (PIN) | Idle lock | Offline read (locked→unlocked) | Recovery |
|---|---|---|---|---|---|---|
| Android Chrome (installed) | PRF | ✓ | ✓ | ✓ | ✓ | ✓ |
| Desktop Chrome/Edge (Hello/Touch ID) | PRF | ✓ | ✓ | ✓ | ✓ | ✓ |
| iOS 18+ Safari (installed PWA) | PRF | ✓ | ✓ | ✓ | ✓ | ✓ |
| iOS <18 / no PRF | PIN only | n/a | ✓ | ✓ | ✓ | ✓ |
| Split-hosting (Pages → remote API) | per-origin | ✓ | ✓ | ✓ | ✓ | ✓ |

## 8. Scope estimate

- §2–3 (vault, WebAuthn+PRF, PIN, lock screen, auto-lock, settings): ~1 day.
- §4 (encrypted offline store replacing Workbox runtime cache): ~1 day + testing.
- Cross-device verification (esp. iOS installed PWA): ~0.5 day.

**No backend changes.** Files touched: new `web/src/lock.ts`,
`web/src/pages/LockScreen.tsx`; edits to `web/src/api.ts`, `web/src/App.tsx`,
`web/src/pages/SystemPage.tsx`, `web/src/pages/KeyEntry.tsx` (post-connect offer),
`web/public/push-sw.js` + `web/vite.config.ts` (cache rework).

## 9. Open questions for review

1. Cache encryption: confirm **Option 1 (page-side store)** over postMessage-to-SW.
2. PIN policy: minimum length, allow alphanumeric passphrase, lockout threshold.
3. Should the first-run lock offer be **opt-in prompt** or just live silently in
   Settings?
4. Do we want a "require biometric on every resume" strict mode as an option, beyond
   the configurable idle timeout?
