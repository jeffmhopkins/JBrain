import { FormEvent, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import { createEntry, extractLabs, get, getMedicalDests, post, setMedicalDests, streamChat, uploadAttachment } from "../api";
import { useGeo, useOnline } from "../hooks";
import StagingPanel from "../components/StagingPanel";
import LabChartCard from "../components/LabChartCard";
import { Icon } from "../components/Icon";
import { makeLinkRenderer, renderWikiLinks } from "../util";

// 'event' rows are persisted approval records (✓ applied X), kept in the chat
// but excluded from the LLM history server-side.
interface Msg { role: "user" | "assistant" | "event"; content: string; }
type Mode = "entry" | "medical" | "assisted" | "research";

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
  { key: "medical", label: "Medical", icon: "medical" },
  { key: "assisted", label: "Assisted", icon: "robot" },
  { key: "research", label: "Research", icon: "search" },
];
const PLACEHOLDER: Record<Mode, string> = {
  entry: "Write an entry…",
  medical: "Log a lab, note, procedure…",
  assisted: "Talk it out…",
  research: "Ask your brain… (read-only)",
};
// Friendly status shown at the bottom of the conversation while a tool runs. Keep in
// sync with the tool schemas in server/app/services/architect.py; an unlisted tool
// falls back to "Working…".
const TOOL_LABELS: Record<string, string> = {
  // Reading notes
  search_notes: "Searching your notes…",
  read_note: "Reading a note…",
  read_notes: "Reading notes…",
  related_notes: "Finding related notes…",
  list_recent_notes: "Looking at recent notes…",
  list_tags: "Listing tags…",
  notes_with_tag: "Finding tagged notes…",
  search_attachments: "Searching attachments…",
  read_attachment: "Reading an attachment…",
  query_sql: "Querying the database…",
  // Location & people
  current_location: "Checking your location…",
  locate_person: "Locating a person…",
  location_fixes: "Reading location history…",
  list_trips: "Listing trips…",
  trip_detail: "Reading a trip…",
  geo_distance: "Measuring distance…",
  nearby_notes: "Finding nearby notes…",
  where_was_i: "Looking up where you were…",
  time_at_place: "Calculating time at a place…",
  places_visited: "Finding places you visited…",
  distance_traveled: "Adding up distance traveled…",
  trail_summary: "Summarizing your trail…",
  entries_at_place: "Finding notes from a place…",
  reverse_geocode: "Looking up an address…",
  forward_geocode: "Looking up coordinates…",
  drug_reference: "Looking up a medication…",
  list_abnormal_labs: "Finding out-of-range labs…",
  show_lab_chart: "Charting lab results…",
  lab_stat: "Checking lab values…",
  lab_value_at: "Checking lab values…",
  // Lists & tags
  read_list: "Reading a list…",
  add_list_item: "Updating a list…",
  set_item_checked: "Updating a list…",
  set_item_priority: "Updating a list…",
  add_sublist: "Updating a list…",
  set_tags: "Tagging the note…",
  // Sharing
  create_share_link: "Creating a share link…",
  create_guided_share: "Setting up a guided share…",
  create_research_share: "Setting up a research share…",
  list_share_links: "Listing share links…",
  revoke_share_link: "Revoking a share link…",
  // Writes
  log_entry: "Logging an entry…",
  propose_actions: "Drafting proposed changes…",
  // Knowledge base
  kb_coverage_check: "Checking knowledge-base coverage…",
  kb_citation_cleanup: "Cleaning up citations…",
  kb_promote_recurrences: "Finding recurring patterns…",
  kb_audit: "Auditing the knowledge base…",
  kb_taxonomy_health: "Checking taxonomy health…",
  kb_needed_links: "Finding missing links…",
  kb_research_links: "Researching references…",
  kb_read_talk: "Reading article notes…",
  kb_add_directive: "Noting a directive…",
};
const toolLabel = (name?: string) => (name && TOOL_LABELS[name]) || "Working…";

// How long a send will wait for a location stamp before posting without one. A cached
// fix (getCoords' maximumAge: 60s) returns in single-digit ms; a cold fix that needs
// the GPS radio is dropped after this budget rather than freezing the UI for up to 10s.
// The stamp is best-effort metadata, not the point of the post.
const GEO_MAX_WAIT = 1500;

export default function Chat() {
  const online = useOnline();
  const geo = useGeo();
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
  // `pending` entries are the optimistic user bubble shown the instant Send is hit (entry/
  // medical have no streamed reply, so this is their only immediate feedback). The save
  // resolves the matching `id` in place (fills title/slug → "Saved:" chip) or drops it on
  // error. Keyed by a unique `id` so reconciliation never touches the wrong row.
  const [entries, setEntries] = useState<{ id: number; text: string; title: string; slug: string; pending?: boolean }[]>([]);
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
  // Charts streamed during the current turn (transient; persisted as event rows on reload).
  const [charts, setCharts] = useState<{ analyte: string; unit?: string | null; from?: string | null; to?: string | null; title?: string }[]>([]);
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
      // A turn may have started streaming while this fetch was in flight (e.g. the user
      // sent before the initial restore-load resolved). Don't clobber the optimistic /
      // actively-streaming view with stale server rows — the post-stream re-sync handles it.
      if (streamActiveRef.current) return;
      setMessages(rows.map((r) => ({ role: r.role, content: r.content })));
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

  async function send(e?: FormEvent) {
    e?.preventDefault();
    const text = input.trim();
    // streaming/busy gate the UI but are async React state; sendingRef is the synchronous
    // latch that blocks a second tap during this send's async pre-flight (GPS, conv-create,
    // upload) before that state has flushed.
    if ((!text && !pendingFile) || streaming || busy || sendingRef.current || !online) return;
    if (text === "/clear") { setInput(""); setEntries([]); newConversation(); return; }
    if (mode === "medical" && !curDest) { alert("Pick or add a medical destination first."); return; }
    sendingRef.current = true;

    // --- Optimistic UI FIRST (synchronous), async work second. Everything the user should
    // see the instant they hit Send happens here, BEFORE we await anything: the composer
    // clears and the user's message/bubble appears immediately, regardless of how slow the
    // GPS fix or the network turns out to be.
    const file = pendingFile;
    const bubble = text || (file ? `📎 ${file.name}` : "");
    setInput(""); setPendingFile(null);
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
        const r = await createEntry(text || (file ? file.name : "Untitled"), undefined, coords, dest);
        let labMsg = "";
        if (file) {
          await uploadAttachment(r.slug, file, setUploadPct);
          // Medical mode: if the upload was a lab PDF, STAGE its values for review on the note.
          if (mode === "medical" && /\.pdf$/i.test(file.name)) {
            try {
              const lab = await extractLabs(r.slug);
              if (lab.staged) labMsg = ` · ${lab.staged} lab results extracted — open the note to review & approve`;
            } catch { /* non-fatal: the note + attachment are saved regardless */ }
          }
        }
        // Resolve the optimistic row in place (by id) → renders the "Saved:" chip.
        setEntries((xs) => xs.map((en) => en.id === enId
          ? { id: enId, text: bubble + labMsg, title: r.title, slug: r.slug }
          : en));
      } catch (err) {
        // Don't silently lose the entry: drop the optimistic row, restore the composer
        // (only if the user hasn't typed something new), and tell them.
        setEntries((xs) => xs.filter((en) => en.id !== enId));
        setInput((cur) => cur.trim() ? cur : text);
        if (file) setPendingFile((cur) => cur || file);
        alert("Couldn't save entry: " + (err instanceof Error ? err.message : "please try again."));
      } finally { setBusy(false); setUploadPct(null); sendingRef.current = false; }
      return;
    }

    // --- Chat modes (assisted/research). Show the user bubble + spinner immediately —
    // even before the conversation id resolves — then queue the actual send behind
    // ensureConversation() so a turn fired before convId is set is never dropped.
    atBottomRef.current = true;   // sending re-engages follow, so you see your message + reply
    setMessages((m) => [...m, { role: "user", content: bubble }, { role: "assistant", content: "" }]);
    let bubbleShown = true;
    // Reset the typewriter cleanly for this new turn.
    bufRef.current = ""; shownRef.current = 0; streamActiveRef.current = true;
    setStreaming(true);
    setStatus("Thinking…");
    let errored = false;
    let cid: number | null = null;
    // Remove the optimistic [user, empty-assistant] pair (only if still present) so a throw
    // before any token is delivered doesn't leave a dangling half-turn on screen.
    const dropOptimisticBubble = () => {
      if (!bubbleShown) return;
      bubbleShown = false;
      setMessages((m) => (m.length >= 2 ? m.slice(0, -2) : m));
    };
    try {
      cid = await ensureConversation();
      let extra = "";
      if (mode === "assisted" && file) {
        // Save the file to a note so there's something to attach it to. Skip
        // auto-analysis: this carrier note has no real content to inform it.
        const coords = await coordsP;
        const r = await createEntry(`Attached file: ${file.name}`, file.name.replace(/\.[^.]+$/, ""), coords);
        await uploadAttachment(r.slug, file, setUploadPct, false);
        setUploadPct(null);
        extra = `\n\n(I attached a file, saved as [[${r.title}]].)`;
        // Fold the saved-note reference into the already-shown user bubble (it sits just
        // before the empty assistant placeholder).
        setMessages((m) => {
          const c = [...m];
          const ui = c.length - 2;
          if (ui >= 0 && c[ui].role === "user") c[ui] = { role: "user", content: (text + extra).trim() };
          return c;
        });
      }
      const msg = (text + extra).trim();
      const coords = await coordsP;   // resolved (or null) by now; bounded by GEO_MAX_WAIT
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
        } else if (ev.type === "chart" && ev.chart) {
          setCharts((c) => [...c, ev.chart!]);
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
    } catch (err) {
      // A throw around conv-create / file-upload / the initial streamChat fetch — NOT an
      // in-stream 'error' EVENT (handled above; that sets `errored` and keeps the turn).
      // Roll back so nothing is lost: drop the optimistic bubble, restore the composer.
      errored = true;
      dropOptimisticBubble();
      setInput((cur) => cur.trim() ? cur : text);
      if (file) setPendingFile((cur) => cur || file);
      alert("Couldn't send: " + (err instanceof Error ? err.message : "please try again."));
    } finally {
      streamActiveRef.current = false;
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
