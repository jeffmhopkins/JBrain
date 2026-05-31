import { useEffect, useState } from "react";
import { del, get, put } from "../api";

interface PromptRow {
  key: string;
  default: string;
  override: string | null;
  effective: string;
}

export default function PromptsPanel() {
  const [rows, setRows] = useState<PromptRow[]>([]);
  const [edited, setEdited] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState("");

  async function load() {
    const r = await get<PromptRow[]>("/api/prompts");
    setRows(r);
    setEdited({});
  }
  useEffect(() => { load(); }, []);

  async function save(key: string) {
    await put(`/api/prompts/${encodeURIComponent(key)}`, { value: edited[key] ?? "" });
    setMsg(`Saved ${key}.`);
    load();
  }
  async function reset(key: string) {
    await del(`/api/prompts/${encodeURIComponent(key)}`);
    setMsg(`Reset ${key} to default.`);
    load();
  }

  return (
    <div>
      <p className="muted" style={{ fontSize: 13 }}>
        Edit the prompts that drive the AI modes and workflow actions. Overrides are
        stored in your database (they survive updates); Reset returns to the shipped default.
      </p>
      {msg && <p className="muted" style={{ fontSize: 13 }}>{msg}</p>}
      {rows.map((r) => {
        const val = edited[r.key] ?? r.effective;
        const dirty = edited[r.key] !== undefined && edited[r.key] !== r.effective;
        return (
          <div className="card" key={r.key}>
            <div className="row">
              <code style={{ fontSize: 13 }}>{r.key}</code>
              {r.override !== null && <span className="badge" style={{ marginLeft: 8 }}>overridden</span>}
            </div>
            <textarea
              rows={Math.min(22, Math.max(6, Math.ceil(val.length / 50)))}
              value={val}
              onChange={(e) => setEdited((m) => ({ ...m, [r.key]: e.target.value }))}
              style={{ marginTop: 8, fontFamily: "monospace", fontSize: 13 }}
            />
            <div className="row" style={{ gap: 8, marginTop: 8 }}>
              <button className="primary" onClick={() => save(r.key)} disabled={!dirty}>Save</button>
              {r.override !== null && <button className="ghost" onClick={() => reset(r.key)}>Reset to default</button>}
            </div>
          </div>
        );
      })}
    </div>
  );
}
