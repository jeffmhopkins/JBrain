import { FormEvent, useState } from "react";
import { useAuth } from "../App";
import { getServer } from "../api";

export default function KeyEntry() {
  const { brainName, connect, pwaVersion } = useAuth();
  const [server, setServerInput] = useState(getServer());
  const [key, setKey] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!key.trim()) return;
    const srv = server.trim();
    // A browser blocks an https page calling an http server ("mixed content"),
    // which otherwise surfaces as an indistinguishable "couldn't reach" error.
    if (location.protocol === "https:" && /^http:\/\//i.test(srv)) {
      setError("This page is HTTPS but the server address is http:// — browsers block that. Use an https:// server.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await connect(key.trim(), srv);
    } catch (err: any) {
      setError(
        err?.status === 401
          ? "That access key was rejected. Check it and try again."
          : "Couldn't reach that server. Check the address (and that it's running).",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login card">
      <h1>{brainName}</h1>
      <p className="muted" style={{ textAlign: "center", marginTop: -8 }}>
        Connect this device to your brain
      </p>
      <form onSubmit={submit}>
        <label className="muted">Server address</label>
        <input
          value={server}
          onChange={(e) => setServerInput(e.target.value)}
          placeholder="https://brain.example.com — leave blank if this site is your server"
          autoCapitalize="none"
          autoCorrect="off"
        />
        <label className="muted" style={{ marginTop: 12, display: "block" }}>Access key</label>
        <textarea
          rows={3}
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder="Paste the access key from install (or /data/access-key.txt)…"
          style={{ fontFamily: "monospace" }}
        />
        {error && <p style={{ color: "var(--danger)" }}>{error}</p>}
        <button className="primary" type="submit" disabled={busy} style={{ width: "100%", marginTop: 16 }}>
          {busy ? "Connecting…" : "Connect"}
        </button>
      </form>
      <p className="muted" style={{ fontSize: 12, marginTop: 14 }}>
        Stored only on this device, sent over HTTPS with each request. App v{pwaVersion}.
      </p>
    </div>
  );
}
