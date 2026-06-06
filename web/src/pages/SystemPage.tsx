import { useEffect, useRef, useState } from "react";
import { get, post, u } from "../api";
import { useAuth } from "../App";
import { enablePush, pushSupported, pushSupportReason } from "../push";
import { useGeo, useTts, useTheme, type Theme } from "../hooks";
import UpdateConsole from "../components/UpdateConsole";
import ModelPicker from "../components/ModelPicker";
import OwnerSetting from "../components/OwnerSetting";
import MediaSettings from "../components/MediaSettings";

// "System" card: version + server update (reusing the same endpoints the
// header UpdateBanner reads), the opt-in location toggle (which previously had
// no UI at all), and Disconnect (moved off the top bar to keep it calm).
export default function SystemPage() {
  const { disconnect, pwaVersion, serverVersion, versionMismatch, vapidPublicKey } = useAuth();
  const geo = useGeo();
  const theme = useTheme();
  const [info, setInfo] = useState<any>(null);
  const [stats, setStats] = useState<any>(null);
  const [msg, setMsg] = useState("");
  // Live update lifecycle: requesting → restarting (server offline) → online; or
  // "queued" when the deploy needs a manual host step and won't restart itself.
  type UpStatus = "idle" | "requesting" | "restarting" | "online" | "queued" | "error" | "failed";
  const [up, setUp] = useState<UpStatus>("idle");
  const [copied, setCopied] = useState(false);
  const [console_, setConsole] = useState(false);   // live update-console modal open
  const pollRef = useRef<number | undefined>(undefined);

  // Host commands to diagnose a failed deploy (the API is down, so we can't fetch the
  // logs ourselves yet — phase 2). Offered with a Copy button.
  const DIAG_CMDS =
    "cd ~/JBrain\n" +
    "docker compose logs --tail=200 api\n\n" +
    "# Or reproduce the startup error directly against your data:\n" +
    "docker compose run --rm api python -c \"from app.db import init_db; init_db(); print('INIT OK')\"";
  const [notifMsg, setNotifMsg] = useState("");
  const [notifBusy, setNotifBusy] = useState(false);
  const [notifDelay, setNotifDelay] = useState(0);   // seconds before the test fires
  // Voice preview: speak a sample line via the device's built-in (on-device) TTS,
  // so you can hear and choose a voice. No server, no streaming — pure Web Speech API.
  const tts = useTts();
  const [showAllVoices, setShowAllVoices] = useState(false);
  // Most devices install dozens of voices across many languages; default the picker to
  // English only (the app's language) so it isn't a wall of unrelated voices. The
  // currently-selected voice is always kept in the list even if it's filtered out.
  const voiceList = (showAllVoices
    ? tts.voices
    : tts.voices.filter((v) => /^en\b/i.test(v.lang) || v.voiceURI === tts.voiceURI)
  ).slice().sort((a, b) => a.lang.localeCompare(b.lang) || a.name.localeCompare(b.name));

  useEffect(() => { get("/api/system/version").then(setInfo).catch(() => {}); }, []);
  useEffect(() => { get("/api/system/stats").then(setStats).catch(() => {}); }, []);
  useEffect(() => () => { if (pollRef.current) window.clearTimeout(pollRef.current); }, []);

  const ping = () =>
    fetch(u("/api/health"), { cache: "no-store" }).then((r) => r.ok).catch(() => false);

  async function doUpdate() {
    setUp("requesting"); setMsg("Triggering update…");
    let r: any;
    try {
      r = await post("/api/system/update");
    } catch {
      setUp("error"); setMsg("Couldn’t reach the server to start the update. Check the server logs.");
      return;
    }
    // Three deploy modes: a configured command (started) or the auto-updater
    // (scheduled+auto) WILL restart the server — watch for it. A bare "scheduled"
    // needs a manual host step (./update.sh), so the server won't drop on its own.
    const willRestart = !!r.started || (!!r.scheduled && !!r.auto);
    if (!willRestart) {
      setUp("queued");
      setMsg(r.message || "Update requested — run ./update.sh on the host to apply it, then reload.");
      return;
    }

    setMsg("Update triggered — waiting for the server to restart…");
    const startedAt = Date.now();
    const FAIL_AFTER = 90 * 1000;        // down this long after going offline → likely a failed deploy
    const HARD_CAP = 8 * 60 * 1000;      // stop polling after this (leave the last state shown)
    let sawDown = false;
    let downSince = 0;
    const poll = async () => {
      const ok = await ping();
      if (ok) {
        if (sawDown) {                   // went down then came back → success
          const ver = await get("/api/system/version").catch(() => null);
          setUp("online");
          setMsg(`Back online${ver?.current ? ` · ${ver.current}` : ""}. Reload the app to finish updating.`);
          return;
        }
        setUp("restarting"); setMsg("Update triggered — waiting for the server to restart…");
      } else {
        sawDown = true;
        if (!downSince) downSince = Date.now();
        if (Date.now() - downSince > FAIL_AFTER) {
          // Offline well past a normal restart → the new version probably failed to boot.
          // Keep polling (slower) so a host-side fix still flips this to "online".
          setUp("failed");
          setMsg("The server didn’t come back up after the update — the new version likely failed to start.");
        } else {
          setUp("restarting"); setMsg("Server offline — restarting…");
        }
      }
      if (Date.now() - startedAt > HARD_CAP) return;   // give up polling; leave failed/restarting visible
      const failing = sawDown && downSince && Date.now() - downSince > FAIL_AFTER;
      pollRef.current = window.setTimeout(poll, failing ? 5000 : 2000);
    };
    pollRef.current = window.setTimeout(poll, 3000);   // give it a moment to begin
  }

  async function copyDiag() {
    try { await navigator.clipboard.writeText(DIAG_CMDS); setCopied(true); setTimeout(() => setCopied(false), 1500); }
    catch { alert(DIAG_CMDS); }
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

  // Speak a sample line with the chosen voice + speed. Tapping while talking stops it.
  const VOICE_SAMPLE =
    "Hi, I’m JBrain. This is what my voice sounds like at this speed. " +
    "I can read your notes back to you, answer questions, and confirm changes out loud.";
  function previewVoice() {
    if (tts.speaking) tts.stop();
    else tts.speak(VOICE_SAMPLE);
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
            <button className="primary" onClick={() => { setConsole(true); doUpdate(); }} disabled={up === "requesting" || up === "restarting"}>
              {up === "requesting" || up === "restarting" ? "Updating…" : "Update server"}
            </button>
            <button className="ghost" onClick={() => setConsole(true)}>Live console</button>
          </div>
        ) : (
          <p className="muted" style={{ fontSize: 13, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
            <span>You’re on the latest server release{info?.current ? ` (${info.current})` : ""}.</span>
            <button className="ghost" onClick={() => setConsole(true)}>Live console</button>
          </p>
        )}
        {up !== "idle" && (
          <div className="update-status">
            <span className={"status-dot " + (up === "online" ? "ok" : up === "error" || up === "failed" ? "err" : up === "queued" ? "info" : "busy")} />
            <span style={{ fontSize: 13 }}>{msg}</span>
            {(up === "online" || up === "queued") && (
              <button className="primary" style={{ padding: "4px 12px" }} onClick={() => window.location.reload()}>
                Reload now
              </button>
            )}
          </div>
        )}
        {up === "failed" && (
          <div className="deploy-failed">
            <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
              Grab the logs on the host and paste them here (or to your AI) to debug or roll back:
            </p>
            <pre className="deploy-cmd">{DIAG_CMDS}</pre>
            <div className="row" style={{ gap: 8 }}>
              <button className="primary" style={{ padding: "4px 12px" }} onClick={copyDiag}>
                {copied ? "Copied ✓" : "Copy commands"}
              </button>
            </div>
            <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
              Still re-checking — if the server recovers, this switches to online automatically.
            </p>
          </div>
        )}
        {up === "idle" && msg && <p className="muted" style={{ fontSize: 13 }}>{msg}</p>}
      </div>

      {stats && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Maintenance</h3>
          <div className="maint-grid">
            <span className="maint-k">Storage</span>
            <span className="maint-v">
              {stats.storage.percent}% used · {fmtBytes(stats.storage.free)} free of {fmtBytes(stats.storage.total)} <span className="muted">(whole disk)</span>
              <span className="muted" style={{ display: "block", fontSize: 12 }}>
                JBrain: database {fmtBytes(stats.storage.db_bytes)} · {stats.storage.attachments_count} attachment{stats.storage.attachments_count === 1 ? "" : "s"}
                {stats.storage.tiles_bytes ? ` · map tiles ${fmtBytes(stats.storage.tiles_bytes)}` : ""}.
                The rest is the OS + Docker images.
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

      <OwnerSetting />

      <ModelPicker />

      <MediaSettings />

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Appearance</h3>
        <p className="muted" style={{ fontSize: 13 }}>
          Theme for this device. <strong>Sunlight</strong> maximises contrast for reading in direct sun.
        </p>
        <div className="seg" role="group" aria-label="Theme">
          {(["dark", "light", "sunlight"] as Theme[]).map((val) => (
            <button key={val} className={theme.theme === val ? "on" : ""}
                    onClick={() => theme.setTheme(val)} style={{ textTransform: "capitalize" }}>
              {val}
            </button>
          ))}
        </div>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Location stamping</h3>
        <p className="muted" style={{ fontSize: 13 }}>
          Attach your coordinates to new entries (opt-in, this device only). GPS is read
          ONLY at the moment you post — never continuously while the app is open.
        </p>
        <label className="row" style={{ gap: 8 }}>
          <input type="checkbox" style={{ width: "auto" }} checked={geo.enabled} onChange={geo.toggle} />
          Stamp new entries with my location
        </label>
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
        <h3 style={{ marginTop: 0 }}>Voice</h3>
        <p className="muted" style={{ fontSize: 13 }}>
          Pick a voice and speed, then hear a sample read aloud. Speech is generated on
          this device — nothing is sent to a server. Your choice is saved on this device.
        </p>
        {!tts.supported ? (
          <p className="muted" style={{ fontSize: 13 }}>This device’s browser doesn’t support speech.</p>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span style={{ fontSize: 13 }}>Voice</span>
              <select value={tts.voiceURI} onChange={(e) => tts.setVoice(e.target.value)}
                      style={{ fontSize: 13, padding: "6px 8px" }}>
                <option value="">Default voice</option>
                {voiceList.map((v) => (
                  <option key={v.voiceURI} value={v.voiceURI}>
                    {v.name} ({v.lang}){v.default ? " — default" : ""}
                  </option>
                ))}
              </select>
              {tts.voices.length === 0 && (
                <span className="muted" style={{ fontSize: 12 }}>
                  No installed voices found yet — your device may load them after a moment.
                </span>
              )}
            </label>
            <label className="row" style={{ gap: 8 }}>
              <input type="checkbox" style={{ width: "auto" }}
                     checked={showAllVoices} onChange={(e) => setShowAllVoices(e.target.checked)} />
              <span style={{ fontSize: 13 }}>Show all languages</span>
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span style={{ fontSize: 13 }}>Speed · {tts.rate.toFixed(1)}×</span>
              <input type="range" min={0.5} max={2} step={0.1} value={tts.rate}
                     onChange={(e) => tts.setRate(Number(e.target.value))} />
            </label>
            <div className="row" style={{ gap: 8 }}>
              <button className="ghost" onClick={previewVoice}>
                {tts.speaking ? "Stop" : "Hear sample"}
              </button>
            </div>
          </div>
        )}
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Connection</h3>
        <p className="muted" style={{ fontSize: 13 }}>
          Forget the access key on this device and return to the login screen.
        </p>
        <button className="ghost" onClick={disconnect}>Disconnect</button>
      </div>

      {console_ && <UpdateConsole onClose={() => setConsole(false)} />}
    </div>
  );
}
