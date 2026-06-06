import { useEffect, useState } from "react";
import { getAutoAnalyze, setAutoAnalyze } from "../api";

// "Note analysis" settings card: when on, every new note's AI analysis (gist, salient
// facts, entities) is computed at capture time instead of waiting for the nightly batch,
// and an image attachment's vision summary folds back into that analysis as soon as it
// lands. Off by default — it calls a cheap model per new note. Backed by the
// analyze-new-note workflow's enabled flag (the single source of truth), read at runtime.
export default function AutoAnalyzeSetting() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  useEffect(() => { getAutoAnalyze().then((s) => setEnabled(s.enabled)).catch(() => {}); }, []);
  if (enabled === null) return null;

  async function toggle(next: boolean) {
    setBusy(true); setMsg("");
    const prev = enabled;
    setEnabled(next);   // optimistic
    try {
      const s = await setAutoAnalyze(next);
      setEnabled(s.enabled);
      setMsg(s.enabled ? "On — new notes are analyzed as you add them." : "Off — analysis runs nightly.");
    } catch (e: any) {
      setEnabled(prev);
      setMsg(e?.message || "Couldn’t save that setting.");
    } finally { setBusy(false); }
  }

  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>Note analysis</h3>
      <p className="muted" style={{ fontSize: 13 }}>
        Analyze each new note as soon as you add it — extracting its gist, salient facts and
        entities, and folding in attachment content (image summaries, transcripts, document text)
        as each attachment is processed. Off by default; when off, analysis runs in the nightly
        batch. Needs an LLM key.
      </p>
      <label className="row" style={{ gap: 8 }}>
        <input type="checkbox" style={{ width: "auto" }} checked={enabled} disabled={busy}
               onChange={(e) => toggle(e.target.checked)} />
        Auto-analyze new notes &amp; their attachments
      </label>
      {msg && <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>{msg}</p>}
    </div>
  );
}
