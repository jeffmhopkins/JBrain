import { useEffect, useState } from "react";
import { get, post } from "../api";
import { useAuth } from "../App";
import { enablePush, pushSupported, pushSupportReason } from "../push";
import { useGeo } from "../hooks";

// "System" card: version + server update (reusing the same endpoints the
// header UpdateBanner reads), the opt-in location toggle (which previously had
// no UI at all), and Disconnect (moved off the top bar to keep it calm).
export default function SystemPage() {
  const { disconnect, pwaVersion, serverVersion, versionMismatch, vapidPublicKey } = useAuth();
  const geo = useGeo();
  const [info, setInfo] = useState<any>(null);
  const [stats, setStats] = useState<any>(null);
  const [msg, setMsg] = useState("");
  const [notifMsg, setNotifMsg] = useState("");
  const [notifBusy, setNotifBusy] = useState(false);
  const [notifDelay, setNotifDelay] = useState(0);   // seconds before the test fires

  useEffect(() => { get("/api/system/version").then(setInfo).catch(() => {}); }, []);
  useEffect(() => { get("/api/system/stats").then(setStats).catch(() => {}); }, []);

  async function doUpdate() {
    setMsg("Requesting update…");
    const r = await post("/api/system/update");
    setMsg(r.message || (r.started ? "Updating…" : "Update requested."));
  }

  // Subscribe THIS device (asking permission if needed), then have the server
  // push a test to all devices — so a banner should appear here within seconds.
  async function sendTestNotification() {
    setNotifMsg(""); setNotifBusy(true);
    try {
      if (!pushSupported()) {
        setNotifMsg(`Notifications aren’t available here: ${pushSupportReason()}.`);
        return;
      }
      if (Notification.permission === "default") await Notification.requestPermission();
      if (Notification.permission !== "granted") {
        setNotifMsg("Notifications are blocked — enable them for this app in your device settings, then try again.");
        return;
      }
      const ok = await enablePush(vapidPublicKey);
      if (!ok) { setNotifMsg("Couldn’t subscribe this device to push."); return; }
      const r = await post("/api/push/test", { delay: notifDelay });
      const devices = `${r.subscriptions} device${r.subscriptions === 1 ? "" : "s"}`;
      if (!r.vapid) setNotifMsg("Push isn’t configured on the server.");
      else if (!r.subscriptions) setNotifMsg("No subscribed devices yet — try again in a moment.");
      else if (r.delay) setNotifMsg(`Test scheduled in ${r.delay}s to ${devices}. Close the app now to test closed-app delivery.`);
      else if (r.failed && !r.sent) setNotifMsg(`Send failed (${devices}): ${r.errors?.[0] || "unknown error"}`);
      else setNotifMsg(`Sent to ${r.sent}/${r.subscriptions} device(s). ${r.failed ? r.failed + " failed." : "Watch for the banner."}`);
    } catch (e: any) {
      setNotifMsg(e?.message || "Couldn’t send the test.");
    } finally { setNotifBusy(false); }
  }

  const fmtBytes = (n: number) => {
    if (!n) return "0 B";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
  };
  const fmtUptime = (s: number) => {
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  };
  const fmtTok = (n: number) => n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : `${n}`;

  return (
    <div className="content">
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Version</h3>
        <p className="muted" style={{ fontSize: 13 }}>
          App v{pwaVersion}{serverVersion ? ` · server v${serverVersion}` : ""}.
          {versionMismatch && " Versions differ — update so they match."}
        </p>
        {info?.update_available ? (
          <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
            <span>Update available: {info.current} → {info.latest}</span>
            {info.release_url && <a href={info.release_url} target="_blank" rel="noreferrer">notes</a>}
            <button className="primary" onClick={doUpdate}>Update server</button>
          </div>
        ) : (
          <p className="muted" style={{ fontSize: 13 }}>
            You’re on the latest server release{info?.current ? ` (${info.current})` : ""}.
          </p>
        )}
        {msg && <p className="muted" style={{ fontSize: 13 }}>{msg}</p>}
      </div>

      {stats && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Maintenance</h3>
          <div className="maint-grid">
            <span className="maint-k">Storage</span>
            <span className="maint-v">
              {stats.storage.percent}% used · {fmtBytes(stats.storage.free)} free of {fmtBytes(stats.storage.total)}
              <span className="muted" style={{ display: "block", fontSize: 12 }}>
                Database {fmtBytes(stats.storage.db_bytes)} · Attachments {fmtBytes(stats.storage.attachments_bytes)} ({stats.storage.attachments_count})
              </span>
            </span>
            <span className="maint-k">Uptime</span>
            <span className="maint-v">{fmtUptime(stats.uptime_seconds)}
              <span className="muted" style={{ fontSize: 12 }}> · since {stats.started_at} UTC</span>
            </span>
            <span className="maint-k">LLM today</span>
            <span className="maint-v">
              ~${stats.tokens.today.cost.toFixed(2)}
              <span className="muted" style={{ fontSize: 12 }}> · {fmtTok(stats.tokens.today.input + stats.tokens.today.output)} tokens · {stats.tokens.today.calls} calls</span>
              {stats.tokens.today.cost >= stats.daily_warn_usd && (
                <span className="maint-warn"> ⚠ high — over ${stats.daily_warn_usd.toFixed(2)}/day</span>
              )}
            </span>
            <span className="maint-k">LLM this month</span>
            <span className="maint-v">
              ~${stats.tokens.month.cost.toFixed(2)}
              <span className="muted" style={{ fontSize: 12 }}> · {fmtTok(stats.tokens.month.input + stats.tokens.month.output)} tokens</span>
            </span>
          </div>
          <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
            Cost is an estimate (token counts are exact); month resets on the 1st in {stats.tokens.tz}. Won’t exactly match your provider bill.
          </p>
        </div>
      )}

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Location stamping</h3>
        <p className="muted" style={{ fontSize: 13 }}>
          Attach your coordinates to new entries (opt-in, this device only).
        </p>
        <label className="row" style={{ gap: 8 }}>
          <input type="checkbox" style={{ width: "auto" }} checked={geo.enabled} onChange={geo.toggle} />
          Stamp new entries with my location
        </label>
        {geo.enabled && geo.coords && (
          <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>Current: {geo.coords.lat}, {geo.coords.lon}</p>
        )}
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Notifications</h3>
        <p className="muted" style={{ fontSize: 13 }}>
          Get a banner + badge when someone proposes an edit to a shared note — even with the app closed. Send a test to confirm it reaches this device.
        </p>
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <button className="ghost" onClick={sendTestNotification} disabled={notifBusy}>
            {notifBusy ? "Sending…" : "Send test notification"}
          </button>
          <select value={notifDelay} onChange={(e) => setNotifDelay(Number(e.target.value))}
                  style={{ width: "auto", fontSize: 13, padding: "6px 8px" }} title="Delay before the test fires">
            <option value={0}>Immediately</option>
            <option value={10}>after 10s</option>
            <option value={30}>after 30s</option>
            <option value={60}>after 1 min</option>
          </select>
        </div>
        {notifMsg && <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>{notifMsg}</p>}
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Connection</h3>
        <p className="muted" style={{ fontSize: 13 }}>
          Forget the access key on this device and return to the login screen.
        </p>
        <button className="ghost" onClick={disconnect}>Disconnect</button>
      </div>
    </div>
  );
}
