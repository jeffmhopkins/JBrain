import { FormEvent, TouchEvent, useEffect, useRef, useState } from "react";
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

const MODES: { key: Mode; label: string; icon: string }[] = [
  { key: "entry", label: "Entry", icon: "plus" },
  { key: "assisted", label: "Assisted", icon: "robot" },
  { key: "research", label: "Research", icon: "search" },
];
const PLACEHOLDER: Record<Mode, string> = {
  entry: "Write an entry… (logged by date — no title needed)",
  assisted: "Talk it out…",
  research: "Ask your brain… (read-only)",
};

export default function Chat() {
  const online = useOnline();
  const geo = useGeo();
  const navigate = useNavigate();
  const [mode, setMode] = useState<Mode>(() => (localStorage.getItem("jbrain_mode") as Mode) || "entry");
  const [menuOpen, setMenuOpen] = useState(false);

  const [convId, setConvId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [entries, setEntries] = useState<{ text: string; title: string; slug: string }[]>([]);
  const [input, setInput] = useState("");
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [uploadPct, setUploadPct] = useState<number | null>(null);
  const [streaming, setStreaming] = useState(false);
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

  function pick(m: Mode) { setMode(m); localStorage.setItem("jbrain_mode", m); setMenuOpen(false); }

  // Swipe left/right across the conversation to move between modes (wraps around).
  const touchStart = useRef<{ x: number; y: number } | null>(null);
  const slideFrom = useRef(16);   // px the mode-flash slides in from (swipe direction)
  function onTouchStart(e: TouchEvent) {
    const t = e.touches[0];
    touchStart.current = { x: t.clientX, y: t.clientY };
  }
  function onTouchEnd(e: TouchEvent) {
    const s = touchStart.current;
    touchStart.current = null;
    if (!s) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - s.x, dy = t.clientY - s.y;
    // Require a clear, mostly-horizontal swipe so it doesn't fight vertical scroll.
    if (Math.abs(dx) < 60 || Math.abs(dx) < Math.abs(dy) * 1.5) return;
    // Carousel of 4 stops: Entry ↔ Assisted ↔ Research ↔ Advanced (wraps).
    const n = MODES.length;
    const i = MODES.findIndex((m) => m.key === mode);
    const ni = (i + (dx < 0 ? 1 : -1) + (n + 1)) % (n + 1);
    if (ni === n) { navigate("/advanced"); return; }   // the 4th stop is Advanced
    slideFrom.current = dx < 0 ? 22 : -22;
    pick(MODES[ni].key);
  }

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

  const convKey = (m: Mode) => `jbrain_conv_${m}`;

  async function loadMessages(id: number) {
    try {
      const rows = await get<{ role: Msg["role"]; content: string }[]>(`/api/chat/conversations/${id}/messages`);
      setMessages(rows.map((r) => ({ role: r.role, content: r.content })));
    } catch { /* keep what we have */ }
  }

  async function newConversation() {
    const { id } = await post("/api/chat/conversations");
    localStorage.setItem(convKey(mode), String(id));
    setConvId(id); setMessages([]); setApplied([]); setUndone(new Set());
  }

  // On entering a chat mode, restore that mode's saved conversation (so the chat
  // survives navigating to Advanced and back); only create one if none is saved.
  useEffect(() => {
    if (mode === "entry") return;
    const saved = localStorage.getItem(convKey(mode));
    if (saved) { setConvId(Number(saved)); loadMessages(Number(saved)); }
    else { newConversation(); }
  }, [mode]);
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
    if (mode !== "entry" && text === "/clear") { setInput(""); newConversation(); return; }
    const coords = geo.enabled ? geo.coords : null;
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
      // Save the file to a note so there's something to attach it to.
      const r = await createEntry(`Attached file: ${file.name}`, file.name.replace(/\.[^.]+$/, ""), coords);
      await uploadAttachment(r.slug, file, setUploadPct);
      setUploadPct(null);
      extra = `\n\n(I attached a file, saved as [[${r.title}]].)`;
    }
    const msg = (text + extra).trim();
    atBottomRef.current = true;   // sending re-engages follow, so you see your message + reply
    setMessages((m) => [...m, { role: "user", content: msg }, { role: "assistant", content: "" }]);
    setStreaming(true);
    let errored = false;
    try {
      await streamChat(convId, msg, (ev) => {
        if (ev.type === "token") {
          setMessages((m) => {
            const c = [...m];
            c[c.length - 1] = { role: "assistant", content: c[c.length - 1].content + (ev.text || "") };
            return c;
          });
        } else if (ev.type === "staging") {
          setStagingTick((t) => t + 1);
        } else if (ev.type === "applied" && ev.action) {
          setApplied((a) => [...a, ev.action!]);
        } else if (ev.type === "error") {
          errored = true;
          setMessages((m) => {
            const c = [...m];
            c[c.length - 1] = { role: "assistant", content: `⚠️ ${ev.message}` };
            return c;
          });
        }
      }, coords, mode === "research" ? "research" : "assisted");
    } finally {
      setStreaming(false);
      if (mode === "assisted") setStagingTick((t) => t + 1);
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
      <div className="messages" ref={scrollRef} onScroll={onMessagesScroll}
           onTouchStart={onTouchStart} onTouchEnd={onTouchEnd}>
        {mode === "entry" ? (
          entries.length === 0
            ? <div className="msg assistant muted">Type below and Send — it's saved straight to your wiki.</div>
            : entries.map((en, i) => {
                // Dated captures have an opaque path title (notes/daily/…); show a
                // friendly label from the entry's first line instead. Titled notes
                // (assisted attachments) keep their own name (root stripped).
                const label = en.title.startsWith("notes/daily/")
                  ? ((en.text.split("\n").find((l) => l.trim()) || "entry").trim().slice(0, 40) || "entry")
                  : en.title.replace(/^notes\//, "");
                return (
                  <div key={i} style={{ display: "contents" }}>
                    {en.text && <div className="msg user">{en.text}</div>}
                    <Link to={`/note/${en.slug}`} className="saved-chip"><Icon name="check" size={14} /> Saved: {label}</Link>
                  </div>
                );
              })
        ) : (
          <>
            {messages.length === 0 && (
              <div className="msg assistant muted">
                {mode === "research"
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
                    <span>✓ {ev.summary}</span>
                    {ev.undo_id == null ? null : undone.has(ev.undo_id)
                      ? <span className="muted" style={{ fontSize: 12 }}>undone</span>
                      : <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => undo(ev.undo_id!)}>Undo</button>}
                  </div>
                );
              }
              return (
                <div key={i} className={`msg ${m.role}`}>
                  {m.role === "assistant" && m.content ? (
                    <div className="md msg-md">
                      <ReactMarkdown components={{ a: makeLinkRenderer(navigate) }}>{renderWikiLinks(m.content)}</ReactMarkdown>
                    </div>
                  ) : (
                    m.content || (streaming && i === messages.length - 1 ? "…" : "")
                  )}
                </div>
              );
            })}
            {mode === "assisted" && applied.map((a) => (
              <div key={`a${a.id}`} className="applied-chip">
                <span>✓ {a.summary}</span>
                {undone.has(a.id)
                  ? <span className="muted" style={{ fontSize: 12 }}>undone</span>
                  : <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => undo(a.id)}>Undo</button>}
              </div>
            ))}
            {mode === "assisted" && (
              <StagingPanel
                tick={stagingTick}
                onChange={() => { setStagingTick((t) => t + 1); if (convId && !streaming) loadMessages(convId); }}
              />
            )}
          </>
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
