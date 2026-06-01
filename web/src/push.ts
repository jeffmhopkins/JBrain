// Web Push opt-in. Subscribes this browser to push and registers it with the API
// so the owner gets a banner + icon badge for new review items even when the app
// is closed. Same-origin only (the SW + API must share an origin); demo/cross-
// origin/unsupported all no-op so the poll fallback stays in charge.
import { getServer, post } from "./api";
import { isDemo } from "./demo";

function urlB64ToUint8Array(b64: string): Uint8Array {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const base64 = (b64 + pad).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

export function pushSupported(): boolean {
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) return false;
  if (isDemo()) return false;
  // The service worker is registered for THIS page's origin, so push works only
  // when the API is same-origin. Empty server = same origin; a configured server
  // URL is fine as long as it resolves to this same origin.
  const srv = getServer();
  if (srv) {
    try { return new URL(srv).origin === window.location.origin; }
    catch { return false; }
  }
  return true;
}

// Subscribe (idempotent) and register with the API. Returns true if this browser
// is subscribed (so the bell can slow its poll). Best-effort: any failure leaves
// the poll as the fallback.
export async function enablePush(vapidPublicKey: string): Promise<boolean> {
  if (!pushSupported() || !vapidPublicKey) return false;
  if (Notification.permission !== "granted") return false;
  try {
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlB64ToUint8Array(vapidPublicKey) as BufferSource,
      });
    }
    const j: any = sub.toJSON();
    await post("/api/push/subscribe", { endpoint: sub.endpoint, keys: j.keys, ua: navigator.userAgent });
    return true;
  } catch {
    return false;
  }
}
