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
      <p className="muted" style={{ fontSize: 13, marginTop: 4 }}>
        Local speech-to-text (no API key) for audio &amp; video, and how video frames are sampled
        for the vision summary. Changes apply to new transcriptions immediately.
      </p>

      <div className="set-fields">
        <div className="set-field">
          <label className="set-label">Transcription model</label>
          <div className="set-control">
            <select value={s.audio_model} onChange={(e) => upd("audio_model", e.target.value)}>
              {s.audio_model_options.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
            <select value={s.audio_compute_type} onChange={(e) => upd("audio_compute_type", e.target.value)}>
              {s.compute_type_options.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
          <p className="set-sub">
            Whisper model + precision. Larger = more accurate but more RAM/slower; use <code>tiny</code> with{" "}
            <code>int8</code> on a tight (2&nbsp;GB) box.
          </p>
        </div>

        <div className="set-field">
          <label className="set-label">Frame sampling</label>
          <div className="set-control narrow">
            <input value={s.video_frame_interval} onChange={(e) => upd("video_frame_interval", e.target.value)}
                   placeholder="30s or 25%" />
          </div>
          <p className="set-sub">
            How often to grab a video frame for the vision summary — a time interval like <code>30s</code>, or a
            percent step like <code>25%</code> (a frame at 0/25/50/75/100%).
          </p>
        </div>

        <div className="set-field">
          <label className="set-label">Max frames per video</label>
          <div className="set-control narrow">
            <input type="number" min={0} value={s.video_frame_max}
                   onChange={(e) => upd("video_frame_max", e.target.value === "" ? 0 : Number(e.target.value))} />
          </div>
          <p className="set-sub">
            Cost cap — every frame rides in one vision call, so this is your ceiling. Set to <code>0</code> to turn
            off frame analysis (transcript only). Needs an LLM key.
          </p>
        </div>
      </div>

      <div className="row">
        <button className="primary" onClick={save} disabled={busy}>{busy ? "Saving…" : "Save"}</button>
      </div>
      {msg && <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>{msg}</p>}
    </div>
  );
}
