import { useEffect, useState } from "react";
import { get, post } from "../api";
import { useAuth } from "../App";
import { useGeo } from "../hooks";

// "System" card: version + server update (reusing the same endpoints the
// header UpdateBanner reads), the opt-in location toggle (which previously had
// no UI at all), and Disconnect (moved off the top bar to keep it calm).
export default function SystemPage() {
  const { disconnect, pwaVersion, serverVersion, versionMismatch } = useAuth();
  const geo = useGeo();
  const [info, setInfo] = useState<any>(null);
  const [msg, setMsg] = useState("");

  useEffect(() => { get("/api/system/version").then(setInfo).catch(() => {}); }, []);

  async function doUpdate() {
    setMsg("Requesting update…");
    const r = await post("/api/system/update");
    setMsg(r.message || (r.started ? "Updating…" : "Update requested."));
  }

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
        <h3 style={{ marginTop: 0 }}>Connection</h3>
        <p className="muted" style={{ fontSize: 13 }}>
          Forget the access key on this device and return to the login screen.
        </p>
        <button className="ghost" onClick={disconnect}>Disconnect</button>
      </div>
    </div>
  );
}
