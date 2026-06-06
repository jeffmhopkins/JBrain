import { FormEvent, TouchEvent as ReactTouchEvent, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import { approveExternalLookup, createEntry, denyExternalLookup, extractLabs, get, getMedicalDests, MAX_ATTACHMENT_BYTES, post, setMedicalDests, streamChat, uploadAttachment } from "../api";
import { useGeo, useOnline, useTts, useTtsEnabled } from "../hooks";
import StagingPanel from "../components/StagingPanel";
import LabChartCard from "../components/LabChartCard";
import { Icon } from "../components/Icon";
import { linkifyAddresses, renderWikiLinks } from "../util";
import { makeChatLinkRenderer } from "../components/CitationLink";
import { toolLabel } from "../toolLabels";
import ToolHistory from "../components/ToolHistory";
import { clearConversationSteps } from "../api";
import { shouldOpenHistoryOnSwipe } from "../swipeGesture";

// 'event' rows are persisted approval records (✓ applied X), kept in the chat
// but excluded from the LLM history server-side. `id` (when present) tags an
// optimistic turn's user/assistant rows so the stream can target them by identity
// instead of by position — robust to anything else mutating the list mid-send.
// `id` (when present) tags an optimistic turn so the stream can target it by identity.
// `dbId` is the persisted message row id (used to fetch its tool-call history); `stepCount`
// is how many tools that reply ran (drives the history pill). Both arrive from loadMessages.
interface Msg { role: "user" | "assistant" | "event"; content: string; id?: number; dbId?: number; stepCount?: number; }
type Mode = "entry" | "medical" | "assisted" | "research" | "analyze";

// Render an applied-action summary with any URL made clickable (so a freshly-minted
// share link is tappable right on the card).
const _URL_RE = /(https?:\/\/[^\s]+)/g;
function renderSummary(text: string) {
  return text.split(_URL_RE).map((part, i) =>
    /^https?:\/\//.test(part)
      ? <a key={i} href={part} target="_blank" rel="noreferrer">{part}</a>
      : <span key={i}>{part}</span>);
}

// Flatten the reply's Markdown into something that reads cleanly aloud: drop wiki/link
// syntax, emphasis, headings, code fences and list bullets, keeping the words.
function speechText(md: string): string {
  return md
    .replace(/```[\s\S]*?```/g, " ")              // code blocks
    .replace(/`([^`]+)`/g, "$1")                  // inline code
    .replace(/\[\[([^\]|]+)(?:\|[^\]]+)?\]\]/g, "$1")  // [[wiki|alias]] → text
    .replace(/!?\[([^\]]+)\]\([^)]+\)/g, "$1")    // [text](url) / images → text
    .replace(/^#{1,6}\s+/gm, "")                  // headings
    .replace(/^\s*[-*+]\s+/gm, "")                // list bullets
    .replace(/(\*\*|__|\*|_|~~)/g, "")            // emphasis markers
    .replace(/[ \t]+/g, " ")
    .trim();
}

const MODES: { key: Mode; label: string; icon: string; hint: string }[] = [
  { key: "entry", label: "Entry", icon: "plus", hint: "Straight to your wiki · no AI" },
  { key: "medical", label: "Medical", icon: "medical", hint: "Labs, notes & procedures" },
  { key: "assisted", label: "Assisted", icon: "robot", hint: "Talk it out — AI proposes notes" },
  { key: "research", label: "Research", icon: "search", hint: "Read-only · ask your brain" },
  { key: "analyze", label: "Analyze", icon: "robot", hint: "Read-only · reason over your brain" },
];
const PLACEHOLDER: Record<Mode, string> = {
  entry: "Write an entry…",
  medical: "Log a lab, note, procedure…",
  assisted: "Talk it out…",
  research: "Ask your brain… (read-only)",
  analyze: "Ask an in-depth question… (read-only)",
};
// Friendly tool labels (the "Searching your notes…" status, and the per-reply tool-call
// history) live in ../toolLabels so the streaming status and the history view never drift.

// How long a send will wait for a location stamp before posting without one. A cached
// fix (getCoords' maximumAge: 60s) returns in single-digit ms; a cold fix that needs
// the GPS radio is dropped after this budget rather than freezing the UI for up to 10s.
// The stamp is best-effort metadata, not the point of the post.
const GEO_MAX_WAIT = 1500;

// Module-scoped (survives route changes): set when the user navigates AWAY from chat, so on return
// the next message starts with a fresh context (stale prior answers cleared server-side).
let _leftChatAt: number | null = null;

export default function Chat() {
  const online = useOnline();
  const geo = useGeo();
  const tts = useTts();                 // on-device speech (saved voice + speed)
  const ttsOn = useTtsEnabled();        // top-bar "read replies aloud" toggle
  const navigate = useNavigate();
  // Mode persists within a session (so navigating away from chat and back keeps it)
  // but a fresh PWA launch starts a new session → empty → defaults to Research mode.
  const [mode, setMode] = useState<Mode>(() => (sessionStorage.getItem("jbrain_mode") as Mode) || "research");
  const [menuOpen, setMenuOpen] = useState(false);
  // Medical-mode destination picklist (notes/medical/<dest>/…), lazily loaded the first
  // time Medical mode is used; the chosen destination persists per device.
  const [dests, setDests] = useState<string[]>([]);
  const [curDest, setCurDest] = useState<string>(() => localStorage.getItem("jbrain_med_dest") || "");

  const [convId, setConvId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  // Which assistant replies have their tool-call history panel open (keyed by persisted
  // message id). Opened by tapping the reply's "n steps" pill OR a guarded left-swipe on it.
  const [openSteps, setOpenSteps] = useState<Set<number>>(new Set());
  const toggleSteps = (id: number) =>
    setOpenSteps((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const swipeRef = useRef<{ x: number; y: number } | null>(null);
  function bubbleTouchStart(e: ReactTouchEvent<HTMLDivElement>) {
    const t = e.touches[0];
    swipeRef.current = { x: t.clientX, y: t.clientY };
  }
  function bubbleTouchEnd(e: ReactTouchEvent<HTMLDivElement>, dbId?: number) {
    const s = swipeRef.current; swipeRef.current = null;
    if (!s || dbId == null) return;
    const t = e.changedTouches[0];
    const selLen = window.getSelection?.()?.toString().length ?? 0;
    if (!shouldOpenHistoryOnSwipe(s, { x: t.clientX, y: t.clientY }, window.innerWidth, selLen)) return;
    setOpenSteps((set) => (set.has(dbId) ? set : new Set(set).add(dbId)));   // open (don't toggle shut)
  }
  // `pending` entries are the optimistic user bubble shown the instant Send is hit (entry/
  // medical have no streamed reply, so this is their only immediate feedback). The save
  // resolves the matching `id` in place (fills title/slug → "Saved:" chip) or drops it on
  // error. Keyed by a unique `id` so reconciliation never touches the wrong row.
  const [entries, setEntries] = useState<{ id: number; text: string; title: string; slug: string; pending?: boolean }[]>([]);
  const [input, setInput] = useState("");
  // Files staged on the composer before Send. Plural so one capture can carry many
  // attachments (the note page already supports this; this closes the compose-box gap).
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [uploadPct, setUploadPct] = useState<number | null>(null);
  const [uploadLabel, setUploadLabel] = useState("");   // "(2/3) photo.jpg" while a multi-file batch uploads
  const [streaming, setStreaming] = useState(false);
  const [status, setStatus] = useState("");   // live "thinking…/searching…" status while streaming
  const [busy, setBusy] = useState(false);
  const [stagingTick, setStagingTick] = useState(0);
  // Transient chips for actions auto-applied during the current stream; at stream
  // end we reload from the server (which has them as persisted 'event' rows).
  const [applied, setApplied] = useState<{ id: number; summary: string }[]>([]);
  // Charts streamed during the current turn (transient; persisted as event rows on reload).
  const [charts, setCharts] = useState<{ analyte: string; unit?: string | null; from?: string | null; to?: string | null; title?: string }[]>([]);
  // Pending external-lookup proposals (the medical_reference gate): the assistant wants to send a
  // term to MedlinePlus; nothing is sent until the owner approves the exact term via an inline chip.
  const [proposals, setProposals] = useState<{ id: number; term: string }[]>([]);
  const [undone, setUndone] = useState<Set<number>>(new Set());

  const endRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  // Synchronous re-entrancy latch: streaming/busy gate the UI but they're async React
  // state, so a second tap during a send's async pre-flight (GPS, conv-create, upload)
  // could start a second turn before that state flushed. This ref blocks it immediately.
  const sendingRef = useRef(false);
  // Mirror convId into a ref + track any in-flight creation so a send that races ahead of
  // newConversation() can await the id (ensureConversation) instead of being dropped.
  const convIdRef = useRef<number | null>(null);
  const convPromiseRef = useRef<Promise<number> | null>(null);
  const entrySeq = useRef(0);   // unique id for optimistic capture-mode entry rows
  const turnSeq = useRef(0);    // unique id for optimistic chat message rows (user/assistant)
  const activeAsstRef = useRef<number | null>(null);   // id of the assistant bubble the stream is painting

  // Typewriter reveal for the active assistant turn. The reply arrives over SSE in
  // bursty chunks; we accumulate the full received-so-far into `bufRef` and reveal
  // it a few chars per tick so the visible text flows in at a steady, fluid pace
  // (matching GuidedChat). `shown` drives what slice of the buffer is rendered.
  const bufRef = useRef("");            // full text received for the current turn
  const shownRef = useRef(0);           // chars revealed so far for the current turn
  const streamActiveRef = useRef(false); // true while SSE is still delivering tokens
  // Set when the user left the app (tab/app backgrounded) or navigated away from chat and came
  // back — the next message then re-grounds from a fresh context instead of reusing stale answers.
  const freshCtxRef = useRef(false);
  // `tick` re-arms the typewriter effect: bumped when new tokens arrive (to wake a
  // caught-up loop) and once per reveal frame (to schedule the next frame).
  const [tick, setTick] = useState(0);
  // Honour reduced-motion: reveal instantly (a plain append) with no animation.
  const reduceMotion = useRef(
    typeof window !== "undefined" && window.matchMedia
      ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
      : false,
  );
  // Write the revealed slice onto THIS turn's assistant bubble (by id), so a concurrent
  // setMessages (a restore/toggle reload, a future event row) can't make us paint the
  // wrong row. No-ops if that bubble is gone (e.g. already re-synced from the server).
  function paint(n: number) {
    const text = bufRef.current.slice(0, n);
    const id = activeAsstRef.current;
    setMessages((m) => {
      const i = m.findIndex((x) => x.id === id);
      if (i < 0) return m;
      const c = [...m];
      c[i] = { ...c[i], content: text };
      return c;
    });
  }
  // Fresh-context signal: mark the next message "fresh" when the user backgrounds the app/tab
  // (visibilitychange → hidden) or navigates away from chat and returns (the module marker set on
  // unmount). A long idle gap is also caught server-side; this covers quick away-and-back.
  useEffect(() => {
    if (_leftChatAt != null) { freshCtxRef.current = true; _leftChatAt = null; }
    const onHide = () => { if (document.visibilityState === "hidden") freshCtxRef.current = true; };
    document.addEventListener("visibilitychange", onHide);
    return () => {
      document.removeEventListener("visibilitychange", onHide);
      _leftChatAt = Date.now();   // leaving chat → next return starts fresh
    };
  }, []);

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

  // Load the medical destinations the first time Medical mode is entered.
  useEffect(() => {
    if (mode !== "medical" || dests.length) return;
    getMedicalDests().then(({ names }) => {
      setDests(names);
      setCurDest((c) => (c && names.includes(c) ? c : names[0] || ""));
    }).catch(() => { /* offline — keep an empty picker */ });
  }, [mode, dests.length]);
  function pickDest(d: string) { setCurDest(d); localStorage.setItem("jbrain_med_dest", d); }
  // Add a new destination inline (full management lives in Advanced → Medical).
  async function addDest() {
    const name = window.prompt("New medical destination (e.g. “2026-03 Admission”)")?.trim();
    if (!name) return;
    try {
      const { names } = await setMedicalDests([...dests, name]);
      setDests(names);
      pickDest(names.find((n) => n.toLowerCase() === name.toLowerCase()) || names[names.length - 1] || name);
    } catch { alert("Couldn’t save that destination — please try again."); }
  }

  // A vertical swipe in the left third of the composer (detected by the shell) carousels the
  // mode — swipe up advances, swipe down reverses (direction arrives as event.detail ±1).
  useEffect(() => {
    function cycle(e: Event) {
      const dir = (e as CustomEvent).detail === -1 ? -1 : 1;
      setMode((m) => {
        const i = MODES.findIndex((x) => x.key === m);
        const next = MODES[(i + dir + MODES.length) % MODES.length].key;
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
      const rows = await get<{ id: number; role: Msg["role"]; content: string; step_count?: number }[]>(`/api/chat/conversations/${id}/messages`);
      // A turn may have started streaming while this fetch was in flight (e.g. the user
      // sent before the initial restore-load resolved). Don't clobber the optimistic /
      // actively-streaming view with stale server rows — the post-stream re-sync handles it.
      if (streamActiveRef.current) return;
      setMessages(rows.map((r) => ({ role: r.role, content: r.content, dbId: r.id, stepCount: r.step_count })));
    } catch { /* keep what we have */ }
  }

  // Low-level: POST a new conversation and adopt its id. Does NOT touch the message view,
  // so it's safe to call mid-send (it won't wipe an optimistic bubble already on screen).
  async function createConversation(): Promise<number> {
    const { id } = await post<{ id: number }>("/api/chat/conversations");
    localStorage.setItem(CHAT_CONV_KEY, String(id));
    convIdRef.current = id;
    setConvId(id);
    return id;
  }

  // /clear and friends: start a brand-new thread AND wipe the current view.
  async function newConversation(): Promise<number> {
    setMessages([]); setApplied([]); setCharts([]); setUndone(new Set());
    return createConversation();
  }

  // Resolve a conversation id for a chat turn, awaiting any creation already in flight
  // (deduped via convPromiseRef) or starting one — so a send issued before convId is set
  // is queued, never silently dropped, and never disturbs the optimistic message view.
  // The promise is cleared on settle so a transient create failure doesn't wedge sending.
  function ensureConversation(): Promise<number> {
    if (convIdRef.current) return Promise.resolve(convIdRef.current);
    if (!convPromiseRef.current) {
      convPromiseRef.current = createConversation().finally(() => { convPromiseRef.current = null; });
    }
    return convPromiseRef.current;
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
    // Load the cached thread in EVERY mode (so entry shows history too). With no cached
    // thread we do NOT eagerly create one: send() lazily creates a conversation on the
    // first chat turn via ensureConversation(), so just entering a chat mode (or launching
    // straight into one) never leaves an orphan empty thread behind.
    if (id) { convIdRef.current = Number(id); setConvId(Number(id)); loadMessages(Number(id)); }
  }, [mode, convId]);
  useEffect(() => {
    if (atBottomRef.current) endRef.current?.scrollIntoView({ behavior: "auto" });
  }, [messages, entries]);

  async function undo(id: number) {
    await post(`/api/staging/${id}/undo`);
    setUndone((s) => new Set(s).add(id));
  }

  // Shared failure handling for an optimistic send: put the user's text/attachment back
  // (but never clobber something they retyped into the now-empty composer) and tell them.
  // Each caller still removes its own optimistic artifact (entry row / chat bubble) first.
  function rollbackComposer(text: string, files: File[], err: unknown, prefix: string) {
    setInput((cur) => cur.trim() ? cur : text);
    if (files.length) setPendingFiles((cur) => cur.length ? cur : files);
    alert(`${prefix}: ` + (err instanceof Error ? err.message : "please try again."));
  }

  // Upload a batch sequentially to one note, surfacing per-file progress. A single file's
  // failure is non-fatal: the note + the files that did land are kept (attachments are
  // additive), and the caller decides how to surface the skipped ones.
  async function uploadAll(slug: string, files: File[], analyze: boolean): Promise<string[]> {
    const errors: string[] = [];
    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      setUploadLabel(files.length > 1 ? `(${i + 1}/${files.length}) ${f.name}` : "");
      try { await uploadAttachment(slug, f, setUploadPct, analyze); }
      catch (e: any) { errors.push(`${f.name}: ${e?.message || "upload failed"}`); }
      setUploadPct(null);
    }
    setUploadLabel("");
    return errors;
  }

  async function send(e?: FormEvent, override?: string) {
    e?.preventDefault();
    const text = (override ?? input).trim();
    // streaming/busy gate the UI but are async React state; sendingRef is the synchronous
    // latch that blocks a second tap during this send's async pre-flight (GPS, conv-create,
    // upload) before that state has flushed.
    if ((!text && pendingFiles.length === 0) || streaming || busy || sendingRef.current || !online) return;
    if (text === "/clear") {
      // Also wipe the cleared thread's stored tool-call history (full-raw logs shouldn't
      // pile up across throwaway chats). Fire-and-forget against the OLD conversation id.
      const old = convIdRef.current;
      setInput(""); setEntries([]); newConversation();
      if (old) clearConversationSteps(old).catch(() => { /* best-effort cleanup */ });
      return;
    }
    if (mode === "medical" && !curDest) { alert("Pick or add a medical destination first."); return; }
    sendingRef.current = true;
    setProposals([]);   // a new turn supersedes any pending external-lookup chips
    // Both chat modes stream a spoken-able prose reply; entry/medical don't.
    const speakable = mode === "assisted" || mode === "research" || mode === "analyze";
    // Unlock speech NOW, inside the tap's user gesture, so reading the reply aloud
    // later (from an async callback) isn't blocked by mobile autoplay policies.
    if (speakable && ttsOn.enabled) tts.prime();

    // --- Optimistic UI FIRST (synchronous), async work second. Everything the user should
    // see the instant they hit Send happens here, BEFORE we await anything: the composer
    // clears and the user's message/bubble appears immediately, regardless of how slow the
    // GPS fix or the network turns out to be.
    const files = pendingFiles;
    const fileLabel = files.length === 1 ? `📎 ${files[0].name}` : files.length ? `📎 ${files.length} files` : "";
    const bubble = text || fileLabel;
    setInput(""); setPendingFiles([]);
    // Best-effort location stamp: start it now (so it runs concurrently with the post) but
    // never block on it — capped at GEO_MAX_WAIT, resolves null if the radio is cold. The
    // post fires un-stamped rather than freezing; awaited just before each network call.
    const coordsP = geo.getCoords(GEO_MAX_WAIT);

    if (mode === "entry" || mode === "medical") {
      // Optimistic capture bubble — the only immediate feedback these modes get (no reply).
      const enId = ++entrySeq.current;
      setEntries((xs) => [...xs, { id: enId, text: bubble, title: "", slug: "", pending: true }]);
      setBusy(true);
      try {
        const dest = mode === "medical" ? (curDest || undefined) : undefined;
        const coords = await coordsP;
        const r = await createEntry(text || (files.length ? files[0].name : "Untitled"), undefined, coords, dest);
        let labMsg = "";
        if (files.length) {
          // Images auto-analyze server-side (analyze defaults to true).
          const errs = await uploadAll(r.slug, files, true);
          if (errs.length) labMsg += ` · ${errs.length} file(s) couldn't attach`;
          // Medical mode: if any upload was a lab PDF, STAGE its values for review. extractLabs
          // is note-level — it stages every PDF on the note — so call it once after the batch.
          if (mode === "medical" && files.some((f) => /\.pdf$/i.test(f.name))) {
            try {
              const lab = await extractLabs(r.slug);
              if (lab.staged) labMsg += ` · ${lab.staged} lab results extracted — open the note to review & approve`;
            } catch { /* non-fatal: the note + attachments are saved regardless */ }
          }
        }
        // Resolve the optimistic row in place (by id) → renders the "Saved:" chip.
        setEntries((xs) => xs.map((en) => en.id === enId
          ? { id: enId, text: bubble + labMsg, title: r.title, slug: r.slug }
          : en));
      } catch (err) {
        // Don't silently lose the entry: drop the optimistic row, restore the composer.
        setEntries((xs) => xs.filter((en) => en.id !== enId));
        rollbackComposer(text, files, err, "Couldn't save entry");
      } finally { setBusy(false); setUploadPct(null); sendingRef.current = false; }
      return;
    }

    // --- Chat modes (assisted/research). Show the user bubble + spinner immediately —
    // even before the conversation id resolves — then queue the actual send behind
    // ensureConversation() so a turn fired before convId is set is never dropped.
    atBottomRef.current = true;   // sending re-engages follow, so you see your message + reply
    const userId = ++turnSeq.current;
    const asstId = ++turnSeq.current;
    activeAsstRef.current = asstId;   // paint()/the error handler target this bubble by id
    setMessages((m) => [...m, { role: "user", content: bubble, id: userId }, { role: "assistant", content: "", id: asstId }]);
    let bubbleShown = true;
    // Reset the typewriter cleanly for this new turn.
    bufRef.current = ""; shownRef.current = 0; streamActiveRef.current = true;
    tts.stop();   // cut off any reply still being read aloud from the previous turn
    setStreaming(true);
    setStatus("Thinking…");
    let errored = false;
    let cid: number | null = null;
    // Remove THIS turn's optimistic rows (by id) so a throw before any token is delivered
    // doesn't leave a dangling half-turn — and never deletes some other row by position.
    const dropOptimisticBubble = () => {
      if (!bubbleShown) return;
      bubbleShown = false;
      setMessages((m) => m.filter((x) => x.id !== userId && x.id !== asstId));
    };
    try {
      cid = await ensureConversation();
      let extra = "";
      if (mode === "assisted" && files.length) {
        // One carrier note PER file (each file gets its own titled note + wikilink) so
        // there's something to attach each to. Skip auto-analysis on images: these carrier
        // notes have no real content to inform it (audio still auto-transcribes, server-side).
        const coords = await coordsP;
        const links: string[] = [];
        const failed: string[] = [];
        for (let i = 0; i < files.length; i++) {
          const f = files[i];
          setUploadLabel(files.length > 1 ? `(${i + 1}/${files.length}) ${f.name}` : "");
          try {
            const r = await createEntry(`Attached file: ${f.name}`, f.name.replace(/\.[^.]+$/, ""), coords);
            await uploadAttachment(r.slug, f, setUploadPct, false);
            links.push(`[[${r.title}]]`);
          } catch { failed.push(f.name); }
          setUploadPct(null);
        }
        setUploadLabel("");
        const saved = links.length ? `saved as ${links.join(", ")}` : "but none could be saved";
        extra = `\n\n(I attached ${files.length === 1 ? "a file" : `${files.length} files`}, ${saved}.`
          + (failed.length ? ` ${failed.length} couldn't attach.` : "") + ")";
        // Fold the saved-note reference(s) into this turn's user bubble (by id).
        setMessages((m) => m.map((x) => x.id === userId ? { ...x, content: (text + extra).trim() } : x));
      }
      const msg = (text + extra).trim();
      const coords = await coordsP;   // resolved (or null) by now; bounded by GEO_MAX_WAIT
      const freshCtx = freshCtxRef.current; freshCtxRef.current = false;
      await streamChat(cid, msg, (ev) => {
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
        } else if (ev.type === "external_proposal" && ev.id && ev.term) {
          setProposals((p) => p.some((x) => x.id === ev.id) ? p : [...p, { id: ev.id!, term: ev.term! }]);
        } else if (ev.type === "chart" && ev.chart) {
          setCharts((c) => [...c, ev.chart!]);
        } else if (ev.type === "replace_text") {
          // Server-verified final text (a fabricated [[link]] / unsourced URL was removed) — swap it
          // in so a dead link never lingers on screen. Don't advance past the new length; repaint.
          bufRef.current = ev.text || "";
          shownRef.current = Math.min(shownRef.current, bufRef.current.length);
          paint(shownRef.current);
        } else if (ev.type === "error") {
          errored = true;
          // Show the error immediately — don't typewriter-drip it. Clear the buffer
          // so the reveal loop won't overwrite the ⚠️ message. Target this turn's
          // assistant bubble by id.
          bufRef.current = ""; shownRef.current = 0; streamActiveRef.current = false;
          setMessages((m) => m.map((x) => x.id === asstId ? { ...x, content: `⚠️ ${ev.message}` } : x));
        }
      }, coords, mode === "assisted" ? "assisted" : mode, freshCtx);
      // Read the finished reply aloud when the top-bar toggle is on (Assisted + Research).
      if (speakable && !errored && ttsOn.enabled) tts.speak(speechText(bufRef.current));
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
    } catch (err) {
      // A throw around conv-create / file-upload / the initial streamChat fetch — NOT an
      // in-stream 'error' EVENT (handled above; that sets `errored` and keeps the turn).
      // Roll back so nothing is lost: drop the optimistic bubble, restore the composer.
      errored = true;
      dropOptimisticBubble();
      rollbackComposer(text, files, err, "Couldn't send");
    } finally {
      streamActiveRef.current = false;
      activeAsstRef.current = null;
      setStreaming(false);
      setStatus("");
      setUploadPct(null);
      setStagingTick((t) => t + 1);   // chat modes (assisted+research) share staging
      sendingRef.current = false;
      // Re-sync from the server: the authoritative turn + any persisted approval
      // ('event') records, correctly ordered. Use the captured `cid` (convId state may
      // not have flushed yet if this send created the conversation). Skip on error to
      // keep the ⚠️ / the restored composer.
      if (!errored && cid) { await loadMessages(cid); setApplied([]); setCharts([]); }
    }
  }

  // The medical_reference gate: approve sends the EXACT term to MedlinePlus (the only outbound
  // moment) then auto-continues; skip declines and the assistant uses internal references only.
  async function approveProposal(p: { id: number; term: string }) {
    const term = p.term.trim();
    if (!term) return;
    setProposals((ps) => ps.filter((x) => x.id !== p.id));
    try { await approveExternalLookup(p.id, term); } catch { /* the continuation will report a miss */ }
    send(undefined, `I approved the external lookup for "${term}". Please continue.`);
  }
  async function skipProposal(p: { id: number; term: string }) {
    setProposals((ps) => ps.filter((x) => x.id !== p.id));
    try { await denyExternalLookup(p.id); } catch { /* ignore */ }
    send(undefined, `Don't look up "${p.term}" externally — use my own references and notes instead.`);
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
              : mode === "medical"
              ? "Log medical info — it’s saved under notes/medical/<destination>. Pick or add a destination below."
              : mode === "research"
              ? "Ask anything about your notes — I only read; I won’t change anything."
              : "Tell me what you want to capture. I’ll ask questions, then propose a note to confirm."}
          </div>
        )}
        {messages.map((m, i) => {
          if (m.role === "event") {
            let ev: { summary?: string; undo_id?: number; chart?: any };
            try { ev = JSON.parse(m.content); } catch { ev = { summary: m.content }; }
            if (ev.chart) return <LabChartCard key={i} spec={ev.chart} />;
            return (
              <div key={i} className="applied-chip">
                <span>✓ {renderSummary(ev.summary || "")}</span>
                {ev.undo_id == null ? null : undone.has(ev.undo_id)
                  ? <span className="muted" style={{ fontSize: 12 }}>undone</span>
                  : <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => undo(ev.undo_id!)}>Undo</button>}
              </div>
            );
          }
          // Empty assistant placeholder (waiting on the first token) → render
          // nothing; the status bar at the bottom shows "Thinking…" instead.
          if (m.role === "assistant" && !m.content) return null;
          const isAsst = m.role === "assistant";
          const hasHistory = isAsst && m.dbId != null && (m.stepCount ?? 0) > 0;
          return (
            <div key={i} className={`msg ${m.role}`}
                 onTouchStart={isAsst ? bubbleTouchStart : undefined}
                 onTouchEnd={isAsst ? (e) => bubbleTouchEnd(e, m.dbId) : undefined}>
              {isAsst ? (
                <>
                  <div className="md msg-md">
                    <ReactMarkdown components={{ a: makeChatLinkRenderer(navigate) }}>{renderWikiLinks(linkifyAddresses(m.content))}</ReactMarkdown>
                  </div>
                  {hasHistory && (
                    <ToolHistory messageId={m.dbId!} count={m.stepCount!} open={openSteps.has(m.dbId!)}
                                 onToggle={() => toggleSteps(m.dbId!)} navigate={navigate} />
                  )}
                </>
              ) : (
                m.content
              )}
            </div>
          );
        })}
        {/* Entry saves (this session): a user bubble + a link to the saved note. */}
        {entries.map((en) => {
          const label = en.title.startsWith("notes/daily/")
            ? ((en.text.split("\n").find((l) => l.trim()) || "entry").trim().slice(0, 40) || "entry")
            : en.title.replace(/^notes\//, "");
          return (
            <div key={`en${en.id}`} style={{ display: "contents" }}>
              {en.text && <div className="msg user">{en.text}</div>}
              {en.pending
                ? <span className="saved-chip" style={{ opacity: 0.55 }}><Icon name="check" size={14} /> Saving…</span>
                : <Link to={`/note/${en.slug}`} className="saved-chip"><Icon name="check" size={14} /> Saved: {label}</Link>}
            </div>
          );
        })}
        {charts.map((c, i) => <LabChartCard key={`ch${i}`} spec={c} />)}
        {proposals.map((p) => (
          <div key={`xp${p.id}`} className="ext-proposal">
            <div className="ext-proposal-text">
              🔍 Search MedlinePlus (external)? <strong>Edit the term</strong> if you like — nothing is sent until you approve.
            </div>
            <input className="ext-proposal-input" value={p.term} aria-label="External search term"
                   onChange={(e) => setProposals((ps) => ps.map((x) => x.id === p.id ? { ...x, term: e.target.value } : x))}
                   onKeyDown={(e) => { if (e.key === "Enter") approveProposal(p); }} />
            <div className="row" style={{ gap: 8, marginTop: 6 }}>
              <button className="primary" disabled={streaming || busy || !p.term.trim()} onClick={() => approveProposal(p)}>Approve &amp; search</button>
              <button className="ghost" disabled={streaming || busy} onClick={() => skipProposal(p)}>Skip</button>
            </div>
          </div>
        ))}
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
        {pendingFiles.map((f, i) => (
          <div key={`${f.name}-${i}`} className="attach-chip">
            <Icon name="clip" size={14} /> {f.name}
            <button className="icon-btn" style={{ padding: 2 }}
                    onClick={() => setPendingFiles((xs) => xs.filter((_, j) => j !== i))}>✕</button>
          </div>
        ))}
        {uploadPct !== null && (
          <div className="attach-chip">
            <Icon name="clip" size={14} /> {uploadLabel ? `${uploadLabel} — ` : ""}
            {uploadPct >= 100 ? "Processing attachment…" : `Uploading… ${uploadPct}%`}
          </div>
        )}
        {mode === "medical" && (
          <div className="med-dest-row">
            <Icon name="medical" size={14} />
            <span className="muted">notes/medical/</span>
            <select className="med-dest-select" value={curDest} onChange={(e) => pickDest(e.target.value)}>
              {dests.length === 0 && <option value="">(add a destination)</option>}
              {dests.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
            <button type="button" className="ghost" style={{ fontSize: 12, padding: "2px 8px" }} onClick={addDest}>＋ New</button>
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
            <button className={`mode-chip m-${mode}`} onClick={() => setMenuOpen((o) => !o)}>
              <Icon name={cur.icon} size={18} /> {cur.label}
            </button>
            {menuOpen && (
              <div className="mode-menu">
                {MODES.map((m) => (
                  <button key={m.key} className={`mode-row m-${m.key}${m.key === mode ? " sel" : ""}`}
                          onClick={() => pick(m.key)}>
                    <span className="mode-tile"><Icon name={m.icon} size={18} /></span>
                    <span className="mode-label">{m.label}</span>
                    <span className="mode-hint">{m.hint}</span>
                  </button>
                ))}
              </div>
            )}
          </span>
          <span className="spacer" />
          {mode !== "research" && mode !== "analyze" && (
            <>
              <input ref={fileRef} type="file" multiple style={{ display: "none" }}
                     onChange={(e) => {
                       const picked = Array.from(e.target.files || []);
                       const tooBig = picked.filter((f) => f.size > MAX_ATTACHMENT_BYTES);
                       const ok = picked.filter((f) => f.size <= MAX_ATTACHMENT_BYTES);
                       if (tooBig.length) alert(`Over 100 MB (skipped): ${tooBig.map((f) => f.name).join(", ")}`);
                       if (ok.length) setPendingFiles((xs) => [...xs, ...ok]);
                       e.currentTarget.value = "";
                     }} />
              <button className="icon-btn" title="Attach file" onClick={() => fileRef.current?.click()}><Icon name="clip" /></button>
            </>
          )}
          <button className="icon-btn send" title="Send" onClick={() => send()}
                  disabled={streaming || busy || !online || (!input.trim() && pendingFiles.length === 0)}><Icon name="send" /></button>
        </div>
      </div>
    </div>
  );
}
