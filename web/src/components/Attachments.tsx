import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { analyzeAttachment, attachmentImageUrl, del, downloadAttachment, get, getAnalysisStatus, MAX_ATTACHMENT_BYTES, uploadAttachment } from "../api";
import { useAuth } from "../App";
import { Icon } from "./Icon";
import Modal from "./Modal";

interface Attachment {
  id: number; filename: string; mime: string; byte_size: number; created_at: string;
  analysis_status?: "none" | "pending" | "done" | "error" | null;
  analysis_detail?: string | null; analyzed_at?: string | null;
  analysis_md?: string | null;   // AI vision summary — shown here, not in the note body
}
type Viewing =
  | { kind: "image"; filename: string; url: string }
  | { kind: "md" | "text"; filename: string; text: string }
  | null;

function humanSize(n: number): string {
  return n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;
}
const isImage = (mime: string) => mime.startsWith("image/");

export default function Attachments({ slug, onNoteChanged }: { slug: string; onNoteChanged?: () => void }) {
  const { hasLlm } = useAuth();
  const [items, setItems] = useState<Attachment[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<{ name: string; pct: number; processing: boolean } | null>(null);
  const [viewing, setViewing] = useState<Viewing>(null);
  const [thumbs, setThumbs] = useState<Record<number, string>>({});   // attachment id -> object URL for inline image previews
  const [thumbErr, setThumbErr] = useState<Record<number, string>>({});   // why a preview failed (surfaced inline)
  const inputRef = useRef<HTMLInputElement>(null);
  const polling = useRef<Set<number>>(new Set());
  const alive = useRef(true);
  const thumbsRef = useRef(thumbs);
  thumbsRef.current = thumbs;

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

  // Inline image previews: fetch each image's bytes once (authed) into an object
  // URL. Kept until unmount, then revoked. Big images are reined in by CSS, not by
  // downloading less — attachments are capped at 10 MB, so this stays cheap.
  useEffect(() => () => { Object.values(thumbsRef.current).forEach((u) => URL.revokeObjectURL(u)); }, []);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      for (const a of items) {
        if (!isImage(a.mime) || thumbsRef.current[a.id]) continue;
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

  async function reanalyze(a: Attachment) {
    setError("");
    try {
      await analyzeAttachment(a.id, a.analysis_status === "done" || a.analysis_status === "error");
      setItems((xs) => xs.map((x) => (x.id === a.id ? { ...x, analysis_status: "pending" } : x)));
      startPoll(a.id);
    } catch (e: any) { setError(e.message || "Could not start analysis."); }
  }

  async function onFiles(files: FileList | null) {
    if (!files) return;
    setError(""); setBusy(true);
    const list = Array.from(files);
    try {
      for (let i = 0; i < list.length; i++) {
        const f = list[i];
        const label = f.name + (list.length > 1 ? ` (${i + 1}/${list.length})` : "");
        if (f.size > MAX_ATTACHMENT_BYTES) { setError(`${f.name} is over 10 MB.`); continue; }
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
        Any file up to 10 MB. Text, PDFs, and image metadata are searchable.{hasLlm ? " Images are summarized by AI automatically." : ""}
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
          {isImage(a.mime) && a.analysis_status === "pending" && (
            <div className="row" style={{ marginTop: 6, fontSize: 11 }}><span className="muted">⏳ Analyzing image…</span></div>
          )}
          {isImage(a.mime) && a.analysis_status === "error" && (
            <div className="row" style={{ marginTop: 6, fontSize: 11 }}>
              <span style={{ color: "var(--danger)" }} title={a.analysis_detail || ""}>⚠ AI analysis failed</span>
            </div>
          )}
          {isImage(a.mime) && a.analysis_md && (
            // The AI vision summary, read-only, collapsed by default (it can be long) — it
            // lives here on the image, not in the note body. Re-analyze (below) refreshes it.
            <details className="att-summary" style={{ marginTop: 6 }}>
              <summary className="muted" style={{ fontSize: 12, cursor: "pointer" }}>✦ AI summary</summary>
              <div className="md" style={{ fontSize: 13, marginTop: 4 }}>
                <ReactMarkdown>{a.analysis_md}</ReactMarkdown>
              </div>
            </details>
          )}
          <div className="row" style={{ marginTop: 6, gap: 6, flexWrap: "wrap" }}>
            <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => view(a)}>View</button>
            <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => dl(a)}>Download</button>
            {hasLlm && isImage(a.mime) && a.analysis_status !== "pending" && (
              <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => reanalyze(a)}>
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
