import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { del, get, uploadAttachment } from "../api";

interface Attachment {
  id: number;
  filename: string;
  mime: string;
  byte_size: number;
  created_at: string;
}

function humanSize(n: number): string {
  return n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`;
}

export default function Attachments({ slug }: { slug: string }) {
  const [items, setItems] = useState<Attachment[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [viewing, setViewing] = useState<{ filename: string; mime: string; text: string } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  async function load() {
    try { setItems(await get(`/api/notes/${slug}/attachments`)); } catch { /* ignore */ }
  }
  useEffect(() => { load(); }, [slug]);

  async function doUpload(file: File) {
    setError(""); setBusy(true);
    try {
      await uploadAttachment(slug, file);
      await load();
    } catch (e: any) {
      setError(e.message || "Upload failed");
    } finally {
      setBusy(false);
    }
  }

  async function onFiles(files: FileList | null) {
    if (!files) return;
    for (const f of Array.from(files)) await doUpload(f);
  }

  async function view(att: Attachment) {
    const full = await get(`/api/attachments/${att.id}`);
    setViewing({ filename: att.filename, mime: att.mime, text: full.content_text });
  }

  async function remove(att: Attachment) {
    if (!confirm(`Delete attachment “${att.filename}”?`)) return;
    await del(`/api/attachments/${att.id}`);
    await load();
  }

  return (
    <div
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => { e.preventDefault(); onFiles(e.dataTransfer.files); }}
    >
      <div className="row">
        <input
          ref={inputRef}
          type="file"
          accept=".txt,.md,.markdown"
          multiple
          style={{ display: "none" }}
          onChange={(e) => onFiles(e.target.files)}
        />
        <button className="ghost" onClick={() => inputRef.current?.click()} disabled={busy}>
          {busy ? "Uploading…" : "+ Attach .txt / .md"}
        </button>
      </div>
      <p className="muted" style={{ fontSize: 11, margin: "6px 0" }}>Drop files here, or use the button. Content is searchable.</p>
      {error && <p style={{ color: "var(--danger)", fontSize: 12 }}>{error}</p>}

      {items.map((a) => (
        <div key={a.id} className="list-item">
          <div className="row">
            <span>📎 {a.filename}</span>
            <span className="spacer" />
            <span className="muted" style={{ fontSize: 11 }}>{humanSize(a.byte_size)}</span>
          </div>
          <div className="row" style={{ marginTop: 6, gap: 6 }}>
            <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => view(a)}>View</button>
            <a className="badge" href={`/api/attachments/${a.id}/download`}>Download</a>
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
            <div className="md" style={{ marginTop: 12 }}>
              {viewing.mime.includes("markdown")
                ? <ReactMarkdown>{viewing.text}</ReactMarkdown>
                : <pre>{viewing.text}</pre>}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
