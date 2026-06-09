import { useEffect, useState } from "react";
import Modal from "./Modal";
import { EntitySummary, EntityDetail, EntityDecision, listEntities } from "../api";

// Monochrome glyph per entity kind — shared with EntitiesPage's list/detail rows.
export const ICON: Record<string, string> = {
  person: "👤", animal: "🐾", org: "🏢", place: "📍", thing: "📦", work: "🎬",
  condition: "🩺", medication: "💊", procedure: "🩻", event: "📅", concept: "💡",
};

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
        <ul className="id-results">
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

type View = "menu" | "aliases" | "merge" | "split" | "history";
const VIEW_LABEL: Record<Exclude<View, "menu">, string> = {
  aliases: "Manage aliases", merge: "Fold a duplicate in", split: "Mark as separate", history: "Identity history",
};

interface Props {
  entity: EntityDetail;
  decisions: EntityDecision[];
  userAliases: EntityDecision[];   // removable, user-added alias decisions
  busy: boolean;
  err: string;
  embeddingsReady: boolean;
  embeddingsReason?: string;
  decLabel: (d: EntityDecision) => string;
  onClose: () => void;
  onAddAlias: (display: string) => void;
  onRemoveAlias: (norm: string) => void;
  onMerge: (other: EntitySummary) => void;
  onSplit: (other: EntitySummary) => void;
}

// The roomy home for an entity's durable identity controls — a launcher of action
// tiles (aliases / fold a duplicate in / keep separate / history), each opening its
// own focused view inside the one responsive Modal (a back chevron returns to the
// launcher). Replaces the cramped inline card at the foot of the entity detail pane.
// Data + mutation logic stay in EntitiesPage; this component is pure UI over callbacks.
export default function EntityIdentityModal(p: Props) {
  const { entity } = p;
  const [view, setView] = useState<View>("menu");
  const [aliasInput, setAliasInput] = useState("");

  const recognized = entity.aliases ?? [];
  const aliasSub = p.userAliases.length
    ? `${p.userAliases.length} alias${p.userAliases.length === 1 ? "" : "es"} added`
    : "Add a nickname or variant";
  const historySub = p.decisions.length
    ? `${p.decisions.length} durable decision${p.decisions.length === 1 ? "" : "s"}`
    : "No decisions yet";

  const submitAlias = () => {
    const v = aliasInput.trim();
    if (!v || p.busy) return;
    p.onAddAlias(v);
    setAliasInput("");
  };

  const title = view === "menu"
    ? "Identity actions"
    : <button className="ghost id-back" onClick={() => setView("menu")}>‹ {VIEW_LABEL[view]}</button>;

  return (
    <Modal title={title} onClose={p.onClose}>
      <div className="id-ent">
        <span>{ICON[entity.type] || "•"} <b>{entity.canonical_name}</b></span>
        <span className="muted"> · {entity.type}</span>
      </div>

      {!p.embeddingsReady && (
        <p className="cap-note">Changes are saved now, but won’t re-index until semantic search is ready ({p.embeddingsReason})</p>
      )}
      {p.err && <p className="id-err">{p.err}</p>}

      {view === "menu" && (
        <>
          <div className="id-menu">
            <button className="id-row" onClick={() => setView("aliases")}>
              <span className="id-tile">🏷️</span>
              <span className="id-main"><span className="id-label">Manage aliases</span><span className="id-sub">{aliasSub}</span></span>
              <span className="id-arrow">›</span>
            </button>
            <button className="id-row" onClick={() => setView("merge")}>
              <span className="id-tile">🔗</span>
              <span className="id-main"><span className="id-label">Fold a duplicate in</span><span className="id-sub">Merge another {entity.type} into this page</span></span>
              <span className="id-arrow">›</span>
            </button>
            <button className="id-row" onClick={() => setView("split")}>
              <span className="id-tile">✂️</span>
              <span className="id-main"><span className="id-label">Mark as separate</span><span className="id-sub">Block an accidental merge</span></span>
              <span className="id-arrow">›</span>
            </button>
            <button className="id-row" onClick={() => setView("history")}>
              <span className="id-tile">📜</span>
              <span className="id-main"><span className="id-label">Identity history</span><span className="id-sub">{historySub}</span></span>
              <span className="id-arrow">›</span>
            </button>
          </div>
          <p className="cap-note">These choices are durable — they survive every knowledge-base rebuild.</p>
        </>
      )}

      {view === "aliases" && (
        <>
          <p className="id-field-label">Add an alias — a nickname or variant that links here.</p>
          <div className="id-add">
            <input className="modal-input" placeholder="e.g. Jeff Hopkins" value={aliasInput} disabled={p.busy}
                   onChange={(e) => setAliasInput(e.target.value)}
                   onKeyDown={(e) => { if (e.key === "Enter") submitAlias(); }} />
            <button className="primary" disabled={p.busy || !aliasInput.trim()} onClick={submitAlias}>Add</button>
          </div>
          {p.userAliases.length > 0 && (
            <div className="id-chips">
              {p.userAliases.map((d) => (
                <span key={d.id} className="chip id-chip">
                  {d.display_a || d.norm_a}
                  <button className="ghost" disabled={p.busy} title="Remove alias" aria-label={`Remove alias ${d.display_a || d.norm_a}`}
                          onClick={() => p.onRemoveAlias(d.norm_a)}>✕</button>
                </span>
              ))}
            </div>
          )}
          {recognized.length > 0 && (
            <p className="id-recognized">Also recognized: {recognized.join(", ")}</p>
          )}
        </>
      )}

      {view === "merge" && (
        <>
          <p className="id-field-label">Fold a duplicate {entity.type} in — it becomes this canonical page.</p>
          <EntityPicker label="Search the duplicate to merge in…" type={entity.type} excludeId={entity.id}
                        disabled={p.busy} onPick={p.onMerge} />
        </>
      )}

      {view === "split" && (
        <>
          <p className="id-field-label">Mark a different {entity.type} as NOT this one (block an accidental merge).</p>
          <EntityPicker label="Search the one to keep separate…" type={entity.type} excludeId={entity.id}
                        disabled={p.busy} onPick={p.onSplit} />
        </>
      )}

      {view === "history" && (
        p.decisions.length === 0
          ? <p className="muted" style={{ fontSize: 13 }}>No identity decisions yet.</p>
          : <ul className="id-history">
              {p.decisions.map((d) => (
                <li key={d.id}><span className="id-kind">{d.kind}</span> {p.decLabel(d)}</li>
              ))}
            </ul>
      )}
    </Modal>
  );
}
