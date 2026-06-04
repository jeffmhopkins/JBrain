import { FormEvent, useState } from "react";
import { setOwner } from "../api";

// First-run gate: capture who owns this brain. The owner's name is what anchors every
// first-person note ("I", "my") to them and titles their personal knowledge-base page,
// so we ask for it once up front rather than leaving the wiki to invent a generic
// "Owner". Shown until the owner is named (or the step is skipped on this device).
export default function OwnerOnboarding({ brainName, onDone }: { brainName: string; onDone: () => void }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function save(e: FormEvent) {
    e.preventDefault();
    const n = name.trim();
    if (!n) return;
    setBusy(true);
    setError("");
    try {
      await setOwner(n);
      onDone();
    } catch (err: any) {
      setError(err?.message || "Couldn’t save that name.");
    } finally {
      setBusy(false);
    }
  }

  function skip() {
    // Remember the skip on this device so it doesn't nag every load; the owner can still
    // set their name any time in Settings.
    try { localStorage.setItem("jbrain_owner_setup_skipped", "1"); } catch { /* ignore */ }
    onDone();
  }

  return (
    <div className="login card">
      <h1>Welcome to {brainName}</h1>
      <p className="muted" style={{ textAlign: "center", marginTop: -8 }}>
        Who owns this brain?
      </p>
      <form onSubmit={save}>
        <label className="muted">Your name</label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Jeff Hopkins"
          autoFocus
        />
        <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
          This anchors everything you write in the first person (“I”, “my”) to you, and
          titles your personal page in the knowledge base. You can change it later in
          Settings.
        </p>
        {error && <p style={{ color: "var(--danger)" }}>{error}</p>}
        <button className="primary" type="submit" disabled={busy} style={{ width: "100%", marginTop: 16 }}>
          {busy ? "Saving…" : "Save & continue"}
        </button>
      </form>
      <div style={{ textAlign: "center", marginTop: 14 }}>
        <button type="button" className="ghost" onClick={skip}>I’ll do this later →</button>
      </div>
    </div>
  );
}
