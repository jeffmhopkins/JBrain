import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { analyzeAttachment, attachmentImageUrl, attachmentMediaUrl, del, downloadAttachment, get, getAnalysisStatus, MAX_ATTACHMENT_BYTES, transcribeAttachment, uploadAttachment } from "../api";
import { useAuth } from "../App";
import { Icon } from "./Icon";
import Modal from "./Modal";

interface Attachment {
  id: number; filename: string; mime: string; byte_size: number; created_at: string;
  analysis_status?: "none" | "pending" | "done" | "error" | null;
  analysis_detail?: string | null; analyzed_at?: string | null;
  analysis_md?: string | null;   // AI vision summary (image) OR speech transcript (audio) — sidecar, not in the note body
}
type Viewing =
  | { kind: "image"; filename: string; url: string }
  | { kind: "md" | "text"; filename: string; text: string }
  | null;

function humanSize(n: number): string {
  return n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;
}
const isImage = (mime: string) => mime.startsWith("image/");
const AUDIO_EXTS = /\.(mp3|wav|m4a|aac|ogg|oga|opus|flac|aiff?|amr|wma|weba|caf|mka)$/i;
const isAudio = (mime: string, filename = "") => mime.startsWith("audio/") || AUDIO_EXTS.test(filename);
const VIDEO_EXTS = /\.(mp4|m4v|webm|mov|ogv|mkv|avi|3gp|mpe?g)$/i;
const isVideo = (mime: string, filename = "") => mime.startsWith("video/") || VIDEO_EXTS.test(filename);
const isMedia = (a: { mime: string; filename: string }) => isAudio(a.mime, a.filename) || isVideo(a.mime, a.filename);
// Audio AND video are transcribed (video via its audio track); both show transcript UI.
const isTranscribable = (a: { mime: string; filename: string }) => isMedia(a);
// Image vision summaries and audio/video transcripts share the analysis_* sidecar slot.
const isEnrichable = (a: { mime: string; filename: string }) => isImage(a.mime) || isMedia(a);
// Images are previewed by downloading their bytes into a data URL; skip that eager fetch for
// unusually large images (use "View" instead). Audio/video no longer download at all — they
// stream from a signed URL — so this cap is image-only now.
const IMAGE_PREVIEW_MAX = 25 * 1024 * 1024;

export default function Attachments({ slug, onNoteChanged }: { slug: string; onNoteChanged?: () => void }) {
  const { hasLlm } = useAuth();
  const [items, setItems] = useState<Attachment[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<{ name: string; pct: number; processing: boolean } | null>(null);
  const [viewing, setViewing] = useState<Viewing>(null);
  const [thumbs, setThumbs] = useState<Record<number, string>>({});   // attachment id -> object URL for inline image previews
  const [thumbErr, setThumbErr] = useState<Record<number, string>>({});   // why a preview failed (surfaced inline)
  const [mediaUrls, setMediaUrls] = useState<Record<number, string>>({});   // attachment id -> blob URL for the inline <audio>/<video> player
  const [mediaErr, setMediaErr] = useState<Record<number, string>>({});   // attachment id -> the player couldn't render (CSP/codec)
  const inputRef = useRef<HTMLInputElement>(null);
  const polling = useRef<Set<number>>(new Set());
  const alive = useRef(true);
  const thumbsRef = useRef(thumbs);
  thumbsRef.current = thumbs;
  const mediaRef = useRef(mediaUrls);
  mediaRef.current = mediaUrls;

  async function load() {
    try {
      const next: Attachment[] = await get(`/api/notes/${slug}/attachments`);
      setItems(next);
      // Resume polling anything the server still reports as pending (e.g. a run
      // that outlived a page navigation).
      for (const a of next) if (a.analysis_status === "pending") startPoll(a.id);
    } catch { /* ignore */ }
  }
  useEffect(() => { load(); }, [slug]);
  useEffect(() => () => { alive.current = false; }, []);

  // Inline image previews: fetch each image's bytes once (authed) into an object URL. Kept
  // until unmount, then revoked. Display size is reined in by CSS; we only skip the eager
  // fetch for unusually large images (use "View" instead) now that the cap is 100 MB.
  useEffect(() => () => { Object.values(thumbsRef.current).forEach((u) => URL.revokeObjectURL(u)); }, []);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      for (const a of items) {
        if (!isImage(a.mime) || thumbsRef.current[a.id] || a.byte_size > IMAGE_PREVIEW_MAX) continue;
        try {
          const url = await attachmentImageUrl(a.id, a.byte_size);
          if (cancelled) { URL.revokeObjectURL(url); return; }
          setThumbs((t) => ({ ...t, [a.id]: url }));
        } catch (e: any) {
          setThumbErr((m) => ({ ...m, [a.id]: e?.status ? `HTTP ${e.status}` : (e?.message || "error") }));
        }
      }
    })();
    return () => { cancelled = true; };
  }, [items]);
  useEffect(() => () => { if (viewing?.kind === "image") URL.revokeObjectURL(viewing.url); }, [viewing]);

  // Inline players: mint a short-lived signed, same-origin streaming URL and hand it straight
  // to the <audio>/<video> element. No blob download (so a big video isn't pulled into memory,
  // and seeking range-streams), and no blob: URL (so no `media-src` CSP dependency — the source
  // is 'self'). On failure the element's onError surfaces a clear message; Download always works.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      for (const a of items) {
        if (!isMedia(a) || mediaRef.current[a.id]) continue;
        try {
          const url = await attachmentMediaUrl(a.id);
          if (cancelled) return;
          if (url) setMediaUrls((u) => ({ ...u, [a.id]: url }));
        } catch { /* download button still works */ }
      }
    })();
    return () => { cancelled = true; };
  }, [items]);

  // Poll a single attachment until analysis settles, then refresh the list so the new
  // AI summary (now stored on the attachment, not the note body) shows in the panel.
  function startPoll(id: number) {
    if (polling.current.has(id)) return;
    polling.current.add(id);
    let tries = 0;
    const tick = async () => {
      if (!alive.current) return;
      try {
        const s = await getAnalysisStatus(id);
        if (s.status === "pending" && tries++ < 60) { setTimeout(tick, 2000); return; }
      } catch { /* fall through to cleanup */ }
      polling.current.delete(id);
      if (alive.current) await load();   // refresh the panel; the note body is unchanged now
    };
    setTimeout(tick, 1500);
  }

  // Kick (or re-kick) the per-type enrichment: vision summary for images, transcript for
  // audio. `force` re-runs a finished/errored one.
  async function enrich(a: Attachment) {
    setError("");
    const force = a.analysis_status === "done" || a.analysis_status === "error";
    try {
      await (isMedia(a) ? transcribeAttachment(a.id, force) : analyzeAttachment(a.id, force));
      setItems((xs) => xs.map((x) => (x.id === a.id ? { ...x, analysis_status: "pending" } : x)));
      startPoll(a.id);
    } catch (e: any) { setError(e.message || "Could not start."); }
  }

  async function onFiles(files: FileList | null) {
    if (!files) return;
    setError(""); setBusy(true);
    const list = Array.from(files);
    try {
      for (let i = 0; i < list.length; i++) {
        const f = list[i];
        const label = f.name + (list.length > 1 ? ` (${i + 1}/${list.length})` : "");
        if (f.size > MAX_ATTACHMENT_BYTES) { setError(`${f.name} is over 100 MB.`); continue; }
        setProgress({ name: label, pct: 0, processing: false });
        try {
          // Images auto-analyze server-side; the response carries analysis.status.
          const res: any = await uploadAttachment(slug, f, (pct) =>
            setProgress((p) => (p ? { ...p, pct, processing: pct >= 100 } : p)));
          await load();   // show each file as it lands
          if (res?.analysis?.status === "pending" && res?.id) startPoll(res.id);
        } catch (e: any) {
          setError(`${f.name}: ${e.message || "upload failed"}`);
        }
      }
    } finally { setProgress(null); setBusy(false); }
  }

  function attErr(e: any): string {
    return e?.status ? `HTTP ${e.status}` : (e?.message || "error");
  }

  async function view(a: Attachment) {
    try {
      if (a.mime.startsWith("image/")) {
        setViewing({ kind: "image", filename: a.filename, url: await attachmentImageUrl(a.id, a.byte_size) });
      } else if (a.mime.includes("markdown") || a.mime.startsWith("text/")) {
        const full = await get(`/api/attachments/${a.id}`);
        setViewing({ kind: a.mime.includes("markdown") ? "md" : "text", filename: a.filename, text: full.content_text || "(empty)" });
      } else {
        await downloadAttachment(a.id, a.filename);  // no inline preview — just download
      }
    } catch (e: any) { setError(`Couldn’t open “${a.filename}” — ${attErr(e)}`); }
  }

  async function dl(a: Attachment) {
    try { await downloadAttachment(a.id, a.filename); }
    catch (e: any) { setError(`Download failed for “${a.filename}” — ${attErr(e)}`); }
  }

  async function remove(a: Attachment) {
    if (confirm(`Delete attachment “${a.filename}”?`)) { await del(`/api/attachments/${a.id}`); load(); onNoteChanged?.(); }
  }

  return (
    <div onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); onFiles(e.dataTransfer.files); }}>
      <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
        <input ref={inputRef} type="file" multiple style={{ display: "none" }} onChange={(e) => onFiles(e.target.files)} />
        <button className="ghost" onClick={() => inputRef.current?.click()} disabled={busy}>
          {busy ? "Uploading…" : "+ Attach file"}
        </button>
      </div>
      <p className="muted" style={{ fontSize: 11, margin: "6px 0" }}>
        Any file up to 100 MB. Text, PDFs, and image metadata are searchable; audio &amp; video play inline and are transcribed locally (no API key).{hasLlm ? " Images are summarized by AI automatically." : ""}
      </p>
      {progress && (
        <div className="upload-progress">
          <div className="row" style={{ fontSize: 12 }}>
            <span>{progress.processing ? "Processing" : "Uploading"} {progress.name}…</span>
            <span className="spacer" />
            {!progress.processing && <span className="muted">{progress.pct}%</span>}
          </div>
          <div className="progress-track">
            <div className={"progress-fill" + (progress.processing ? " processing" : "")}
                 style={{ width: progress.processing ? "100%" : `${progress.pct}%` }} />
          </div>
        </div>
      )}
      {error && <p style={{ color: "var(--danger)", fontSize: 12 }}>{error}</p>}

      {items.map((a) => (
        <div key={a.id} className="list-item">
          <div className="row">
            <span><Icon name="clip" size={15} /> {a.filename}</span>
            <span className="spacer" />
            <span className="muted" style={{ fontSize: 11 }}>{humanSize(a.byte_size)}</span>
          </div>
          {isImage(a.mime) && thumbs[a.id] && (
            // No loading="lazy": these are in-memory blob: URLs and the section sits
            // below the fold — lazy + off-screen blob images render broken on mobile.
            <img src={thumbs[a.id]} alt={a.filename} className="att-thumb"
                 title="Click to view full size" onClick={() => view(a)}
                 onError={() => {
                   // Decode failed despite type/size checks — drop the broken <img>
                   // and surface the reason (the error line was hidden behind it before).
                   setThumbs((t) => { const n = { ...t }; if (n[a.id]) URL.revokeObjectURL(n[a.id]); delete n[a.id]; return n; });
                   setThumbErr((m) => ({ ...m, [a.id]: "the downloaded bytes didn’t decode as an image" }));
                 }} />
          )}
          {isImage(a.mime) && thumbErr[a.id] && !thumbs[a.id] && (
            <div style={{ fontSize: 11, color: "var(--danger)", margin: "6px 0 2px" }}>
              Couldn’t show image — {thumbErr[a.id]}
            </div>
          )}
          {isVideo(a.mime, a.filename) && mediaUrls[a.id] && !mediaErr[a.id] && (
            // eslint-disable-next-line jsx-a11y/media-has-caption -- user-uploaded media.
            <video controls src={mediaUrls[a.id]} className="att-video" style={{ width: "100%", marginTop: 8, borderRadius: 8 }}
                   onError={() => setMediaErr((m) => ({ ...m, [a.id]: "playback" }))} />
          )}
          {isAudio(a.mime, a.filename) && !isVideo(a.mime, a.filename) && mediaUrls[a.id] && !mediaErr[a.id] && (
            // eslint-disable-next-line jsx-a11y/media-has-caption -- user-uploaded media; the
            // transcript below is the caption.
            <audio controls src={mediaUrls[a.id]} style={{ width: "100%", marginTop: 8 }}
                   onError={() => setMediaErr((m) => ({ ...m, [a.id]: "playback" }))} />
          )}
          {isMedia(a) && mediaErr[a.id] && (
            // Same-origin streaming, so it isn't a CSP problem — the browser just can't decode
            // this codec inline (e.g. HEVC/H.265 from some phones). Download → native player works.
            <div style={{ fontSize: 11, color: "var(--danger)", margin: "8px 0 2px" }}>
              Can’t play this {isVideo(a.mime, a.filename) ? "video" : "audio"} inline (unsupported codec) — use Download to play it.
            </div>
          )}
          {isEnrichable(a) && a.analysis_status === "pending" && (
            <div className="row" style={{ marginTop: 6, fontSize: 11 }}>
              <span className="muted">{isTranscribable(a) ? "⏳ Transcribing…" : "⏳ Analyzing image…"}</span>
            </div>
          )}
          {isEnrichable(a) && a.analysis_status === "error" && (
            <div className="row" style={{ marginTop: 6, fontSize: 11 }}>
              <span style={{ color: "var(--danger)" }} title={a.analysis_detail || ""}>
                {isTranscribable(a) ? "⚠ Transcription failed" : "⚠ AI analysis failed"}
              </span>
            </div>
          )}
          {isEnrichable(a) && a.analysis_md && (
            // The vision summary (image) or transcript (audio/video), read-only, collapsed by
            // default (it can be long) — a sidecar on the attachment, not in the note body.
            // The enrich button (below) refreshes it.
            <details className="att-summary" style={{ marginTop: 6 }}>
              <summary className="muted" style={{ fontSize: 12, cursor: "pointer" }}>
                {isTranscribable(a) ? "📝 Transcript" : "✦ AI summary"}
              </summary>
              <div className="md" style={{ fontSize: 13, marginTop: 4 }}>
                <ReactMarkdown>{a.analysis_md}</ReactMarkdown>
              </div>
            </details>
          )}
          <div className="row" style={{ marginTop: 6, gap: 6, flexWrap: "wrap" }}>
            <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => view(a)}>View</button>
            <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => dl(a)}>Download</button>
            {isTranscribable(a) && a.analysis_status !== "pending" && (
              // Local transcription — no LLM key required, so not gated on hasLlm.
              <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => enrich(a)}>
                {a.analysis_status === "done" || a.analysis_status === "error" ? "Re-transcribe" : "Transcribe"}
              </button>
            )}
            {hasLlm && isImage(a.mime) && a.analysis_status !== "pending" && (
              <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => enrich(a)}>
                {a.analysis_status === "done" || a.analysis_status === "error" ? "Re-analyze" : "Analyze with AI"}
              </button>
            )}
            <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => remove(a)}>Delete</button>
          </div>
        </div>
      ))}

      {viewing && (
        <Modal title={viewing.filename} onClose={() => setViewing(null)}>
          {viewing.kind === "image"
            ? <img src={viewing.url} alt={viewing.filename} style={{ maxWidth: "100%", borderRadius: 8 }} />
            : viewing.kind === "md"
              ? <div className="md"><ReactMarkdown>{viewing.text}</ReactMarkdown></div>
              : <pre style={{ whiteSpace: "pre-wrap" }}>{viewing.text}</pre>}
        </Modal>
      )}
    </div>
  );
}
