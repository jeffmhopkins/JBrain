// End-to-end encryption for share-link chat. The server is a blind relay: it never sees
// the channel key or any plaintext. The channel key K (AES-256-GCM) is generated in the
// owner's browser and stored only as two WRAPPED copies:
//   • owner_wrap — K sealed under a key derived from the access key (owner-device recovery)
//   • guest_wrap — K sealed under the link-fragment secret [+ OTP] (recipient recovery)
// Everything uses the built-in WebCrypto SubtleCrypto — no dependency.

const te = new TextEncoder();
const td = new TextDecoder();
const PBKDF2_ITERS = 200_000;

function b64e(buf: ArrayBuffer | Uint8Array): string {
  const b = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
  return btoa(s);
}
function b64d(s: string): Uint8Array {
  const raw = atob(s);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}
function b64urlE(b: Uint8Array): string {
  return b64e(b).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function b64urlD(s: string): Uint8Array {
  return b64d(s.replace(/-/g, "+").replace(/_/g, "/"));
}

export function randomBytes(n: number): Uint8Array {
  const b = new Uint8Array(n);
  crypto.getRandomValues(b);
  return b;
}

export interface ChannelKey { key: CryptoKey; raw: Uint8Array; }

export async function generateChannelKey(): Promise<ChannelKey> {
  const raw = randomBytes(32);
  return { key: await importAesKey(raw), raw };
}

async function importAesKey(raw: Uint8Array): Promise<CryptoKey> {
  return crypto.subtle.importKey("raw", raw as BufferSource, "AES-GCM", false, ["encrypt", "decrypt"]);
}

async function deriveKEK(password: Uint8Array, salt: Uint8Array): Promise<CryptoKey> {
  const base = await crypto.subtle.importKey("raw", password as BufferSource, "PBKDF2", false, ["deriveKey"]);
  return crypto.subtle.deriveKey(
    { name: "PBKDF2", salt: salt as BufferSource, iterations: PBKDF2_ITERS, hash: "SHA-256" },
    base, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
}

// Seal the raw channel key under a password (the access key, or the fragment secret [+ OTP]).
export async function wrapKey(raw: Uint8Array, password: Uint8Array): Promise<string> {
  const salt = randomBytes(16), iv = randomBytes(12);
  const kek = await deriveKEK(password, salt);
  const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv: iv as BufferSource }, kek, raw as BufferSource);
  return JSON.stringify({ salt: b64e(salt), iv: b64e(iv), ct: b64e(ct) });
}

// Recover the channel key from a wrap. Throws if the password is wrong (GCM auth fails) —
// that's how a wrong OTP / tampered link is detected.
export async function unwrapKey(wrapped: string, password: Uint8Array): Promise<ChannelKey> {
  const w = JSON.parse(wrapped);
  const kek = await deriveKEK(password, b64d(w.salt));
  const buf = await crypto.subtle.decrypt({ name: "AES-GCM", iv: b64d(w.iv) as BufferSource }, kek, b64d(w.ct) as BufferSource);
  const raw = new Uint8Array(buf);
  return { key: await importAesKey(raw), raw };
}

export interface Sealed { iv: string; ct: string; }

export async function encryptJSON(key: CryptoKey, obj: unknown): Promise<Sealed> {
  const iv = randomBytes(12);
  const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv: iv as BufferSource }, key, te.encode(JSON.stringify(obj)));
  return { iv: b64e(iv), ct: b64e(ct) };
}
export async function decryptJSON<T = any>(key: CryptoKey, iv: string, ct: string): Promise<T> {
  const buf = await crypto.subtle.decrypt({ name: "AES-GCM", iv: b64d(iv) as BufferSource }, key, b64d(ct) as BufferSource);
  return JSON.parse(td.decode(buf)) as T;
}
export async function encryptBytes(key: CryptoKey, data: ArrayBuffer): Promise<{ iv: string; blob: Blob }> {
  const iv = randomBytes(12);
  const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv: iv as BufferSource }, key, data);
  return { iv: b64e(iv), blob: new Blob([ct]) };
}
export async function decryptBytes(key: CryptoKey, iv: string, data: ArrayBuffer): Promise<ArrayBuffer> {
  return crypto.subtle.decrypt({ name: "AES-GCM", iv: b64d(iv) as BufferSource }, key, data);
}

// The recipient's password = the link-fragment secret bytes, optionally salted with the OTP
// (delivered out-of-band). Mixing the OTP in here means a leaked link alone can't decrypt.
export function guestPassword(fragmentSecret: string, otp?: string): Uint8Array {
  const sec = b64urlD(fragmentSecret);
  const extra = te.encode(otp ? `|${otp.trim().toUpperCase()}` : "");
  const out = new Uint8Array(sec.length + extra.length);
  out.set(sec, 0); out.set(extra, sec.length);
  return out;
}
export const ownerPassword = (accessKey: string): Uint8Array => te.encode(accessKey);

export const newFragmentSecret = (): string => b64urlE(randomBytes(32));

// A short, human-deliverable code from an unambiguous alphabet (no 0/O/1/I).
export function newOtp(): string {
  const A = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const b = randomBytes(8);
  let s = "";
  for (let i = 0; i < 8; i++) s += A[b[i] % A.length];
  return s.slice(0, 4) + "-" + s.slice(4);
}

// Short Authentication String: an emoji fingerprint both sides derive from K, so they can
// confirm out-of-band that no one is in the middle.
const SAS = ["🐶", "🐱", "🦊", "🐻", "🐼", "🐨", "🦁", "🐮", "🐷", "🐸", "🐵", "🐔", "🦄", "🐝", "🦋", "🐢",
             "🐬", "🦉", "🌵", "🌻", "🍎", "🍌", "🍓", "🍔", "🍕", "⚽", "🎸", "🚀", "⭐", "🔥", "🌈", "🎲"];
export async function sasFingerprint(raw: Uint8Array): Promise<string> {
  const h = new Uint8Array(await crypto.subtle.digest("SHA-256", raw as BufferSource));
  return [0, 1, 2, 3, 4].map((i) => SAS[h[i] % SAS.length]).join(" ");
}

// E2EE needs SubtleCrypto, which browsers expose only in secure contexts (https / localhost).
export const cryptoAvailable = (): boolean =>
  typeof crypto !== "undefined" && !!crypto.subtle && !!window.isSecureContext;
