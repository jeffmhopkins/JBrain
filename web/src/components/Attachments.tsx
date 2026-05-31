import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { attachmentObjectUrl, del, downloadAttachment, get, MAX_ATTACHMENT_BYTES, uploadAttachment } from "../api";
import { Icon } from "./Icon";

interface Attachment { id: number; filename: string; mime: string; byte_size: number; created_at: string; }
type Viewing =
  | { kind: "image"; filename: string; url: string }
  | { kind: "md" | "text"; filename: string; text: string }
  | null;

function humanSize(n: number): string {
  return n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export default function Attachments({ slug }: { slug: string }) {
  const [items, setItems] = useState<Attachment[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [viewing, setViewing] = useState<Viewing>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  async function load() {
    try { setItems(await get(`/api/notes/${slug}/attachments`)); } catch { /* ignore */ }
  }
  useEffect(() => { load(); }, [slug]);
  useEffect(() => () => { if (viewing?.kind === "image") URL.revokeObjectURL(viewing.url); }, [viewing]);

  async function onFiles(files: FileList | null) {
    if (!files) return;
    setError(""); setBusy(true);
    try {
      for (const f of Array.from(files)) {
        if (f.size > MAX_ATTACHMENT_BYTES) { setError(`${f.name} is over 10 MB.`); continue; }
        await uploadAttachment(slug, f);
      }
      await load();
    } catch (e: any) {
      setError(e.message || "Upload failed");
    } finally { setBusy(false); }
  }

  async function view(a: Attachment) {
    if (a.mime.startsWith("image/")) {
      setViewing({ kind: "image", filename: a.filename, url: await attachmentObjectUrl(a.id) });
    } else if (a.mime.includes("markdown") || a.mime.startsWith("text/")) {
      const full = await get(`/api/attachments/${a.id}`);
      setViewing({ kind: a.mime.includes("markdown") ? "md" : "text", filename: a.filename, text: full.content_text || "(empty)" });
    } else {
      downloadAttachment(a.id, a.filename);  // no inline preview — just download
    }
  }

  async function remove(a: Attachment) {
    if (confirm(`Delete attachment “${a.filename}”?`)) { await del(`/api/attachments/${a.id}`); load(); }
  }

  return (
    <div onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); onFiles(e.dataTransfer.files); }}>
      <div className="row">
        <input ref={inputRef} type="file" multiple style={{ display: "none" }} onChange={(e) => onFiles(e.target.files)} />
        <button className="ghost" onClick={() => inputRef.current?.click()} disabled={busy}>
          {busy ? "Uploading…" : "+ Attach file"}
        </button>
      </div>
      <p className="muted" style={{ fontSize: 11, margin: "6px 0" }}>Any file up to 10 MB. Text, PDFs, and image metadata are searchable.</p>
      {error && <p style={{ color: "var(--danger)", fontSize: 12 }}>{error}</p>}

      {items.map((a) => (
        <div key={a.id} className="list-item">
          <div className="row">
            <span><Icon name="clip" size={15} /> {a.filename}</span>
            <span className="spacer" />
            <span className="muted" style={{ fontSize: 11 }}>{humanSize(a.byte_size)}</span>
          </div>
          <div className="row" style={{ marginTop: 6, gap: 6 }}>
            <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => view(a)}>View</button>
            <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => downloadAttachment(a.id, a.filename)}>Download</button>
            <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => remove(a)}>Delete</button>
          </div>
        </div>
      ))}

      {viewing && (
        <div className="overlay" onClick={() => setViewing(null)}>
          <div className="overlay-card" onClick={(e) => e.stopPropagation()}>
            <div className="row">
              <strong>{viewing.filename}</strong>
              <span className="spacer" />
              <button className="ghost" onClick={() => setViewing(null)}>Close</button>
            </div>
            <div style={{ marginTop: 12 }}>
              {viewing.kind === "image"
                ? <img src={viewing.url} alt={viewing.filename} style={{ maxWidth: "100%", borderRadius: 8 }} />
                : viewing.kind === "md"
                  ? <div className="md"><ReactMarkdown>{viewing.text}</ReactMarkdown></div>
                  : <pre style={{ whiteSpace: "pre-wrap" }}>{viewing.text}</pre>}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
