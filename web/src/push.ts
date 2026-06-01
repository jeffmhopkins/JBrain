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

// Empty string = supported; otherwise a short reason why push can't run here.
export function pushSupportReason(): string {
  if (!("serviceWorker" in navigator)) return "this browser has no service worker (open the installed app, not an in-app browser)";
  if (!("PushManager" in window)) return "no Push API (on iPhone, push needs the app installed to the Home Screen)";
  if (!("Notification" in window)) return "no Notification API";
  if (isDemo()) return "demo mode";
  const srv = getServer();
  if (srv) {
    try {
      const o = new URL(srv).origin;
      if (o !== window.location.origin) return `server ${o} is a different origin than ${window.location.origin}`;
    } catch { return "the configured server URL is invalid"; }
  }
  return "";
}

export function pushSupported(): boolean {
  return pushSupportReason() === "";
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
