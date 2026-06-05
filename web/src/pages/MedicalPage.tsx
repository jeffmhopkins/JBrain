import { useEffect, useState } from "react";
import { getMedicalDests, setMedicalDests, getMedicalOwner, setMedicalOwner } from "../api";
import { Icon } from "../components/Icon";

// Manage the Medical-mode destination picklist. Medical-mode captures file under
// notes/medical/<destination>/NN — a browsable folder per stay/category — and this is
// where the owner curates the destinations the composer offers. Opened from Advanced.
export default function MedicalPage() {
  const [names, setNames] = useState<string[]>([]);
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);
  const [ownerName, setOwnerName] = useState("");
  const [ownerDob, setOwnerDob] = useState("");
  const [ownerSaved, setOwnerSaved] = useState(false);

  useEffect(() => { getMedicalDests().then((r) => setNames(r.names)).catch(() => {}); }, []);
  useEffect(() => { getMedicalOwner().then((o) => { setOwnerName(o.name || ""); setOwnerDob(o.dob || ""); }).catch(() => {}); }, []);

  async function saveOwner() {
    setBusy(true);
    try { const o = await setMedicalOwner(ownerName.trim(), ownerDob.trim());
          setOwnerName(o.name || ""); setOwnerDob(o.dob || ""); setOwnerSaved(true); setTimeout(() => setOwnerSaved(false), 2000); }
    catch (e: any) { alert(e?.message || "Couldn’t save owner identity."); }
    finally { setBusy(false); }
  }

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

      <h3 style={{ marginTop: 24, marginBottom: 4 }}>Whose labs are these?</h3>
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        Set the patient these records belong to. When you import a lab PDF or photo, JBrain reads
        the document’s patient and <strong>warns you in red if the date of birth doesn’t match</strong> —
        so a family member’s or a stranger’s chart can’t be merged into your trends by accident.
        Optional; leave blank to skip the check.
      </p>
      <div className="people-add" style={{ gap: 8, flexWrap: "wrap" }}>
        <input placeholder="Name (as printed on results)" value={ownerName}
               onChange={(e) => setOwnerName(e.target.value)} style={{ flex: "2 1 180px" }} />
        <input placeholder="DOB — YYYY-MM-DD" value={ownerDob}
               onChange={(e) => setOwnerDob(e.target.value)} style={{ flex: "1 1 130px" }} />
        <button className="primary" disabled={busy} onClick={saveOwner}>
          {ownerSaved ? "Saved ✓" : "Save"}</button>
      </div>
    </div>
  );
}
