import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  EntitySummary, EntityDetail, EntityDecision,
  listEntities, getEntity, mergeEntities, splitEntities,
  addEntityAlias, removeEntityAlias, listEntityDecisions, getEntityRebuildStatus,
} from "../api";
import { leaf, slugify } from "../util";

const ICON: Record<string, string> = {
  person: "👤", animal: "🐾", org: "🏢", place: "📍", thing: "📦", work: "🎬",
  condition: "🩺", medication: "💊", procedure: "🩻", event: "📅", concept: "💡",
};
const TYPES = [["", "All"], ["person", "People"], ["animal", "Animals"], ["org", "Orgs"],
  ["place", "Places"], ["thing", "Things"], ["work", "Media"], ["condition", "Conditions"],
  ["medication", "Meds"], ["procedure", "Procedures"], ["event", "Events"], ["concept", "Concepts"]];

// Inline entity search → pick. Used to choose the other side of a merge/split.
function EntityPicker({ label, type, excludeId, onPick, disabled }: {
  label: string; type: string; excludeId: number; onPick: (e: EntitySummary) => void; disabled?: boolean;
}) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<EntitySummary[]>([]);
  useEffect(() => {
    if (!q.trim()) { setHits([]); return; }
    let live = true;
    listEntities(q.trim(), type).then((r) => { if (live) setHits(r.filter((e) => e.id !== excludeId).slice(0, 8)); }).catch(() => {});
    return () => { live = false; };
  }, [q, type, excludeId]);
  return (
    <div style={{ marginTop: 6 }}>
      <input className="modal-input" placeholder={label} value={q} disabled={disabled}
             onChange={(e) => setQ(e.target.value)} style={{ width: "100%" }} />
      {hits.length > 0 && (
        <ul style={{ listStyle: "none", padding: 0, margin: "4px 0", border: "1px solid var(--border,#333)", borderRadius: 6 }}>
          {hits.map((e) => (
            <li key={e.id}>
              <button className="entity-row" disabled={disabled}
                      onClick={() => { setQ(""); setHits([]); onPick(e); }}>
                <span>{ICON[e.type] || "•"} {e.canonical_name}</span>
                <span className="muted" style={{ fontSize: 12 }}>{e.note_count}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// Browse the canonical entity index aggregated from per-note AI analysis. Clicking an entity
// shows every note that mentions it, its KB article, and the durable identity controls
// (merge a duplicate in, mark a different person apart, add/remove an alias) that survive
// every entity_index.rebuild().
export default function EntitiesPage() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") || "";
  const type = params.get("type") || "";
  const [list, setList] = useState<EntitySummary[]>([]);
  const [sel, setSel] = useState<EntityDetail | null>(null);
  const [decisions, setDecisions] = useState<EntityDecision[]>([]);
  const [aliasInput, setAliasInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [ledgerOpen, setLedgerOpen] = useState(false);
  // The mutating ops record their decision synchronously but defer the (slow) index rebuild
  // to a coalesced background worker. While it runs we show a "Refreshing…" badge and poll
  // /status; when the rebuild generation advances we re-fetch so the fold is reflected.
  const [rebuilding, setRebuilding] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  function refreshList() {
    listEntities(q, type).then(setList).catch(() => setList([]));
  }
  useEffect(refreshList, [q, type]);

  // Poll the deferred-rebuild status until the index reconciles past `fromGen`, then re-fetch
  // the open detail + the list so the merge/split/alias result is reflected. Idempotent: a new
  // op replaces the prior poll. Cleaned up on unmount.
  function watchRebuild(fromGen: number, keepId: number) {
    if (pollRef.current) clearInterval(pollRef.current);
    setRebuilding(true);
    pollRef.current = setInterval(async () => {
      try {
        const s = await getEntityRebuildStatus();
        if (!s.rebuilding && s.generation > fromGen) {
          if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
          setRebuilding(false);
          getEntity(keepId).then((d) => {
            setSel(d);
            listEntityDecisions(d.id).then(setDecisions).catch(() => setDecisions([]));
          }).catch(() => {});
          refreshList();
        }
      } catch { /* transient; keep polling */ }
    }, 1200);
  }
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  useEffect(() => {
    if (q && list.length >= 1 && (!sel || sel.canonical_name.toLowerCase() !== q.toLowerCase())) {
      const hit = list.find((e) => e.canonical_name.toLowerCase() === q.toLowerCase()) || (list.length === 1 ? list[0] : null);
      if (hit) open(hit.id);
    }
  }, [list]); // eslint-disable-line

  function open(id: number) {
    setErr(""); setAliasInput("");
    getEntity(id).then((d) => { setSel(d); listEntityDecisions(id).then(setDecisions).catch(() => setDecisions([])); }).catch(() => {});
  }
  function setType(t: string) { const p = new URLSearchParams(params); t ? p.set("type", t) : p.delete("type"); setParams(p); }
  function setQ(v: string) { const p = new URLSearchParams(params); v ? p.set("q", v) : p.delete("q"); setParams(p); }

  // Each identity op records its decision synchronously and defers the index rebuild. We show
  // the immediate (pre-fold) detail + ledger right away, then watch the deferred rebuild and
  // re-fetch once it reconciles so the merge/split/alias result materializes.
  async function run(action: () => Promise<EntityDetail | { ok: boolean }>, keepId: number) {
    if (busy) return;
    setBusy(true); setErr("");
    // Capture the generation BEFORE the op so we only re-fetch once the rebuild it triggers lands.
    let fromGen = -1;
    try { fromGen = (await getEntityRebuildStatus()).generation; } catch { /* fall back to 0 */ fromGen = 0; }
    try {
      const res = await action();
      const detail = (res as EntityDetail).id ? (res as EntityDetail) : await getEntity(keepId);
      setSel(detail);
      listEntityDecisions(detail.id).then(setDecisions).catch(() => setDecisions([]));
      refreshList();
      watchRebuild(fromGen, detail.id);
    } catch (e: any) {
      setErr(e?.message || "That identity change didn’t go through.");
    } finally { setBusy(false); }
  }

  // User-added alias decisions anchored to the selected entity (removable; heuristic a.k.a. aren't).
  const userAliases = sel
    ? decisions.filter((d) => d.kind === "alias" && d.norm_b === sel.normalized_key)
    : [];
  const decLabel = (d: EntityDecision) => {
    if (d.kind === "merge") return `${d.display_a || d.norm_a} → ${d.display_b || d.canonical}`;
    if (d.kind === "split") return `${d.display_a || d.norm_a} ∥ ${d.display_b || d.norm_b}`;
    return `“${d.display_a || d.norm_a}” → ${d.norm_b}`;
  };

  return (
    <div className="content">
      <h2 style={{ marginTop: 0 }}>Entities</h2>
      <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>
        People, organizations, places, and things the AI found across your notes.
      </p>

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", margin: "10px 0" }}>
        <input className="modal-input" placeholder="Search entities…" value={q}
               onChange={(e) => setQ(e.target.value)} style={{ flex: "1 1 200px" }} />
        {TYPES.map(([val, label]) => (
          <button key={val} className={"ghost" + (type === val ? " active" : "")} onClick={() => setType(val)}>{label}</button>
        ))}
      </div>

      <div style={{ display: "flex", gap: 24, flexWrap: "wrap", alignItems: "flex-start" }}>
        <ul style={{ listStyle: "none", padding: 0, flex: "1 1 280px", maxWidth: 420 }}>
          {list.map((e) => (
            <li key={e.id}>
              <button className="entity-row" onClick={() => open(e.id)}>
                <span>{ICON[e.type] || "•"} {e.canonical_name}</span>
                <span className="muted" style={{ fontSize: 12 }}>{e.note_count}</span>
              </button>
            </li>
          ))}
          {list.length === 0 && <li className="muted" style={{ fontSize: 13 }}>No entities yet — run Note analysis / a rebuild first.</li>}
        </ul>

        {sel && (
          <div style={{ flex: "1 1 340px" }}>
            <h3 style={{ marginTop: 0, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <span>{ICON[sel.type] || "•"} {sel.canonical_name}</span>
              {rebuilding && (
                <span className="muted" style={{ fontSize: 12, fontWeight: "normal" }} title="Rebuilding the entity index in the background">
                  ⟳ Refreshing…
                </span>
              )}
            </h3>
            {!!sel.aliases?.length && <p className="muted" style={{ fontSize: 12, marginTop: -6 }}>a.k.a. {sel.aliases.join(", ")}</p>}
            {sel.article_title
              ? <p><Link to={`/note/${slugify(sel.article_title)}`} className="wikilink">📖 {leaf(sel.article_title)}</Link></p>
              : <p className="muted" style={{ fontSize: 13 }}>No KB article yet.</p>}

            <h4>Mentioned in {sel.notes.length} note{sel.notes.length === 1 ? "" : "s"}</h4>
            <ul style={{ paddingLeft: 18 }}>
              {sel.notes.map((n) => (<li key={n.id}><Link to={`/note/${n.slug}`}>{leaf(n.title)}</Link></li>))}
            </ul>

            {/* ── Identity controls ── */}
            <div className="card" style={{ marginTop: 12 }}>
              <h4 style={{ marginTop: 0 }}>Identity</h4>
              {err && <p style={{ color: "var(--danger,#e66)", fontSize: 13 }}>{err}</p>}

              <label className="muted" style={{ fontSize: 12 }}>Add an alias (a nickname/variant that links here)</label>
              <div style={{ display: "flex", gap: 6, marginTop: 4 }}>
                <input className="modal-input" placeholder="e.g. Jeff Hopkins" value={aliasInput} disabled={busy}
                       onChange={(e) => setAliasInput(e.target.value)}
                       onKeyDown={(e) => { if (e.key === "Enter" && aliasInput.trim()) run(() => addEntityAlias(sel.id, aliasInput.trim()), sel.id); }}
                       style={{ flex: 1 }} />
                <button className="primary" disabled={busy || !aliasInput.trim()}
                        onClick={() => { run(() => addEntityAlias(sel.id, aliasInput.trim()), sel.id); setAliasInput(""); }}>Add</button>
              </div>
              {userAliases.length > 0 && (
                <div style={{ marginTop: 6 }}>
                  {userAliases.map((d) => (
                    <span key={d.id} className="chip" style={{ marginRight: 6, display: "inline-flex", gap: 4, alignItems: "center" }}>
                      {d.display_a || d.norm_a}
                      <button className="ghost" style={{ padding: "0 4px" }} disabled={busy} title="Remove alias"
                              onClick={() => run(() => removeEntityAlias(sel.id, d.norm_a), sel.id)}>✕</button>
                    </span>
                  ))}
                </div>
              )}

              <label className="muted" style={{ fontSize: 12, display: "block", marginTop: 12 }}>
                Fold a duplicate of this {sel.type} into it (it becomes the canonical page)
              </label>
              <EntityPicker label="Search the duplicate to merge in…" type={sel.type} excludeId={sel.id} disabled={busy}
                            onPick={(e) => run(() => mergeEntities(e.id, sel.id), sel.id)} />

              <label className="muted" style={{ fontSize: 12, display: "block", marginTop: 12 }}>
                Mark a different {sel.type} as NOT this one (block an accidental merge)
              </label>
              <EntityPicker label="Search the one to keep separate…" type={sel.type} excludeId={sel.id} disabled={busy}
                            onPick={(e) => run(() => splitEntities(sel.id, e.id), sel.id)} />

              {decisions.length > 0 && (
                <div style={{ marginTop: 12 }}>
                  <button className="ghost" style={{ fontSize: 12 }} onClick={() => setLedgerOpen((o) => !o)}>
                    {ledgerOpen ? "▾" : "▸"} Identity decisions ({decisions.length})
                  </button>
                  {ledgerOpen && (
                    <ul style={{ paddingLeft: 18, fontSize: 12 }} className="muted">
                      {decisions.map((d) => <li key={d.id}>{d.kind}: {decLabel(d)}</li>)}
                    </ul>
                  )}
                </div>
              )}
              <p className="muted" style={{ fontSize: 11, marginTop: 8 }}>
                These choices are durable — they survive every knowledge-base rebuild.
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
