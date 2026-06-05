import { useEffect, useState } from "react";
import { getMediaSettings, MediaSettings as MS, setMediaSettings } from "../api";

// "Media & transcription" settings card: the local Whisper model used for audio/video
// transcription, and the video frame-sampling cadence/cap for vision summaries. Backed by
// the DB `meta` KV (overriding the .env defaults) and read at runtime, so changes apply to
// new transcriptions immediately — no restart.
export default function MediaSettings() {
  const [s, setS] = useState<MS | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  useEffect(() => { getMediaSettings().then(setS).catch(() => {}); }, []);
  if (!s) return null;

  const upd = (k: keyof MS, v: any) => setS((p) => (p ? { ...p, [k]: v } : p));

  async function save() {
    if (!s) return;
    setBusy(true); setMsg("");
    try {
      const next = await setMediaSettings({
        audio_model: s.audio_model,
        audio_compute_type: s.audio_compute_type,
        video_frame_interval: s.video_frame_interval,
        video_frame_max: Number(s.video_frame_max),
      });
      setS(next);
      setMsg("Saved — new transcriptions use these right away. Re-transcribe an item to apply.");
    } catch (e: any) {
      setMsg(e?.message || "Couldn’t save those settings.");
    } finally { setBusy(false); }
  }

  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>Media &amp; transcription</h3>
      <p className="muted" style={{ fontSize: 13 }}>
        Local speech-to-text (no API key) for audio &amp; video, and how video frames are sampled
        for the vision summary. Applies to new transcriptions immediately.
      </p>

      <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center", marginBottom: 8 }}>
        <label style={{ minWidth: 130, fontSize: 13 }}>Transcription model</label>
        <select value={s.audio_model} onChange={(e) => upd("audio_model", e.target.value)} style={{ flex: 1, minWidth: 140 }}>
          {s.audio_model_options.map((m) => <option key={m} value={m}>{m}</option>)}
        </select>
        <select value={s.audio_compute_type} onChange={(e) => upd("audio_compute_type", e.target.value)} style={{ minWidth: 120 }}>
          {s.compute_type_options.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      </div>
      <p className="muted" style={{ fontSize: 11, margin: "0 0 10px" }}>
        Larger = more accurate but more RAM/slower. Use <code>tiny</code> on a tight (2&nbsp;GB) box.
      </p>

      <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center", marginBottom: 4 }}>
        <label style={{ minWidth: 130, fontSize: 13 }}>Video frames</label>
        <input value={s.video_frame_interval} onChange={(e) => upd("video_frame_interval", e.target.value)}
               placeholder="30s or 25%" style={{ width: 110 }} title="Cadence: a time interval like 30s, or a percent step like 25%" />
        <span className="muted" style={{ fontSize: 12 }}>every</span>
        <input type="number" min={0} value={s.video_frame_max}
               onChange={(e) => upd("video_frame_max", e.target.value === "" ? 0 : Number(e.target.value))}
               style={{ width: 80 }} title="Max frames per video = your cost ceiling (all frames ride in one vision call)" />
        <span className="muted" style={{ fontSize: 12 }}>max frames</span>
      </div>
      <p className="muted" style={{ fontSize: 11, margin: "0 0 10px" }}>
        Interval = cadence (e.g. <code>30s</code> or <code>25%</code>); max frames is the cost cap (all frames go in one
        vision call). Set max to <code>0</code> to turn off video frame analysis (transcript only). Needs an LLM key.
      </p>

      <div className="row">
        <button className="primary" onClick={save} disabled={busy}>{busy ? "Saving…" : "Save"}</button>
      </div>
      {msg && <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>{msg}</p>}
    </div>
  );
}
