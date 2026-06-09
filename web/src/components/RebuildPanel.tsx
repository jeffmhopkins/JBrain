import { useEffect, useRef, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  RebuildEvent, RebuildCandidate, RebuildSkipped, SSEHandle, ApiError, CandidateFact,
  rebuildStream, regatherStream, draftStream, suggestStream, guideStream, redraftStream,
  searchRebuildSources, findFacts, acceptRebuild, rejectRebuild,
} from "../api";
import { makeLinkRenderer, renderWikiLinks, stripSummarySentinels } from "../util";
import Modal from "./Modal";
import MarkdownDiff from "./MarkdownDiff";
import { Icon } from "./Icon";

type Stage = "gather" | "curate" | "draft";
type Phase = "streaming" | "review" | "guiding" | "guiding-streaming" | "stale" | "truncated" | "empty" | "error";
interface Step { tool: string; query?: string; running: boolean; summary?: string; items?: string[]; }

interface Props {
  slug: string;
  note: { title: string; content_md: string };
  onClose: () => void;
  onAccepted: (slug: string) => void;
  // "suggest" (default entry): a conversational targeted edit FROM the current article.
  // "rebuild": write the article from scratch (reachable from inside the suggest panel too).
  mode?: "rebuild" | "suggest";
}

const TOOL_LABEL: Record<string, string> = {
  search_notes: "Searching your notes",
  propose_sources: "Choosing the best sources",
};
const leaf = (t: string) => t.split("/").pop() || t;

// Two-stage live rebuild: gather (agent tool use) → curate (pick sources) → draft (write).
export default function RebuildPanel({ slug, note, onClose, onAccepted, mode = "rebuild" }: Props) {
  const navigate = useNavigate();
  const suggest = mode === "suggest";
  const currentLeaf = note.title.split("/").pop() || note.title;
  const parent = note.title.includes("/") ? note.title.slice(0, note.title.lastIndexOf("/")) : "kb";

  const [stage, setStage] = useState<Stage>("gather");
  // gather
  const [steps, setSteps] = useState<Step[]>([]);
  const [gatherStatus, setGatherStatus] = useState("Gathering context…");
  const [gatherErr, setGatherErr] = useState<string | null>(null);
  // curate
  const [candidates, setCandidates] = useState<RebuildCandidate[]>([]);
  const [skipped, setSkipped] = useState<RebuildSkipped[]>([]);
  const [skipOpen, setSkipOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [addResults, setAddResults] = useState<{ note_id: number; title: string; date: string }[]>([]);
  const [hint, setHint] = useState("");
  const [regathering, setRegathering] = useState(false);
  // draft
  const [phase, setPhase] = useState<Phase>("streaming");
  const [tab, setTab] = useState<"draft" | "guide">("draft");
  const [status, setStatus] = useState("Thinking…");
  const [busy, setBusy] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);
  const [thoughts, setThoughts] = useState("");
  const [draft, setDraft] = useState("");
  const [draftSources, setDraftSources] = useState<string[]>([]);
  const [warn, setWarn] = useState<string | null>(null);
  const [showDiff, setShowDiff] = useState(suggest);   // suggest mode shows the change by default
  const [thread, setThread] = useState<{ role: "user" | "ai"; text: string }[]>([]);
  const [guideInput, setGuideInput] = useState("");
  // Truth-seeker (suggest mode): privacy-filtered candidate facts the owner approves before
  // they fold into the message — nothing is applied to the article without consent.
  const [factsOpen, setFactsOpen] = useState(false);
  const [factQuery, setFactQuery] = useState("");
  const [factResults, setFactResults] = useState<CandidateFact[]>([]);
  const [factSel, setFactSel] = useState<Set<number>>(new Set());
  const [factBusy, setFactBusy] = useState(false);
  const [factSearched, setFactSearched] = useState(false);
  const [accepting, setAccepting] = useState(false);
  // Stage-2 output budget. Starts at the server default; a truncated draft can be re-drafted
  // at a larger, approved budget (server clamps the ceiling).
  const [budget, setBudget] = useState(6000);
  const [renameOn, setRenameOn] = useState(false);
  const [renameLeaf, setRenameLeaf] = useState(currentLeaf);

  const runId = useRef<string | null>(null);
  const sse = useRef<SSEHandle | null>(null);
  const sawContent = useRef(false);
  // Suggest mode: the first edit turn calls /suggest (seeds BASE + sources + backlinks); every
  // later turn reuses /guide on the same transcript. These survive re-renders.
  const suggestStarted = useRef(false);
  const suggestIds = useRef<number[]>([]);
  const bodyRef = useRef<HTMLDivElement>(null);
  // A STABLE onClose for Modal: its focus effect keys on the onClose identity, so passing a
  // fresh requestClose each render re-ran it on every keystroke — stealing focus from the
  // Guide textarea and dismissing the mobile keyboard. Route through a ref instead.
  const closeRef = useRef<() => void>(() => {});
  const stableClose = useCallback(() => closeRef.current(), []);

  // ---- gather (Stage 1) --------------------------------------------------------
  function handleGather(e: RebuildEvent) {
    switch (e.type) {
      case "run_started":
        runId.current = e.run_id;
        if (e.kind === "suggest" && e.draft) setDraft(e.draft);   // show the current article now
        break;
      case "tool_use":
        setGatherStatus(TOOL_LABEL[e.tool] || "Working…");
        setSteps((s) => [...s, { tool: e.tool, query: e.query, running: true }]);
        break;
      case "tool_result":
        setSteps((s) => {
          const i = [...s].reverse().findIndex((x) => x.running && x.tool === e.tool);
          if (i < 0) return s;
          const idx = s.length - 1 - i;
          const next = [...s];
          next[idx] = { ...next[idx], running: false, summary: e.summary, items: e.items };
          return next;
        });
        break;
      case "sources_proposed":
        setCandidates(e.candidates);
        setSkipped(e.skipped);
        setRegathering(false);
        setStage("curate");
        break;
      case "error": setGatherErr(e.message); break;
    }
  }

  useEffect(() => {
    sse.current = rebuildStream(slug, handleGather, mode);
    sse.current.done.catch(() => {});
    return () => { sse.current?.abort(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  // ---- draft (Stage 2) ---------------------------------------------------------
  function handleDraft(e: RebuildEvent) {
    switch (e.type) {
      case "thinking_delta": setStatus("Thinking…"); setThoughts((t) => t + e.text); break;
      case "content_delta":
        if (!sawContent.current) { sawContent.current = true; setActivityOpen(false); setStatus("Drafting…"); }
        setDraft((d) => d + e.text);
        break;
      case "lint": setWarn(e.message); break;
      case "done":
        setDraft(e.draft); setBusy(false); setStatus("Ready");
        if (!e.draft.trim()) { setPhase("empty"); break; }
        if (e.truncated) { setWarn("The draft was cut short (length limit) — review carefully."); setPhase("truncated"); break; }
        setWarn(null);
        setPhase((p) => (p === "guiding-streaming" ? "guiding" : "review"));
        break;
      case "error": setBusy(false); setStatus("Failed"); setPhase("error"); break;
    }
  }

  useEffect(() => {
    if (!busy) return;
    const sc = bodyRef.current?.closest(".modal-body");
    if (sc) sc.scrollTop = sc.scrollHeight;
  }, [draft, busy]);

  // ---- curate actions ----------------------------------------------------------
  const selectedIds = candidates.filter((c) => c.on).map((c) => c.note_id);

  function toggle(id: number) {
    setCandidates((cs) => cs.map((c) => (c.note_id === id ? { ...c, on: !c.on } : c)));
  }
  function includeSkipped(s: RebuildSkipped) {
    setCandidates((cs) => [...cs, { ...s, on: true, private: false, added: true }]);
    setSkipped((sk) => sk.filter((x) => x.note_id !== s.note_id));
  }
  async function runAddSearch(q: string) {
    if (!q.trim() || !runId.current) { setAddResults([]); return; }
    try {
      const have = new Set(candidates.map((c) => c.note_id));
      const r = await searchRebuildSources(runId.current, q);
      setAddResults(r.filter((x) => !have.has(x.note_id)));
    } catch { setAddResults([]); }
  }
  function addSource(r: { note_id: number; title: string; date: string }) {
    setCandidates((cs) => [{ note_id: r.note_id, title: r.title, date: r.date,
      reason: "Added by you", on: true, private: false, added: true }, ...cs]);
    setAddResults((a) => a.filter((x) => x.note_id !== r.note_id));
    setAddOpen(false);
  }
  function doRegather() {
    if (!runId.current || regathering) return;
    setRegathering(true);
    const h = regatherStream(runId.current, hint.trim(), handleGather);
    sse.current = h;
    h.done.catch(() => setRegathering(false));
    setHint("");
  }

  function startDraft() {
    const chosen = candidates.filter((c) => c.on);
    if (!chosen.length || !runId.current) return;
    setDraftSources(chosen.map((c) => c.title));
    setStage("draft"); setPhase("streaming"); setBusy(true); setStatus("Thinking…"); setBudget(6000);
    sawContent.current = false; setDraft(""); setThoughts(""); setActivityOpen(false);
    const h = draftStream(runId.current, chosen.map((c) => c.note_id), handleDraft);
    sse.current = h; h.done.catch(() => {});
  }

  // Suggest mode: enter the conversational edit loop showing the CURRENT article. No LLM call
  // yet — the owner talks first; the first message fires /suggest (see sendGuide). Sources are
  // optional (a pure formatting/structure fix needs none).
  function startEditing() {
    if (!runId.current) return;
    const chosen = candidates.filter((c) => c.on);
    suggestIds.current = chosen.map((c) => c.note_id);
    setDraftSources(chosen.map((c) => c.title));
    setStage("draft"); setPhase("guiding"); setTab("guide"); setStatus("Ready");
    setBusy(false); setActivityOpen(false);
  }

  // Re-run the last drafting turn at a larger budget after a truncation (server re-asks the
  // same prompt — no auto-continue — and clamps the ceiling).
  function reDraft() {
    if (!runId.current || busy) return;
    const next = budget < 10000 ? 10000 : 16000;
    setBudget(next);
    setDraft(""); setThoughts(""); sawContent.current = false; setWarn(null);
    setBusy(true); setStatus("Thinking…"); setPhase("streaming"); setActivityOpen(false);
    const h = redraftStream(runId.current, next, handleDraft);
    sse.current = h; h.done.catch(() => {});
  }

  // ---- draft actions -----------------------------------------------------------
  function requestClose() {
    if (stage === "draft" && (phase === "streaming" || phase === "guiding-streaming") && draft &&
        !confirm("Stop the rebuild? Your page won't change.")) return;
    sse.current?.abort();
    if (runId.current) rejectRebuild(runId.current).catch(() => {});
    onClose();
  }
  function doReject() {
    sse.current?.abort();
    if (runId.current) rejectRebuild(runId.current).catch(() => {});
    onClose();
  }
  async function doAccept() {
    if (!runId.current || accepting) return;
    setAccepting(true);
    const nl = renameLeaf.trim();
    const renameTo = renameOn && nl && nl !== currentLeaf ? `${parent}/${nl}` : undefined;
    try {
      const r = await acceptRebuild(runId.current, renameTo);
      onAccepted(r.slug);
    } catch (err) {
      const e = err as ApiError;
      if (e.status === 409 && /stale/i.test(e.message)) {
        setWarn("This page changed since the rebuild started — accepting would overwrite that edit.");
        setPhase("stale");
      } else if (e.status === 409) { setWarn(e.message); }
      else { alert(e.message || "Couldn't accept the rebuild."); }
    } finally { setAccepting(false); }
  }
  // ---- truth-seeker (suggest mode) ---------------------------------------------
  async function runFindFacts() {
    if (!factQuery.trim() || !runId.current || factBusy) return;
    setFactBusy(true); setFactResults([]); setFactSel(new Set());
    try { setFactResults(await findFacts(runId.current, factQuery.trim())); }
    catch { setFactResults([]); }
    finally { setFactBusy(false); setFactSearched(true); }
  }
  function toggleFact(id: number) {
    setFactSel((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  }
  // Fold the owner-APPROVED facts into the message as cited bullet points; the edit turn then
  // incorporates them with footnotes. Nothing reaches the article until the owner sends + accepts.
  function addFacts() {
    const chosen = factResults.filter((f) => factSel.has(f.source_id));
    if (!chosen.length) return;
    const block = "Incorporate these facts, citing each source:\n" +
      chosen.map((f) => `- ${f.claim} [[${f.source_title}]]`).join("\n");
    setGuideInput((g) => (g.trim() ? g.trim() + "\n\n" : "") + block);
    setFactResults([]); setFactSel(new Set()); setFactsOpen(false); setFactQuery(""); setFactSearched(false);
  }

  function openGuide() { setPhase("guiding"); setTab("guide"); }
  function sendGuide() {
    const text = guideInput.trim();
    if (!text || !runId.current || busy) return;
    setGuideInput("");
    setThread((t) => [...t, { role: "user", text }]);
    setTab("draft"); setDraft(""); sawContent.current = false; setBudget(6000);
    setBusy(true); setStatus(suggest ? "Editing…" : "Revising…"); setPhase("guiding-streaming"); setActivityOpen(false);
    // Suggest mode's FIRST turn opens the edit loop (/suggest, seeds BASE + sources + backlinks);
    // every later turn — and all of rebuild's Guide turns — continue via /guide.
    const first = suggest && !suggestStarted.current;
    const h = first
      ? suggestStream(runId.current, suggestIds.current, text, handleDraft)
      : guideStream(runId.current, text, handleDraft);
    if (first) suggestStarted.current = true;
    sse.current = h;
    h.done.then(() => setThread((t) => [...t, { role: "ai", text: suggest ? "Done — see the updated page." : "Updated the draft — take a look." }]))
      .catch(() => {});
  }

  // ---- render ------------------------------------------------------------------
  const title = stage === "curate" ? "Review sources" : suggest ? "Edit with AI" : "Rebuild page";
  const guiding = phase === "guiding" || phase === "guiding-streaming";
  const reviewish = phase === "review" || phase === "truncated" || phase === "stale";

  const draftMd = (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: makeLinkRenderer(navigate) }}>
      {renderWikiLinks(stripSummarySentinels(draft))}
    </ReactMarkdown>
  );

  const footer = (() => {
    if (stage === "gather") {
      if (gatherErr) return <button className="primary rb-foot-btn" onClick={onClose}>Close</button>;
      return <button className="ghost rb-foot-btn" onClick={requestClose}>Cancel</button>;
    }
    if (stage === "curate") {
      const n = selectedIds.length;
      if (suggest)
        return (<>
          <button className="ghost rb-foot-btn" onClick={startDraft} disabled={!n}
                  title="Rewrite the whole article from scratch instead of editing it">Rebuild from scratch</button>
          <button className="primary rb-foot-btn" onClick={startEditing}>
            {n ? `Edit with ${n} source${n !== 1 ? "s" : ""}` : "Start editing"}</button>
        </>);
      return <button className="primary rb-foot-btn" onClick={startDraft} disabled={!n}>
        {n ? `Draft from ${n} source${n !== 1 ? "s" : ""}` : "Pick at least one source"}</button>;
    }
    // draft stage
    if (phase === "streaming") return <button className="ghost rb-foot-btn" onClick={requestClose}>Cancel</button>;
    if (phase === "review" || phase === "truncated")
      return (<>
        <button className="ghost danger-hover rb-foot-btn" onClick={doReject}>Reject</button>
        {phase === "truncated" && budget < 16000 &&
          <button className="ghost rb-foot-btn" onClick={reDraft} disabled={busy}>Re-draft with more room</button>}
        <button className="ghost rb-foot-btn" onClick={openGuide}>Guide</button>
        <button className="primary rb-foot-btn" onClick={doAccept} disabled={accepting}>
          {accepting ? "Saving…" : (phase === "truncated" ? "Accept anyway" : "Accept")}</button>
      </>);
    if (phase === "stale")
      return (<>
        <button className="ghost danger-hover rb-foot-btn" onClick={doReject}>Reject</button>
        <button className="primary rb-foot-btn" onClick={onClose}>Close</button>
      </>);
    if (phase === "empty" || phase === "error")
      return <button className="primary rb-foot-btn" onClick={onClose}>Close</button>;
    if (guiding) {
      if (tab === "guide")
        return (
          <div className="rb-composer">
            <textarea value={guideInput} placeholder={suggest ? "Tell me what to change…" : "Steer the rewrite…"}
                      onChange={(e) => setGuideInput(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendGuide(); } }} />
            <button className="rb-send" onClick={sendGuide} disabled={busy || !guideInput.trim()} aria-label="Send">
              <Icon name="send" size={18} /></button>
          </div>);
      // The segmented Draft/Guide toggle above already switches back to Guide, so the
      // footer's slot is better spent on a way out: Cancel (requestClose guards mid-stream).
      return (<>
        <button className="ghost rb-foot-btn" onClick={requestClose}>Cancel</button>
        <button className="primary rb-foot-btn" onClick={doAccept} disabled={accepting}>
          {accepting ? "Saving…" : "Accept"}</button>
      </>);
    }
    return null;
  })();

  const headerExtra = <span className="rb-base-chip">{/^kb\/(health|finance)\//i.test(note.title) && <span className="rb-lock">🔒</span>}{slug}</span>;
  closeRef.current = requestClose;   // keep the stable wrapper pointing at the latest logic

  return (
    <Modal title={title} onClose={stableClose} footer={footer} headerExtra={headerExtra}>
      <div className="rb-panel" ref={bodyRef}>
        {/* progress pips */}
        <div className="rb-pips">
          {(["gather", "curate", "draft"] as Stage[]).map((s, i) => (
            <span key={s} className={"rb-pip" + (stage === s ? " on" : (["gather", "curate", "draft"].indexOf(stage) > i ? " done" : ""))} />
          ))}
          <span className="rb-pip-lbl">
            {stage === "gather" ? "1 · Gathering context" : stage === "curate" ? "2 · Review sources" : suggest ? "3 · Editing" : "3 · Drafting"}
          </span>
        </div>

        {/* ---- STAGE 1: gather ---- */}
        {stage === "gather" && (
          gatherErr
            ? <div className="rb-warn">⚠️ {gatherErr}</div>
            : <>
                <div className="rb-status"><span className="rb-dots"><i /><i /><i /></span> {gatherStatus}</div>
                <ol className="rb-steps">
                  {steps.map((st, i) => (
                    <li key={i} className={"rb-step" + (st.running ? " run" : "")}>
                      <div className="rb-step-lab">
                        {st.running ? <span className="rb-dots"><i /><i /><i /></span> : "✓"}{" "}
                        {TOOL_LABEL[st.tool] || st.tool}{st.query ? <span className="muted"> — “{st.query}”</span> : null}
                      </div>
                      {st.items && st.items.length > 0 && (
                        <div className="rb-refs">{st.items.slice(0, 6).map((t, j) => <span key={j} className="rb-chip">🔗 {leaf(t)}</span>)}</div>
                      )}
                    </li>
                  ))}
                  {steps.length === 0 && <li className="muted" style={{ fontSize: 13 }}>Looking through your notes…</li>}
                </ol>
              </>
        )}

        {/* ---- STAGE 2: curate ---- */}
        {stage === "curate" && (
          <>
            <div className="rb-hint">{suggest
              ? "Pick the notes the AI may draw facts from while editing (optional — a formatting or wording fix needs none). Then start the conversation."
              : "The AI picked these from your notes. Uncheck anything you don't want shaping the article — or add your own. Sources are notes only; links to existing KB pages are added automatically."}</div>
            <div className="rb-act-h">Sources found ({candidates.length})</div>
            {candidates.map((c) => (
              <div key={c.note_id} className={"rb-src" + (c.on ? " on" : " off")} onClick={() => toggle(c.note_id)}>
                <span className="rb-cbx">{c.on ? "✓" : ""}</span>
                <div className="rb-src-main">
                  <div className="rb-src-title">{leaf(c.title)}
                    {c.private && <span className="rb-pbadge">🔒 private</span>}
                    {c.added && <span className="rb-added">added</span>}</div>
                  <div className="rb-src-meta">{c.title}{c.date ? ` · ${c.date}` : ""}</div>
                  <div className="rb-src-reason">{c.private && !c.on ? "Left out — private note on a non-private page" : c.reason}</div>
                </div>
              </div>
            ))}

            <div className="rb-addbar">
              <div className="rb-addrow">
                <button className="rb-mini" onClick={() => { setAddOpen((o) => !o); setAddResults([]); }}>＋ Add a note</button>
                <input className="rb-ti" placeholder="Find more like… (e.g. travel bread)" value={hint}
                       onChange={(e) => setHint(e.target.value)}
                       onKeyDown={(e) => { if (e.key === "Enter") doRegather(); }} />
                <button className="rb-mini" onClick={doRegather} disabled={regathering}>{regathering ? "…" : "Re-gather"}</button>
              </div>
              {addOpen && (
                <div>
                  <input className="rb-ti" autoFocus placeholder="Search your notes to add…"
                         onChange={(e) => runAddSearch(e.target.value)} />
                  {addResults.length > 0 && (
                    <div className="rb-results">
                      {addResults.map((r) => (
                        <div key={r.note_id} className="rb-rrow">
                          <span>{leaf(r.title)}<br /><span className="muted" style={{ fontSize: 11 }}>{r.title}{r.date ? ` · ${r.date}` : ""}</span></span>
                          <button className="rb-radd" onClick={() => addSource(r)}>＋ Add</button>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>

            {skipped.length > 0 && (
              <>
                <button className="rb-disc" onClick={() => setSkipOpen((o) => !o)}>
                  {skipOpen ? "▾" : "▸"} Considered but skipped ({skipped.length})</button>
                {skipOpen && skipped.map((s) => (
                  <div key={s.note_id} className="rb-src off">
                    <span className="rb-cbx" />
                    <div className="rb-src-main">
                      <div className="rb-src-title">{leaf(s.title)}</div>
                      <div className="rb-src-meta">{s.title}{s.date ? ` · ${s.date}` : ""}</div>
                      <div className="rb-src-reason">{s.reason}</div>
                    </div>
                    <button className="rb-incl" onClick={() => includeSkipped(s)}>＋ Include</button>
                  </div>
                ))}
              </>
            )}
          </>
        )}

        {/* ---- STAGE 3: draft ---- */}
        {stage === "draft" && (
          <>
            <div className={"rb-activity" + (activityOpen ? " open" : "")}>
              <button className="rb-act-row" onClick={() => setActivityOpen((o) => !o)}>
                <span className="rb-status" style={{ margin: 0 }}>
                  {busy && <span className="rb-dots"><i /><i /><i /></span>} {status}</span>
                <span className="rb-caret">▸</span>
              </button>
              {activityOpen && (
                <div className="rb-act-body">
                  {thoughts && <><div className="rb-act-h">Thoughts</div><div className="rb-thoughts">{thoughts}</div></>}
                  <div className="rb-act-h">Sources ({draftSources.length})</div>
                  <div className="rb-refs">{draftSources.map((t, i) => <span key={i} className="rb-chip">🔗 {leaf(t)}</span>)}</div>
                </div>
              )}
            </div>

            {warn && <div className="rb-warn">⚠️ {warn}</div>}

            {guiding && (
              <div className="rb-seg show">
                <button className={tab === "draft" ? "on" : ""} onClick={() => setTab("draft")}>📄 Draft</button>
                <button className={tab === "guide" ? "on" : ""} onClick={() => setTab("guide")}>💬 Guide</button>
              </div>
            )}

            {!(guiding && tab === "guide") ? (
              <div>
                {(reviewish || (suggest && guiding)) && draft && (
                  <div className="rb-draft-bar">
                    <span className="rb-phase">{showDiff ? "Changes" : (suggest ? "Edited page" : "New draft")}</span>
                    <button className="rb-seechg" onClick={() => setShowDiff((s) => !s)}>{showDiff ? "Hide changes" : "See changes"}</button>
                  </div>
                )}
                {(reviewish || guiding) && draft && (() => {
                  const h1 = (draft.match(/^#\s+(.+?)\s*$/m)?.[1] || "").trim();
                  return (
                    <div className="rb-rename">
                      <label className="rb-rename-tog">
                        <input type="checkbox" checked={renameOn} onChange={(e) => setRenameOn(e.target.checked)} /> Rename page
                      </label>
                      {renameOn && (
                        <>
                          <div className="rb-rename-row">
                            <span className="rb-rename-prefix">{parent}/</span>
                            <input className="rb-ti" value={renameLeaf} onChange={(e) => setRenameLeaf(e.target.value)} />
                          </div>
                          {h1 && h1 !== currentLeaf && h1 !== renameLeaf.trim() && (
                            <button className="rb-suggest" onClick={() => setRenameLeaf(h1)}>Use AI heading: “{h1}”</button>
                          )}
                        </>
                      )}
                    </div>
                  );
                })()}
                <div className="rb-draft-body md">
                  {phase === "empty" ? <p className="muted">The model didn't produce a draft.</p>
                    : showDiff ? <MarkdownDiff before={note.content_md} after={draft} /> : draftMd}
                </div>
              </div>
            ) : (
              <div className="rb-guide">
                <div className="rb-thread">
                  <div className="rb-msg ai">{suggest
                    ? "Tell me what to change — I'll edit the page and keep everything else, drawing facts from your chosen sources."
                    : "Tell me how to revise this — I'll rewrite from your chosen sources (no new lookups)."}</div>
                  {thread.map((m, i) => <div key={i} className={"rb-msg " + (m.role === "user" ? "user" : "ai")}>{m.text}</div>)}
                </div>
                {suggest && (
                  <div className="rb-facts">
                    <button className="rb-mini" onClick={() => setFactsOpen((o) => !o)}>🔎 Find facts in your notes</button>
                    {factsOpen && (
                      <div>
                        <div className="rb-addrow">
                          <input className="rb-ti" placeholder="What should I look for? (e.g. when did Al move)" value={factQuery}
                                 onChange={(e) => setFactQuery(e.target.value)}
                                 onKeyDown={(e) => { if (e.key === "Enter") runFindFacts(); }} />
                          <button className="rb-mini" onClick={runFindFacts} disabled={factBusy || !factQuery.trim()}>{factBusy ? "…" : "Search"}</button>
                        </div>
                        {factResults.length > 0 && (
                          <>
                            <div className="rb-results">
                              {factResults.map((f) => (
                                <label key={f.source_id} className="rb-frow">
                                  <input type="checkbox" checked={factSel.has(f.source_id)} onChange={() => toggleFact(f.source_id)} />
                                  <span>{f.claim}<br /><span className="muted" style={{ fontSize: 11 }}>🔗 {leaf(f.source_title)}{f.date ? ` · ${f.date}` : ""}</span></span>
                                </label>
                              ))}
                            </div>
                            <button className="rb-mini" onClick={addFacts} disabled={!factSel.size}>
                              ＋ Add {factSel.size || ""} to my message</button>
                          </>
                        )}
                        {!factBusy && factSearched && factResults.length === 0 &&
                          <div className="muted" style={{ fontSize: 12 }}>No matching facts in your notes.</div>}
                      </div>
                    )}
                  </div>
                )}
                {draftSources.length > 0 && (
                  <div className="rb-ctx"><span className="muted">Working from:</span>
                    {draftSources.map((t, i) => <span key={i} className="rb-chip">🔗 {leaf(t)}</span>)}</div>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </Modal>
  );
}
