import { FormEvent, useState } from "react";
import { useAuth } from "../App";

export default function KeyEntry() {
  const { brainName, connect } = useAuth();
  const [key, setKey] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!key.trim()) return;
    setBusy(true);
    setError("");
    try {
      await connect(key.trim());
    } catch {
      setError("That access key was rejected. Check it and try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login card">
      <h1>{brainName}</h1>
      <p className="muted" style={{ textAlign: "center", marginTop: -8 }}>
        Paste your access key to connect this device
      </p>
      <form onSubmit={submit}>
        <textarea
          rows={3}
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder="Paste the access key from install (or /data/access-key.txt)…"
          autoFocus
          style={{ fontFamily: "monospace" }}
        />
        {error && <p style={{ color: "var(--danger)" }}>{error}</p>}
        <button className="primary" type="submit" disabled={busy} style={{ width: "100%", marginTop: 16 }}>
          {busy ? "Connecting…" : "Connect"}
        </button>
      </form>
      <p className="muted" style={{ fontSize: 12, marginTop: 14 }}>
        The key is stored only on this device and sent over HTTPS with each request.
      </p>
    </div>
  );
}
