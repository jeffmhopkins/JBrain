import { FormEvent, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import { createEntry, get, post, streamChat, uploadAttachment } from "../api";
import { useGeo, useOnline } from "../hooks";
import StagingPanel from "../components/StagingPanel";
import { Icon } from "../components/Icon";
import { makeLinkRenderer, renderWikiLinks } from "../util";

// 'event' rows are persisted approval records (✓ applied X), kept in the chat
// but excluded from the LLM history server-side.
interface Msg { role: "user" | "assistant" | "event"; content: string; }
type Mode = "entry" | "assisted" | "research";

// Render an applied-action summary with any URL made clickable (so a freshly-minted
// share link is tappable right on the card).
const _URL_RE = /(https?:\/\/[^\s]+)/g;
function renderSummary(text: string) {
  return text.split(_URL_RE).map((part, i) =>
    /^https?:\/\//.test(part)
      ? <a key={i} href={part} target="_blank" rel="noreferrer">{part}</a>
      : <span key={i}>{part}</span>);
}

const MODES: { key: Mode; label: string; icon: string }[] = [
  { key: "entry", label: "Entry", icon: "plus" },
  { key: "assisted", label: "Assisted", icon: "robot" },
  { key: "research", label: "Research", icon: "search" },
];
const PLACEHOLDER: Record<Mode, string> = {
  entry: "Write an entry…",
  assisted: "Talk it out…",
  research: "Ask your brain… (read-only)",
};
// Friendly status shown at the bottom of the conversation while a tool runs.
const TOOL_LABELS: Record<string, string> = {
  search_notes: "Searching your notes…",
  read_note: "Reading a note…",
  search_attachments: "Searching attachments…",
  read_attachment: "Reading an attachment…",
  list_recent_notes: "Looking at recent notes…",
  read_inbox: "Checking the inbox…",
  query_sql: "Querying the database…",
  current_location: "Checking your location…",
  geo_distance: "Measuring distance…",
  nearby_notes: "Finding nearby notes…",
  read_list: "Reading a list…",
  add_list_item: "Updating a list…",
  set_item_checked: "Updating a list…",
  set_item_priority: "Updating a list…",
  add_sublist: "Updating a list…",
  set_tags: "Tagging the note…",
  propose_actions: "Drafting proposed changes…",
};
const toolLabel = (name?: string) => (name && TOOL_LABELS[name]) || "Working…";

export default function Chat() {
  const online = useOnline();
  const geo = useGeo();
  const navigate = useNavigate();
  // Mode persists within a session (so navigating away from chat and back keeps it)
  // but a fresh PWA launch starts a new session → empty → defaults to Entry capture.
  const [mode, setMode] = useState<Mode>(() => (sessionStorage.getItem("jbrain_mode") as Mode) || "entry");
  const [menuOpen, setMenuOpen] = useState(false);

  const [convId, setConvId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [entries, setEntries] = useState<{ text: string; title: string; slug: string }[]>([]);
  const [input, setInput] = useState("");
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [uploadPct, setUploadPct] = useState<number | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [status, setStatus] = useState("");   // live "thinking…/searching…" status while streaming
  const [busy, setBusy] = useState(false);
  const [stagingTick, setStagingTick] = useState(0);
  // Transient chips for actions auto-applied during the current stream; at stream
  // end we reload from the server (which has them as persisted 'event' rows).
  const [applied, setApplied] = useState<{ id: number; summary: string }[]>([]);
  const [undone, setUndone] = useState<Set<number>>(new Set());

  const endRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  // Typewriter reveal for the active assistant turn. The reply arrives over SSE in
  // bursty chunks; we accumulate the full received-so-far into `bufRef` and reveal
  // it a few chars per tick so the visible text flows in at a steady, fluid pace
  // (matching GuidedChat). `shown` drives what slice of the buffer is rendered.
  const bufRef = useRef("");            // full text received for the current turn
  const shownRef = useRef(0);           // chars revealed so far for the current turn
  const streamActiveRef = useRef(false); // true while SSE is still delivering tokens
  // `tick` re-arms the typewriter effect: bumped when new tokens arrive (to wake a
  // caught-up loop) and once per reveal frame (to schedule the next frame).
  const [tick, setTick] = useState(0);
  // Honour reduced-motion: reveal instantly (a plain append) with no animation.
  const reduceMotion = useRef(
    typeof window !== "undefined" && window.matchMedia
      ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
      : false,
  );
  // Write the revealed slice onto the last (assistant) message bubble.
  function paint(n: number) {
    const text = bufRef.current.slice(0, n);
    setMessages((m) => {
      if (m.length === 0 || m[m.length - 1].role !== "assistant") return m;
      const c = [...m];
      c[c.length - 1] = { role: "assistant", content: text };
      return c;
    });
  }
  // The typewriter loop: each frame advances `shown` toward the buffer length. The
  // step scales with how far behind we are so a long reply catches up instead of
  // lagging seconds behind, while short bursts still animate a couple chars at a
  // time. It schedules the next frame by bumping `tick`; it stops (no reschedule)
  // once the buffer is fully revealed and the stream is no longer active.
  useEffect(() => {
    if (reduceMotion.current) return;
    const total = bufRef.current.length;
    const behind = total - shownRef.current;
    if (behind <= 0) return;   // caught up; a new token will re-arm us
    // While tokens are still arriving, reveal at a steady fluid pace (catch-up scaled
    // to the backlog). Once the stream is DONE delivering, drain the remaining buffer
    // fast — there's nothing left to wait for, so the tail shouldn't crawl behind the
    // model (the "slow text at the end").
    const active = streamActiveRef.current;
    const step = active ? Math.max(2, Math.ceil(behind / 8)) : Math.max(28, Math.ceil(behind / 3));
    const id = window.setTimeout(() => {
      shownRef.current = Math.min(bufRef.current.length, shownRef.current + step);
      paint(shownRef.current);
      setTick((t) => t + 1);
    }, active ? 20 : 8);
    return () => clearTimeout(id);
  }, [tick]);
  // Only auto-follow new content when the user is already at the bottom; if they
  // scroll up to read, leave them there even as the reply streams in.
  const atBottomRef = useRef(true);
  const scrollHideTimer = useRef<number>();
  function onMessagesScroll() {
    const el = scrollRef.current;
    if (!el) return;
    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    // Reveal the scrollbar only while scrolling, then auto-hide it.
    el.classList.add("scrolling");
    clearTimeout(scrollHideTimer.current);
    scrollHideTimer.current = window.setTimeout(() => el.classList.remove("scrolling"), 900);
  }

  // Grow the compose box upward as you type, up to ~half the visible height,
  // then scroll inside it.
  useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = "auto";
    const max = Math.round((window.visualViewport?.height ?? window.innerHeight) * 0.5);
    el.style.height = Math.min(el.scrollHeight, max) + "px";
    el.style.overflowY = el.scrollHeight > max ? "auto" : "hidden";
  }, [input]);

  function pick(m: Mode) { setMode(m); sessionStorage.setItem("jbrain_mode", m); setMenuOpen(false); }

  // A swipe-up from the left third of the composer (detected by the shell) cycles the mode.
  useEffect(() => {
    function cycle() {
      setMode((m) => {
        const next = MODES[(MODES.findIndex((x) => x.key === m) + 1) % MODES.length].key;
        sessionStorage.setItem("jbrain_mode", next);
        return next;
      });
    }
    window.addEventListener("jbrain:cyclemode", cycle);
    return () => window.removeEventListener("jbrain:cyclemode", cycle);
  }, []);

  // Swipe navigation lives on the shell body (vertical-only); pages don't wire touch handlers.
  const slideFrom = useRef(16);   // px the mode-flash slides in from (button mode-change)

  // Brief sliding mode-name flash on every switch (swipe or menu).
  const [flashKey, setFlashKey] = useState(0);
  const [showFlash, setShowFlash] = useState(false);
  const flashTimer = useRef<number>();
  const firstRender = useRef(true);
  useEffect(() => {
    if (firstRender.current) { firstRender.current = false; return; }
    setShowFlash(true);
    setFlashKey((k) => k + 1);
    clearTimeout(flashTimer.current);
    flashTimer.current = window.setTimeout(() => setShowFlash(false), 850);
    return () => clearTimeout(flashTimer.current);
  }, [mode]);

  // Assisted + research share ONE conversation/thread; only the per-turn AI permission
  // differs. (Entry has no conversation.)
  const CHAT_CONV_KEY = "jbrain_conv_chat";

  async function loadMessages(id: number) {
    try {
      const rows = await get<{ role: Msg["role"]; content: string }[]>(`/api/chat/conversations/${id}/messages`);
      setMessages(rows.map((r) => ({ role: r.role, content: r.content })));
    } catch { /* keep what we have */ }
  }

  async function newConversation() {
    const { id } = await post("/api/chat/conversations");
    localStorage.setItem(CHAT_CONV_KEY, String(id));
    setConvId(id); setMessages([]); setApplied([]); setUndone(new Set());
  }

  // Restore (or migrate to) the single shared chat thread on first entry into a chat
  // mode. Toggling assisted↔research afterwards keeps the SAME thread (the guard below) —
  // only the AI's permission changes per turn. One-time migration adopts the old
  // per-mode conversation (assisted preferred) into jbrain_conv_chat.
  useEffect(() => {
    if (convId) return;                       // already in a thread → keep it across toggles
    let id = localStorage.getItem(CHAT_CONV_KEY);
    if (!id) {
      id = localStorage.getItem("jbrain_conv_assisted") || localStorage.getItem("jbrain_conv_research");
      if (id) {
        localStorage.setItem(CHAT_CONV_KEY, id);
        localStorage.removeItem("jbrain_conv_assisted");
        localStorage.removeItem("jbrain_conv_research");
      }
    }
    // Load the cached thread in EVERY mode (so entry shows history too), but only spin up a
    // brand-new conversation when in a chat mode — entry alone shouldn't create empty threads.
    if (id) { setConvId(Number(id)); loadMessages(Number(id)); }
    else if (mode !== "entry") { newConversation(); }
  }, [mode, convId]);
  useEffect(() => {
    if (atBottomRef.current) endRef.current?.scrollIntoView({ behavior: "auto" });
  }, [messages, entries]);

  async function undo(id: number) {
    await post(`/api/staging/${id}/undo`);
    setUndone((s) => new Set(s).add(id));
  }

  async function send(e?: FormEvent) {
    e?.preventDefault();
    const text = input.trim();
    if ((!text && !pendingFile) || streaming || busy || !online) return;
    if (text === "/clear") { setInput(""); setEntries([]); newConversation(); return; }
    const coords = await geo.getCoords();   // one-shot GPS fix, only now (if enabled)
    const file = pendingFile;
    setInput(""); setPendingFile(null);

    if (mode === "entry") {
      setBusy(true);
      try {
        const r = await createEntry(text || (file ? file.name : "Untitled"), undefined, coords);
        if (file) await uploadAttachment(r.slug, file, setUploadPct);
        setEntries((xs) => [...xs, { text: text || (file ? `📎 ${file.name}` : ""), title: r.title, slug: r.slug }]);
      } catch (err) {
        // Don't silently lose the entry: put the text back and tell the user.
        setInput(text);
        if (file) setPendingFile(file);
        alert("Couldn't save entry: " + (err instanceof Error ? err.message : "please try again."));
      } finally { setBusy(false); setUploadPct(null); }
      return;
    }

    if (!convId) return;
    let extra = "";
    if (mode === "assisted" && file) {
      // Save the file to a note so there's something to attach it to. Skip
      // auto-analysis: this carrier note has no real content to inform it.
      const r = await createEntry(`Attached file: ${file.name}`, file.name.replace(/\.[^.]+$/, ""), coords);
      await uploadAttachment(r.slug, file, setUploadPct, false);
      setUploadPct(null);
      extra = `\n\n(I attached a file, saved as [[${r.title}]].)`;
    }
    const msg = (text + extra).trim();
    atBottomRef.current = true;   // sending re-engages follow, so you see your message + reply
    setMessages((m) => [...m, { role: "user", content: msg }, { role: "assistant", content: "" }]);
    // Reset the typewriter cleanly for this new turn.
    bufRef.current = ""; shownRef.current = 0; streamActiveRef.current = true;
    setStreaming(true);
    setStatus("Thinking…");
    let errored = false;
    try {
      await streamChat(convId, msg, (ev) => {
        if (ev.type === "token") {
          if (ev.text) setStatus((s) => (s === "Responding…" ? s : "Responding…"));
          // Accumulate into the buffer; the typewriter loop reveals it gradually.
          // With reduced motion, reveal instantly (a plain append).
          bufRef.current += ev.text || "";
          if (reduceMotion.current) {
            shownRef.current = bufRef.current.length;
            paint(shownRef.current);
          } else {
            setTick((t) => t + 1);   // wake/keep the reveal loop going
          }
        } else if (ev.type === "tool") {
          setStatus(toolLabel(ev.tool));
        } else if (ev.type === "staging") {
          setStagingTick((t) => t + 1);
        } else if (ev.type === "applied" && ev.action) {
          setApplied((a) => [...a, ev.action!]);
        } else if (ev.type === "error") {
          errored = true;
          // Show the error immediately — don't typewriter-drip it. Clear the buffer
          // so the reveal loop won't overwrite the ⚠️ message.
          bufRef.current = ""; shownRef.current = 0; streamActiveRef.current = false;
          setMessages((m) => {
            const c = [...m];
            c[c.length - 1] = { role: "assistant", content: `⚠️ ${ev.message}` };
            return c;
          });
        }
      }, coords, mode === "research" ? "research" : "assisted");
      // Stream finished delivering: let the typewriter reveal the remaining buffered
      // text to completion before we swap in the authoritative server copy (which is
      // identical), so the reload is a visual no-op rather than a jarring jump.
      if (!errored && !reduceMotion.current) {
        streamActiveRef.current = false;
        await new Promise<void>((resolve) => {
          const finishRef = () => {
            if (shownRef.current >= bufRef.current.length) { resolve(); return; }
            setTick((t) => t + 1);
            window.setTimeout(finishRef, 24);
          };
          finishRef();
        });
      }
    } finally {
      streamActiveRef.current = false;
      setStreaming(false);
      setStatus("");
      setStagingTick((t) => t + 1);   // chat modes (assisted+research) share staging
      // Re-sync from the server: the authoritative turn + any persisted approval
      // ('event') records, correctly ordered. Skip on error to keep the ⚠️.
      if (!errored && convId) { await loadMessages(convId); setApplied([]); }
    }
  }

  const cur = MODES.find((m) => m.key === mode)!;

  return (
    <div className="chat-wrap">
      {showFlash && (
        <div className="mode-flash" key={flashKey} style={{ ["--from" as any]: slideFrom.current + "px" }}>
          <Icon name={cur.icon} size={16} /> {cur.label}
        </div>
      )}
      <div className="messages" ref={scrollRef} onScroll={onMessagesScroll}>
        {/* ONE shared view for every mode. The conversation thread always shows; entry
            just SAVES (no AI turn) and appends its saved-note chips here too. */}
        {messages.length === 0 && entries.length === 0 && (
          <div className="msg assistant muted">
            {mode === "entry"
              ? "Type below and Send — it’s saved straight to your wiki (the AI isn’t involved)."
              : mode === "research"
              ? "Ask anything about your notes — I only read; I won’t change anything."
              : "Tell me what you want to capture. I’ll ask questions, then propose a note to confirm."}
          </div>
        )}
        {messages.map((m, i) => {
          if (m.role === "event") {
            let ev: { summary: string; undo_id?: number };
            try { ev = JSON.parse(m.content); } catch { ev = { summary: m.content }; }
            return (
              <div key={i} className="applied-chip">
                <span>✓ {renderSummary(ev.summary)}</span>
                {ev.undo_id == null ? null : undone.has(ev.undo_id)
                  ? <span className="muted" style={{ fontSize: 12 }}>undone</span>
                  : <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => undo(ev.undo_id!)}>Undo</button>}
              </div>
            );
          }
          // Empty assistant placeholder (waiting on the first token) → render
          // nothing; the status bar at the bottom shows "Thinking…" instead.
          if (m.role === "assistant" && !m.content) return null;
          return (
            <div key={i} className={`msg ${m.role}`}>
              {m.role === "assistant" ? (
                <div className="md msg-md">
                  <ReactMarkdown components={{ a: makeLinkRenderer(navigate) }}>{renderWikiLinks(m.content)}</ReactMarkdown>
                </div>
              ) : (
                m.content
              )}
            </div>
          );
        })}
        {/* Entry saves (this session): a user bubble + a link to the saved note. */}
        {entries.map((en, i) => {
          const label = en.title.startsWith("notes/daily/")
            ? ((en.text.split("\n").find((l) => l.trim()) || "entry").trim().slice(0, 40) || "entry")
            : en.title.replace(/^notes\//, "");
          return (
            <div key={`en${i}`} style={{ display: "contents" }}>
              {en.text && <div className="msg user">{en.text}</div>}
              <Link to={`/note/${en.slug}`} className="saved-chip"><Icon name="check" size={14} /> Saved: {label}</Link>
            </div>
          );
        })}
        {applied.map((a) => (
          <div key={`a${a.id}`} className="applied-chip">
            <span>✓ {renderSummary(a.summary)}</span>
            {undone.has(a.id)
              ? <span className="muted" style={{ fontSize: 12 }}>undone</span>
              : <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => undo(a.id)}>Undo</button>}
          </div>
        ))}
        {/* Pending proposals are global → keep them approvable in any mode. */}
        <StagingPanel
          tick={stagingTick}
          onChange={() => { setStagingTick((t) => t + 1); if (convId && !streaming) loadMessages(convId); }}
        />
        {streaming && (
          <div className="chat-status" aria-live="polite">
            <span className="typing-dots"><span /><span /><span /></span>
            <span className="chat-status-text">{status || "Thinking…"}</span>
          </div>
        )}
        <div ref={endRef} />
      </div>

      {/* Compose box (rounded), shared across modes. */}
      <div className="composer-box">
        {pendingFile && (
          <div className="attach-chip">
            <Icon name="clip" size={14} /> {pendingFile.name}
            <button className="icon-btn" style={{ padding: 2 }} onClick={() => setPendingFile(null)}>✕</button>
          </div>
        )}
        {uploadPct !== null && (
          <div className="attach-chip">
            <Icon name="clip" size={14} /> {uploadPct >= 100 ? "Processing attachment…" : `Uploading… ${uploadPct}%`}
          </div>
        )}
        <textarea
          ref={taRef}
          rows={1}
          placeholder={online ? PLACEHOLDER[mode] : "Offline — reconnect to continue"}
          value={input} disabled={!online}
          onChange={(e) => setInput(e.target.value)}
        />
        <div className="composer-row">
          <span className="mode-wrap">
            <button className="mode-chip" onClick={() => setMenuOpen((o) => !o)}>
              <Icon name={cur.icon} size={18} /> {cur.label}
            </button>
            {menuOpen && (
              <div className="mode-menu">
                {MODES.map((m) => (
                  <button key={m.key} onClick={() => pick(m.key)}>
                    <Icon name={m.icon} size={16} /> {m.label}
                  </button>
                ))}
              </div>
            )}
          </span>
          <span className="spacer" />
          {mode !== "research" && (
            <>
              <input ref={fileRef} type="file" style={{ display: "none" }}
                     onChange={(e) => {
                       const f = e.target.files?.[0];
                       if (f) { if (f.size > 10 * 1024 * 1024) alert("File too large (10 MB max)."); else setPendingFile(f); }
                       e.currentTarget.value = "";
                     }} />
              <button className="icon-btn" title="Attach file" onClick={() => fileRef.current?.click()}><Icon name="clip" /></button>
            </>
          )}
          <button className="icon-btn send" title="Send" onClick={() => send()}
                  disabled={streaming || busy || !online || (!input.trim() && !pendingFile)}><Icon name="send" /></button>
        </div>
      </div>
    </div>
  );
}
