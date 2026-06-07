import { useEffect, useState } from "react";
import { getOwner, setOwner, Owner } from "../api";

// "Owner" settings card. The owner's name is the single identity the knowledge base
// resolves first-person notes to (and titles their kb/People/<name> page). Editing it
// here renames the default person — the same value onboarding captures.
export default function OwnerSetting() {
  const [owner, setOwnerState] = useState<Owner | null>(null);
  const [name, setName] = useState("");
  const [aliases, setAliases] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  async function load() {
    try {
      const o = await getOwner();
      setOwnerState(o);
      setName(o.is_set ? o.name : "");
      setAliases(o.aliases || "");
    } catch { /* ignore */ }
  }
  useEffect(() => { load(); }, []);

  const dirty = (o: Owner | null) =>
    !!name.trim() && (!o || name.trim() !== o.name || aliases.trim() !== (o.aliases || ""));

  async function save() {
    const n = name.trim();
    if (!dirty(owner)) return;
    setBusy(true);
    setMsg("");
    try {
      const o = await setOwner(n, aliases.trim());
      setOwnerState(o);
      setName(o.name);
      setAliases(o.aliases || "");
      setMsg("Saved. Re-analyze your notes and rebuild the knowledge base so existing entries and your page pick up the new name.");
    } catch (e: any) {
      setMsg(e?.message || "Couldn’t save that name.");
      load();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>Owner</h3>
      <p className="muted" style={{ fontSize: 13 }}>
        Your name anchors every note you write in the first person (“I”, “my”) to you, and
        titles your personal page in the knowledge base (kb/People/{owner?.is_set ? owner.name : "<name>"}).
        {owner && !owner.is_set && " It isn’t set yet, so the wiki falls back to a generic “Owner”."}
      </p>
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") save(); }}
          placeholder="e.g. Jeff Hopkins"
          style={{ flex: 1, minWidth: 180 }}
        />
        <button className="primary" onClick={save} disabled={busy || !dirty(owner)}>
          {busy ? "Saving…" : "Save"}
        </button>
      </div>
      <label className="muted" style={{ fontSize: 13, display: "block", marginTop: 10 }}>
        Also known as (optional)
        <input
          value={aliases}
          onChange={(e) => setAliases(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") save(); }}
          placeholder="e.g. Jeff, JMH"
          style={{ display: "block", width: "100%", marginTop: 4 }}
        />
        <span style={{ fontSize: 12 }}>
          Nicknames/variants (comma-separated) so the wiki links them to your page — e.g. “Jeff” → your “Jeffrey Hopkins” article.
        </span>
      </label>
      {msg && <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>{msg}</p>}
    </div>
  );
}
