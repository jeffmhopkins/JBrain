import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  RebuildEvent, SSEHandle, rebuildStream, guideStream, acceptRebuild, rejectRebuild, ApiError,
} from "../api";
import { makeLinkRenderer, renderWikiLinks, stripSummarySentinels } from "../util";
import Modal from "./Modal";
import MarkdownDiff from "./MarkdownDiff";
import { Icon } from "./Icon";

type Phase =
  | "streaming" | "review" | "guiding" | "guiding-streaming"
  | "stale" | "truncated" | "empty" | "error" | "cancelled";

interface Props {
  slug: string;
  note: { title: string; content_md: string };
  onClose: () => void;
  onAccepted: (slug: string) => void;
}

// The live "Rebuild page now" panel. Mobile-first: thoughts/tools collapse into a tappable
// activity strip so the streaming article is the hero; a thumb-reachable footer carries
// Reject / Guide / Accept; Guide opens a Draft⇄Guide segmented view with a composer.
export default function RebuildPanel({ slug, note, onClose, onAccepted }: Props) {
  const navigate = useNavigate();
  const isPrivate = /^kb\/(health|finance)\//i.test(note.title);

  const [phase, setPhase] = useState<Phase>("streaming");
  const [tab, setTab] = useState<"draft" | "guide">("draft");
  const [status, setStatus] = useState("Thinking…");
  const [busy, setBusy] = useState(false);          // a turn is streaming (dots in status)
  const [activityOpen, setActivityOpen] = useState(true);
  const [thoughts, setThoughts] = useState("");
  const [sources, setSources] = useState<{ title: string }[]>([]);
  const [draft, setDraft] = useState("");
  const [warn, setWarn] = useState<string | null>(null);
  const [showDiff, setShowDiff] = useState(false);
  const [thread, setThread] = useState<{ role: "user" | "ai"; text: string }[]>([]);
  const [guideInput, setGuideInput] = useState("");
  const [accepting, setAccepting] = useState(false);

  const runId = useRef<string | null>(null);
  const sse = useRef<SSEHandle | null>(null);
  const sawContent = useRef(false);
  const draftScroll = useRef<HTMLDivElement>(null);

  // ---- event handling ----------------------------------------------------------
  function handle(e: RebuildEvent) {
    switch (e.type) {
      case "run_started": runId.current = e.run_id; break;
      case "sources": setSources(e.items); break;
      case "thinking_delta": setStatus("Thinking…"); setThoughts((t) => t + e.text); break;
      case "content_delta":
        if (!sawContent.current) { sawContent.current = true; setActivityOpen(false); setStatus("Drafting…"); }
        setDraft((d) => d + e.text);
        break;
      case "lint": setWarn(e.message); break;
      case "done": {
        setDraft(e.draft);
        setBusy(false);
        setStatus("Ready");
        if (!e.draft.trim()) { setPhase("empty"); break; }
        if (e.truncated) { setWarn("The draft was cut short (length limit) — review carefully."); setPhase("truncated"); break; }
        setWarn(null);
        setPhase((p) => (p === "guiding-streaming" ? "guiding" : "review"));
        break;
      }
      case "error": setBusy(false); setStatus("Failed"); setPhase("error"); break;
    }
  }

  // Start the initial rebuild on mount; abort on unmount.
  useEffect(() => {
    sawContent.current = false;
    setBusy(true);
    sse.current = rebuildStream(slug, handle);
    sse.current.done.catch(() => { /* aborted / network — UI already reflects state */ });
    return () => { sse.current?.abort(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  // Keep the newest tokens in view while streaming (the modal-body is the scroller).
  useEffect(() => {
    if (!busy) return;
    const scroller = draftScroll.current?.closest(".modal-body");
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
  }, [draft, busy]);

  // ---- actions -----------------------------------------------------------------
  function requestClose() {
    if ((phase === "streaming" || phase === "guiding-streaming") && draft &&
        !confirm("Stop the rebuild? Your page won't change.")) return;
    sse.current?.abort();
    if (runId.current && phase !== "cancelled") rejectRebuild(runId.current).catch(() => {});
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
    try {
      const r = await acceptRebuild(runId.current);
      onAccepted(r.slug);
    } catch (err) {
      const e = err as ApiError;
      if (e.status === 409 && /stale/i.test(e.message)) {
        setWarn("This page changed since the rebuild started — accepting would overwrite that edit.");
        setPhase("stale");
      } else if (e.status === 409) {
        setWarn(e.message);
      } else {
        alert(e.message || "Couldn't accept the rebuild.");
      }
    } finally {
      setAccepting(false);
    }
  }

  function openGuide() { setPhase("guiding"); setTab("guide"); }

  function sendGuide() {
    const text = guideInput.trim();
    if (!text || !runId.current || busy) return;
    setGuideInput("");
    setThread((t) => [...t, { role: "user", text }]);
    setTab("draft");          // flip to Draft so the rewrite is visible as it streams
    setDraft(""); sawContent.current = false;
    setBusy(true); setStatus("Revising…"); setPhase("guiding-streaming"); setActivityOpen(false);
    const h = guideStream(runId.current, text, handle);
    sse.current = h;
    h.done
      .then(() => setThread((t) => [...t, { role: "ai", text: "Updated the draft — take a look." }]))
      .catch(() => { /* aborted / error already surfaced */ });
  }

  // ---- render helpers ----------------------------------------------------------
  const draftMd = (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: makeLinkRenderer(navigate) }}>
      {renderWikiLinks(stripSummarySentinels(draft))}
    </ReactMarkdown>
  );

  const guiding = phase === "guiding" || phase === "guiding-streaming";
  const reviewish = phase === "review" || phase === "truncated" || phase === "stale";

  const footer = (() => {
    if (phase === "streaming") return <button className="ghost rb-foot-btn" onClick={requestClose}>Cancel</button>;
    if (phase === "review" || phase === "truncated")
      return (
        <>
          <button className="ghost danger-hover rb-foot-btn" onClick={doReject}>Reject</button>
          <button className="ghost rb-foot-btn" onClick={openGuide}>Guide</button>
          <button className="primary rb-foot-btn" onClick={doAccept} disabled={accepting}>
            {accepting ? "Saving…" : (phase === "truncated" ? "Accept anyway" : "Accept")}
          </button>
        </>
      );
    if (phase === "stale")
      return (
        <>
          <button className="ghost danger-hover rb-foot-btn" onClick={doReject}>Reject</button>
          <button className="primary rb-foot-btn" onClick={onClose}>Close</button>
        </>
      );
    if (phase === "empty" || phase === "error" || phase === "cancelled")
      return <button className="primary rb-foot-btn" onClick={onClose}>Close</button>;
    if (guiding) {
      if (tab === "guide")
        return (
          <div className="rb-composer">
            <textarea value={guideInput} placeholder="Steer the rewrite…"
                      onChange={(e) => setGuideInput(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendGuide(); } }} />
            <button className="rb-send" onClick={sendGuide} disabled={busy || !guideInput.trim()} aria-label="Send">
              <Icon name="send" size={18} />
            </button>
          </div>
        );
      return (
        <>
          <button className="ghost rb-foot-btn" onClick={() => setTab("guide")}>💬 Guide</button>
          <button className="primary rb-foot-btn" onClick={doAccept} disabled={accepting}>
            {accepting ? "Saving…" : "Accept"}
          </button>
        </>
      );
    }
    return null;
  })();

  const headerExtra = (
    <span className="rb-base-chip">{isPrivate && <span className="rb-lock">🔒</span>}{slug}</span>
  );

  return (
    <Modal title="Rebuild page" onClose={requestClose} footer={footer} headerExtra={headerExtra}>
      <div className="rb-panel">
        {guiding && (
          <div className="rb-seg">
            <button className={tab === "draft" ? "on" : ""} onClick={() => setTab("draft")}>📄 Draft</button>
            <button className={tab === "guide" ? "on" : ""} onClick={() => setTab("guide")}>💬 Guide</button>
          </div>
        )}

        <div className={"rb-activity" + (activityOpen ? " open" : "")}>
          <button className="rb-act-row" onClick={() => setActivityOpen((o) => !o)}>
            <span className="rb-status">
              {busy && <span className="rb-dots"><i /><i /><i /></span>} {status}
            </span>
            <span className="rb-caret">▸</span>
          </button>
          {activityOpen && (
            <div className="rb-act-body">
              {thoughts && <>
                <div className="rb-act-h">Thoughts</div>
                <div className="rb-thoughts">{thoughts}</div>
              </>}
              {sources.length > 0 && <>
                <div className="rb-act-h">Sources ({sources.length})</div>
                <div className="rb-refs">{sources.map((s, i) => <span key={i} className="rb-chip">🔗 {s.title}</span>)}</div>
              </>}
              {!thoughts && sources.length === 0 && <div className="muted" style={{ fontSize: 13 }}>Gathering context…</div>}
            </div>
          )}
        </div>

        {warn && <div className="rb-warn">⚠️ {warn}</div>}

        {/* Draft tab (default) vs Guide tab */}
        {!(guiding && tab === "guide") ? (
          <div className="rb-draft" ref={draftScroll}>
            {reviewish && draft && (
              <div className="rb-draft-bar">
                <span className="rb-phase">{showDiff ? "Changes" : "New draft"}</span>
                <button className="rb-seechg" onClick={() => setShowDiff((s) => !s)}>
                  {showDiff ? "Hide changes" : "See changes"}
                </button>
              </div>
            )}
            <div className="rb-draft-body md">
              {phase === "empty"
                ? <p className="muted">The model didn't produce a draft.</p>
                : showDiff
                  ? <MarkdownDiff before={note.content_md} after={draft} />
                  : draftMd}
            </div>
          </div>
        ) : (
          <div className="rb-guide">
            <div className="rb-thread">
              <div className="rb-msg ai">Tell me how to revise this — tone, what to add or cut. I'll rewrite from what I already gathered (no new lookups).</div>
              {thread.map((m, i) => <div key={i} className={"rb-msg " + (m.role === "user" ? "user" : "ai")}>{m.text}</div>)}
            </div>
            {sources.length > 0 && (
              <div className="rb-ctx"><span className="muted">Working from:</span>
                {sources.map((s, i) => <span key={i} className="rb-chip">🔗 {s.title}</span>)}</div>
            )}
          </div>
        )}
      </div>
    </Modal>
  );
}
