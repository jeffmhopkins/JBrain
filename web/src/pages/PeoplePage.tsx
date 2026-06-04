import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Person, getPeople, addPerson, updatePerson, deletePerson, generateLocationKey, revokeLocationKey, getServer } from "../api";

// One copyable "setup code" the tracker app parses into Name + Server + Key, so a
// family phone is configured by pasting a single string. jbt1.<base64url(JSON)>.
function setupCode(name: string, server: string, token: string): string {
  const json = JSON.stringify({ n: name, s: server, k: token });
  const b64 = btoa(unescape(encodeURIComponent(json)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return "jbt1." + b64;
}

// People aren't accounts — they label/colour location trails (matched from a fix's
// `source` via aliases) and can link to a KB page. Exactly one is the default ("Me"),
// the catch-all for any unmatched source. Configured here, opened from Advanced.
export default function PeoplePage() {
  const [people, setPeople] = useState<Person[]>([]);
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState<number | null>(null);
  const server = getServer() || window.location.origin;   // baked into the setup code
  const codeFor = (p: Person) => (p.location_key ? setupCode(p.name, server, p.location_key) : "");

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

  async function genKey(id: number) {
    try { const r = await generateLocationKey(id); patch(id, "location_key", r.location_key); }
    catch (e: any) { alert(e?.message || "Couldn't generate a key."); }
  }
  async function revokeKey(id: number) {
    if (!confirm("Revoke this location key? Any device using it stops being able to log.")) return;
    try { await revokeLocationKey(id); patch(id, "location_key", null); }
    catch (e: any) { alert(e?.message || "Couldn't revoke."); }
  }
  async function copyKey(p: Person) {
    const code = codeFor(p);
    if (!code) return;
    try { await navigator.clipboard.writeText(code); setCopied(p.id); setTimeout(() => setCopied(null), 1500); }
    catch { alert(code); }
  }
  async function remove(p: Person) {
    if (!confirm(`Remove “${p.name}”? Their trail stays but un-attributes to the default.`)) return;
    try { await deletePerson(p.id); load(); }
    catch (e: any) { alert(e?.message || "Couldn't remove."); }
  }

  return (
    <div className="tool-body">
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        Users label and colour the people whose devices feed this brain. Each one colours its
        location trail on the Map, and a note dictated on its watch is attributed to it too. A
        location fix is matched to a user by its source (the PWA sends <code>pwa</code>, the watch
        <code>wear</code>, a phone its Name) via the aliases below; anything unmatched falls to the
        {" "}<strong>default</strong>. Users aren't login accounts — they're labels for the people
        and devices sharing this brain.
      </p>

      <div className="people-add">
        <input placeholder="Add a user…" value={newName}
               onChange={(e) => setNewName(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter") add(); }} />
        <button className="primary" disabled={busy || !newName.trim()} onClick={add}>Add</button>
      </div>

      <ul className="people-list">
        {people.map((p) => (
          <li key={p.id} className="person-row">
            <div className="person-top">
              <input className="person-color" type="color" value={p.color}
                     onChange={(e) => { patch(p.id, "color", e.target.value); commit(p.id, { color: e.target.value }); }}
                     title="Trail colour" />
              <div className="person-main">
                {/* Name + aliases are baked into the live setup code and drive fix
                    attribution — changing them would desync the codes already on
                    people's phones, so they're locked until the key is revoked. */}
                <input className="person-name" value={p.name}
                       disabled={!!p.location_key}
                       title={p.location_key ? "Locked while a setup code is active — revoke it to rename." : undefined}
                       onChange={(e) => patch(p.id, "name", e.target.value)}
                       onBlur={(e) => commit(p.id, { name: e.target.value.trim() })} />
                <input className="person-aliases" placeholder="source aliases (comma-separated)"
                       value={p.aliases}
                       disabled={!!p.location_key}
                       title={p.location_key ? "Locked while a setup code is active — revoke it to change ingestion sources." : undefined}
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
            </div>
            <div className="person-key">
              {p.location_key ? (
                <>
                  <code className="person-key-val" title={codeFor(p)}>{codeFor(p)}</code>
                  <button className="ghost" onClick={() => copyKey(p)}>{copied === p.id ? "Copied ✓" : "Copy setup code"}</button>
                  <button className="ghost" onClick={() => genKey(p.id)}>Regenerate</button>
                  <button className="ghost danger-hover" onClick={() => revokeKey(p.id)}>Revoke</button>
                </>
              ) : (
                <button className="ghost" onClick={() => genKey(p.id)}>+ Location key</button>
              )}
            </div>
          </li>
        ))}
      </ul>

      <p className="muted" style={{ fontSize: 12, marginTop: 14 }}>
        Setup code: copy it and paste into the tracker app's <strong>Access key</strong> field — it auto-fills the
        user's Name, your server, and a scoped key (no typing). That key can log this user's location and file notes
        dictated on their watch, and nothing else (it can't read the trail or reach anything else); both are attributed
        to them. While a code is active the user's <strong>Name</strong> and <strong>ingestion sources</strong> lock
        (the code on their phone bakes those in) — <strong>Revoke</strong> to edit, then re-issue. Tip: you can also tag
        a KB note as a user from the note itself, or add a tracker's Name as an alias above to fold its trail in.
      </p>
    </div>
  );
}
