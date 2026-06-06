import { useEffect, useState } from "react";
import { getAutoAnalyze, setAutoAnalyze } from "../api";

// "Note analysis" settings card: the master switch for automatic AI processing. When on,
// every new note is analyzed at capture time (gist, salient facts, entities) instead of
// waiting for the nightly batch, AND its attachments auto-enrich — images get a vision
// summary, audio/video are transcribed — each folding back into the note's analysis as it
// lands. Off by default: nothing auto-processes (you can still run it per-attachment by
// hand). Backed by the analyze-new-note workflow's enabled flag (single source of truth).
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
      setMsg(s.enabled
        ? "On — new notes and their attachments are processed as you add them."
        : "Off — no auto-processing; note analysis runs nightly.");
    } catch (e: any) {
      setEnabled(prev);
      setMsg(e?.message || "Couldn’t save that setting.");
    } finally { setBusy(false); }
  }

  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>Note analysis</h3>
      <p className="muted" style={{ fontSize: 13 }}>
        Master switch for automatic AI processing. When on, each new note is analyzed as you add
        it — extracting its gist, salient facts and entities — and its attachments auto-enrich
        (images get a vision summary, audio &amp; video are transcribed, document text is read),
        each folding into the note&rsquo;s analysis. When off, none of this runs automatically (the
        note&rsquo;s own analysis still runs in the nightly batch, and you can analyze or transcribe
        any attachment by hand from its panel). Vision &amp; note analysis need an LLM key;
        transcription is local.
      </p>
      <label className="row" style={{ gap: 8 }}>
        <input type="checkbox" style={{ width: "auto" }} checked={enabled} disabled={busy}
               onChange={(e) => toggle(e.target.checked)} />
        Auto-analyze new notes &amp; auto-enrich their attachments
      </label>
      {msg && <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>{msg}</p>}
    </div>
  );
}
