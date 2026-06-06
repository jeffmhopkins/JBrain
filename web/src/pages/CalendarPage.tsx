import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { calUpcoming, calHistory, calQuickAdd, calReschedule, calCancel, CalEvent } from "../api";

// Calendar tab — an agenda over the calendar projection. Everything here ultimately
// writes NOTES (the source of truth): quick-add writes a dated note, reschedule/cancel
// write superseding notes. The list is the re-derived sidecar (v_upcoming/v_event_history).

function fmtWhen(e: CalEvent): string {
  const s = e.starts_at || "";
  if (!s) return "(undated)";
  if (e.all_day || !s.includes("T")) return s.slice(0, 10);
  return s.slice(0, 16).replace("T", " ");
}

const KINDS = ["event", "appointment", "deadline", "reminder"];

export default function CalendarPage() {
  const [tab, setTab] = useState<"upcoming" | "history">("upcoming");
  const [items, setItems] = useState<CalEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  // Quick-add form state.
  const [title, setTitle] = useState("");
  const [date, setDate] = useState("");
  const [time, setTime] = useState("");
  const [kind, setKind] = useState("event");

  // Refetch the current tab (used by the action handlers + Refresh).
  async function load() {
    setLoading(true); setErr("");
    try {
      setItems(tab === "upcoming" ? await calUpcoming(365) : await calHistory());
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally { setLoading(false); }
  }

  // Tab-driven load with a stale-response guard so rapid toggles can't render the
  // wrong tab's data.
  useEffect(() => {
    let ignore = false;
    setLoading(true); setErr("");
    (tab === "upcoming" ? calUpcoming(365) : calHistory())
      .then((d) => { if (!ignore) setItems(d); })
      .catch((e: any) => { if (!ignore) setErr(String(e?.message || e)); })
      .finally(() => { if (!ignore) setLoading(false); });
    return () => { ignore = true; };
  }, [tab]);

  async function add(ev: React.FormEvent) {
    ev.preventDefault();
    if (!title.trim() || !date) return;
    setBusy(true); setErr("");
    try {
      await calQuickAdd({ title: title.trim(), date, time: time || undefined, kind });
      setTitle(""); setTime("");
      setTab("upcoming");
      await load();
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally { setBusy(false); }
  }

  async function reschedule(e: CalEvent) {
    const to = window.prompt(`Reschedule "${e.title}" to which date? (YYYY-MM-DD)`, (e.starts_at || "").slice(0, 10));
    if (!to) return;
    // Preserve the time for a timed event (don't silently downgrade it to all-day).
    let toTime: string | undefined;
    if (!e.all_day && (e.starts_at || "").includes("T")) {
      const t = window.prompt("Time? (HH:MM, 24h — leave blank for all-day)", (e.starts_at || "").slice(11, 16));
      if (t === null) return;             // cancelled the prompt
      toTime = t.trim() || undefined;
    }
    setBusy(true); setErr("");
    try { await calReschedule(e.id, to.trim(), toTime); await load(); }
    catch (er: any) { setErr(String(er?.message || er)); }
    finally { setBusy(false); }
  }

  async function cancel(e: CalEvent) {
    if (!window.confirm(`Cancel "${e.title}"? This writes a cancellation note.`)) return;
    setBusy(true); setErr("");
    try { await calCancel(e.id); await load(); }
    catch (er: any) { setErr(String(er?.message || er)); }
    finally { setBusy(false); }
  }

  return (
    <div className="content">
      <h2>Calendar</h2>
      <p className="muted" style={{ fontSize: 13 }}>
        Appointments, deadlines and recurring patterns derived from your notes. Adding or
        editing here writes a note — the calendar is re-derived, so your notes stay the
        source of truth.
      </p>

      <form className="card" onSubmit={add}>
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <input placeholder="What…" value={title} onChange={(e) => setTitle(e.target.value)}
                 style={{ flex: "2 1 160px" }} />
          <input type="date" value={date} onChange={(e) => setDate(e.target.value)} style={{ flex: "1 1 130px" }} />
          <input type="time" value={time} onChange={(e) => setTime(e.target.value)} style={{ flex: "0 1 110px" }} />
          <select value={kind} onChange={(e) => setKind(e.target.value)} style={{ flex: "0 1 130px" }}>
            {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
          <button className="primary" disabled={busy || !title.trim() || !date}>Add</button>
        </div>
      </form>

      <div className="row" style={{ gap: 8, margin: "12px 0" }}>
        <button className={tab === "upcoming" ? "primary" : "ghost"} onClick={() => setTab("upcoming")}>Upcoming</button>
        <button className={tab === "history" ? "primary" : "ghost"} onClick={() => setTab("history")}>History</button>
        <span className="spacer" />
        <button className="ghost" onClick={load} disabled={busy}>Refresh</button>
      </div>

      {err && <p style={{ color: "var(--danger, #c33)", fontSize: 13 }}>{err}</p>}
      {loading && <p className="muted">Loading…</p>}
      {!loading && items.length === 0 && !err &&
        <p className="muted">Nothing {tab === "upcoming" ? "coming up" : "in history"}.</p>}

      {items.map((e) => (
        <div className="card" key={`${e.id}-${e.starts_at}`}>
          <div className="row">
            <strong>{e.title}</strong>
            {e.recurring && <span className="muted" style={{ fontSize: 11, marginLeft: 6 }}>· recurring</span>}
            {e.kind && e.kind !== "event" && !e.recurring &&
              <span className="muted" style={{ fontSize: 11, marginLeft: 6 }}>· {e.kind}</span>}
            <span className="spacer" />
            <span className="muted" style={{ fontSize: 12 }}>{fmtWhen(e)}</span>
          </div>
          {(e.status && e.status !== "confirmed") &&
            <p className="muted" style={{ fontSize: 12, margin: "4px 0" }}>{e.status}</p>}
          <div className="row" style={{ gap: 8, marginTop: 8 }}>
            {e.note_slug &&
              <Link className="ghost" style={{ padding: "6px 12px", borderRadius: 8 }} to={`/note/${e.note_slug}`}>
                Open note
              </Link>}
            {tab === "upcoming" && !e.recurring && <>
              <button className="ghost" onClick={() => reschedule(e)} disabled={busy}>Reschedule</button>
              <button className="ghost" onClick={() => cancel(e)} disabled={busy}>Cancel</button>
            </>}
          </div>
        </div>
      ))}
    </div>
  );
}
