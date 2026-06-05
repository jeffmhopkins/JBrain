import { useEffect, useState } from "react";
import { getMedicalDests, setMedicalDests } from "../api";
import { Icon } from "../components/Icon";

// Manage the Medical-mode destination picklist. Medical-mode captures file under
// notes/medical/<destination>/NN — a browsable folder per stay/category — and this is
// where the owner curates the destinations the composer offers. Opened from Advanced.
export default function MedicalPage() {
  const [names, setNames] = useState<string[]>([]);
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => { getMedicalDests().then((r) => setNames(r.names)).catch(() => {}); }, []);

  async function save(next: string[]) {
    setBusy(true);
    try { const r = await setMedicalDests(next); setNames(r.names); }
    catch (e: any) { alert(e?.message || "Couldn’t save destinations."); }
    finally { setBusy(false); }
  }
  function add() {
    const n = newName.trim();
    if (!n) return;
    setNewName("");
    save([...names, n]);
  }
  function remove(d: string) {
    if (!confirm(`Remove “${d}”? Notes already filed under notes/medical/${d} are untouched.`)) return;
    save(names.filter((x) => x !== d));
  }

  return (
    <div className="tool-body">
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        Medical mode (in the compose box) saves what you log straight to your wiki under{" "}
        <code>notes/medical/&lt;destination&gt;/</code> — one browsable folder per hospital stay or
        category. Curate the destinations the picker offers here; you can also add one inline while
        logging. General medical knowledge still synthesises into{" "}
        <code>kb/Reference/Medicine</code>, and anything personal into your own People article’s
        Health section.
      </p>

      <div className="people-add">
        <input placeholder="Add a destination… (e.g. “2026-03 Admission”)" value={newName}
               onChange={(e) => setNewName(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter") add(); }} />
        <button className="primary" disabled={busy || !newName.trim()} onClick={add}>Add</button>
      </div>

      <ul className="people-list">
        {names.length === 0 && <li className="muted" style={{ padding: "10px 2px", fontSize: 13 }}>No destinations yet — add one above.</li>}
        {names.map((d) => (
          <li key={d} className="person-row" style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Icon name="medical" size={16} />
            <span style={{ flex: 1 }}>{d}</span>
            <span className="muted" style={{ fontSize: 12 }}>notes/medical/{d}/</span>
            <button className="ghost" style={{ fontSize: 12 }} disabled={busy} onClick={() => remove(d)}>Remove</button>
          </li>
        ))}
      </ul>
    </div>
  );
}
