import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Person, getPeople, addPerson, updatePerson, deletePerson } from "../api";

// People aren't accounts — they label/colour location trails (matched from a fix's
// `source` via aliases) and can link to a KB page. Exactly one is the default ("Me"),
// the catch-all for any unmatched source. Configured here, opened from Advanced.
export default function PeoplePage() {
  const [people, setPeople] = useState<Person[]>([]);
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);

  const load = () => getPeople().then(setPeople).catch(() => {});
  useEffect(() => { load(); }, []);

  // Optimistic local edit; commit the changed field to the server.
  function patch(id: number, field: keyof Person, value: any) {
    setPeople((ps) => ps.map((p) => (p.id === id ? { ...p, [field]: value } : p)));
  }
  async function commit(id: number, body: any, reload = false) {
    try { await updatePerson(id, body); if (reload) load(); }
    catch (e: any) { alert(e?.message || "Couldn't save."); load(); }
  }

  async function add() {
    const name = newName.trim();
    if (!name) return;
    setBusy(true);
    try { await addPerson({ name }); setNewName(""); load(); }
    catch (e: any) { alert(e?.message || "Couldn't add."); }
    finally { setBusy(false); }
  }

  async function makeDefault(id: number) { await commit(id, { is_default: true }, true); }
  async function remove(p: Person) {
    if (!confirm(`Remove “${p.name}”? Their trail stays but un-attributes to the default.`)) return;
    try { await deletePerson(p.id); load(); }
    catch (e: any) { alert(e?.message || "Couldn't remove."); }
  }

  return (
    <div className="tool-body">
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        People colour and label location trails on the Map. A fix is matched to a person by its
        source (the PWA sends <code>pwa</code>, the watch <code>wear</code>, a phone its Name) via
        the aliases below; anything unmatched falls to the <strong>default</strong>.
      </p>

      <div className="people-add">
        <input placeholder="Add a person…" value={newName}
               onChange={(e) => setNewName(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter") add(); }} />
        <button className="primary" disabled={busy || !newName.trim()} onClick={add}>Add</button>
      </div>

      <ul className="people-list">
        {people.map((p) => (
          <li key={p.id} className="person-row">
            <input className="person-color" type="color" value={p.color}
                   onChange={(e) => { patch(p.id, "color", e.target.value); commit(p.id, { color: e.target.value }); }}
                   title="Trail colour" />
            <div className="person-main">
              <input className="person-name" value={p.name}
                     onChange={(e) => patch(p.id, "name", e.target.value)}
                     onBlur={(e) => commit(p.id, { name: e.target.value.trim() })} />
              <input className="person-aliases" placeholder="source aliases (comma-separated)"
                     value={p.aliases}
                     onChange={(e) => patch(p.id, "aliases", e.target.value)}
                     onBlur={(e) => commit(p.id, { aliases: e.target.value })} />
            </div>
            <div className="person-actions">
              {p.is_default
                ? <span className="badge" title="The catch-all for unmatched sources">Default</span>
                : <button className="ghost" onClick={() => makeDefault(p.id)}>Make default</button>}
              {p.note_slug && <Link className="ghost" to={`/note/${p.note_slug}`}>Page</Link>}
              {!p.is_default && <button className="place-del" title="Remove" onClick={() => remove(p)}>✕</button>}
            </div>
          </li>
        ))}
      </ul>

      <p className="muted" style={{ fontSize: 12, marginTop: 14 }}>
        Tip: tag a KB note as a person from the note itself (a “Tag as person” button on <code>kb/</code> pages),
        or add a phone tracker's Name as an alias here to fold its trail into someone.
      </p>
    </div>
  );
}
