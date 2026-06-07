import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  calUpcoming, calHistory, calRange, calQuickAdd, calReschedule, calCancel, CalEvent,
  Reminder, AddedEvent, calGetReminders, calSetReminders,
  calRecentlyAdded, calMarkReviewed, calDismiss, calUndismiss,
} from "../api";
import { useAuth } from "../App";
import Modal from "../components/Modal";

// Reminder presets. Timed events use minute offsets from start; all-day events use a
// 9am day-of anchor (offset 0 = morning of; 900 = 6pm evening before; 2880 = 2 days before).
const TIMED_PRESETS: { label: string; off: number }[] = [
  { label: "At time", off: 0 }, { label: "10m", off: 10 }, { label: "30m", off: 30 },
  { label: "1h", off: 60 }, { label: "2h", off: 120 }, { label: "1d", off: 1440 }, { label: "2d", off: 2880 },
];
const ALLDAY_PRESETS: { label: string; off: number }[] = [
  { label: "Morning of", off: 0 }, { label: "Evening before", off: 900 }, { label: "2 days before", off: 2880 },
];
const remKey = (r: Reminder) => `${r.anchor || "start"}:${r.offset_minutes}`;

function RemindChips({ allDay, value, onChange }: {
  allDay: boolean; value: Reminder[]; onChange: (v: Reminder[]) => void;
}) {
  const anchor = allDay ? "day_of" : "start";
  const presets = allDay ? ALLDAY_PRESETS : TIMED_PRESETS;
  const on = new Set(value.filter((r) => (r.anchor || "start") === anchor).map((r) => r.offset_minutes));
  function toggle(off: number) {
    const has = on.has(off);
    onChange(has ? value.filter((r) => !((r.anchor || "start") === anchor && r.offset_minutes === off))
                 : [...value, { offset_minutes: off, anchor }]);
  }
  return (
    <div className="cal-field">
      <label>REMIND ME</label>
      <div className="rem-chips">
        {presets.map((p) => (
          <button key={p.label} type="button" className={"rem-chip" + (on.has(p.off) ? " on" : "")}
            aria-pressed={on.has(p.off)} onClick={() => toggle(p.off)}>{p.label}</button>
        ))}
        {value.length > 0 && (
          <button type="button" className="rem-chip none" aria-label="Clear reminders"
            onClick={() => onChange([])}>None</button>
        )}
      </div>
    </div>
  );
}

type View = "list" | "day" | "week" | "month";
const LS_VIEW = "jbrain_cal_view";
const KINDS = ["event", "appointment", "deadline", "reminder"];
const WD = ["S", "M", "T", "W", "T", "F", "S"];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const ROWH = 40;

// --- date helpers (wall-clock; event strings are owner-local, treated verbatim) ---
const pad = (n: number) => String(n).padStart(2, "0");
const ymd = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
function parseYmd(s: string): Date { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); }
function addDays(d: Date, n: number): Date { const x = new Date(d); x.setDate(x.getDate() + n); return x; }
function addMonths(d: Date, n: number): Date { return new Date(d.getFullYear(), d.getMonth() + n, 1); }
const startOfWeek = (d: Date) => addDays(d, -d.getDay());                 // Sunday
const startOfMonth = (d: Date) => new Date(d.getFullYear(), d.getMonth(), 1);

const browserTz = () => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone; } catch { return "UTC"; } };
function todayIso(tz: string): string {
  try { return new Intl.DateTimeFormat("en-CA", { timeZone: tz }).format(new Date()); }
  catch { return ymd(new Date()); }
}
function nowMinutes(tz: string): number {
  try {
    const s = new Intl.DateTimeFormat("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date());
    const [h, m] = s.split(":").map(Number); return h * 60 + m;
  } catch { const d = new Date(); return d.getHours() * 60 + d.getMinutes(); }
}

// --- event helpers ---
const dayKey = (e: CalEvent) => (e.starts_at || "").slice(0, 10);
const isTimed = (e: CalEvent) => !e.all_day && !!e.starts_at && e.starts_at.includes("T");
function timeLabel(e: CalEvent): string {
  if (!e.starts_at) return "—";
  return isTimed(e) ? e.starts_at.slice(11, 16) : "all-day";
}
function minutesOf(s: string | null | undefined): number {
  if (!s || !s.includes("T")) return 0;
  const [h, m] = s.slice(11, 16).split(":").map(Number); return h * 60 + (m || 0);
}
function kindTag(e: CalEvent): string | null {
  if (e.recurring) return null;
  return e.kind && e.kind !== "event" ? e.kind : null;
}
const isCancelled = (e: CalEvent) => e.status === "cancelled";
function sortEvents(a: CalEvent, b: CalEvent): number {
  const at = isTimed(a), bt = isTimed(b);
  if (at !== bt) return at ? 1 : -1;
  if (at && bt) return minutesOf(a.starts_at) - minutesOf(b.starts_at);
  return a.title.localeCompare(b.title);
}
function bucket(events: CalEvent[]): Map<string, CalEvent[]> {
  const m = new Map<string, CalEvent[]>();
  for (const e of events) {
    const k = dayKey(e); if (!k) continue;
    if (!m.has(k)) m.set(k, []);
    m.get(k)!.push(e);
  }
  for (const v of m.values()) v.sort(sortEvents);
  return m;
}
const evKey = (e: CalEvent) => `${e.id}-${e.starts_at}`;

export default function CalendarPage() {
  const { appTz } = useAuth();
  const tz = appTz || browserTz();
  const [view, setView] = useState<View>(() => (localStorage.getItem(LS_VIEW) as View) || "list");
  const [cursor, setCursor] = useState<Date>(() => parseYmd(todayIso(appTz || browserTz())));
  const [selDay, setSelDay] = useState<string>(() => todayIso(appTz || browserTz()));
  const [listTab, setListTab] = useState<"upcoming" | "history">("upcoming");
  const [events, setEvents] = useState<CalEvent[]>([]);
  const [loading, setLoading] = useState(true);            // start true so the empty-state never flashes
  const [err, setErr] = useState("");
  const [sheet, setSheet] = useState<{ ev?: CalEvent; addDate?: string; addTime?: string } | null>(null);
  const [, setTick] = useState(0);                          // 60s tick to keep the now-line live
  const [reloadNonce, setReloadNonce] = useState(0);        // explicit refetch signal for the review banner
  const cache = useRef<Map<string, CalEvent[]>>(new Map());
  const today = todayIso(tz);

  useEffect(() => { const id = setInterval(() => setTick((t) => t + 1), 60000); return () => clearInterval(id); }, []);

  function rangeFor(v: View, c: Date): [string, string] {
    if (v === "day") return [ymd(c), ymd(c)];
    if (v === "week") { const s = startOfWeek(c); return [ymd(s), ymd(addDays(s, 6))]; }
    const s = startOfWeek(startOfMonth(c)); return [ymd(s), ymd(addDays(s, 41))];
  }
  function reload() { cache.current.clear(); setReloadNonce((n) => n + 1); load(); }
  function load(): () => void {
    setLoading(true); setErr("");
    let ignore = false;
    const ok = (d: CalEvent[]) => { if (!ignore) { setEvents(d); setLoading(false); } };
    const fail = (e: any) => { if (!ignore) { setErr(String(e?.message || e)); setLoading(false); } };
    if (view === "list") {
      (listTab === "upcoming" ? calUpcoming(365) : calHistory()).then(ok).catch(fail);
    } else {
      const [from, to] = rangeFor(view, cursor);
      const key = `${from}..${to}`;
      const hit = cache.current.get(key);
      if (hit) { setEvents(hit); setLoading(false); }
      else calRange(from, to).then((d) => { if (ignore) return; cache.current.set(key, d); ok(d); }).catch(fail);
    }
    return () => { ignore = true; };
  }
  useEffect(() => load(), [view, listTab, view === "list" ? 0 : +cursor]);  // eslint-disable-line react-hooks/exhaustive-deps

  function switchView(v: View) { setView(v); localStorage.setItem(LS_VIEW, v); }
  function step(dir: number) {
    setCursor((c) => view === "month" ? addMonths(c, dir) : addDays(c, dir * (view === "week" ? 7 : 1)));
  }
  function goToday() { const t = parseYmd(today); setCursor(t); setSelDay(today); }

  const byDay = useMemo(() => bucket(events), [events]);
  const periodLabel = useMemo(() => {
    if (view === "month") return `${MON[cursor.getMonth()]} ${cursor.getFullYear()}`;
    if (view === "week") { const s = startOfWeek(cursor), e = addDays(s, 6);
      const yr = s.getFullYear() !== e.getFullYear() ? ` ${e.getFullYear()}` : "";
      return `${MON[s.getMonth()]} ${s.getDate()} – ${s.getMonth() === e.getMonth() ? "" : MON[e.getMonth()] + " "}${e.getDate()}${yr}`; }
    if (view === "day") return `${DOW[cursor.getDay()]} ${MON[cursor.getMonth()]} ${cursor.getDate()}`;
    return "";
  }, [view, cursor]);

  async function doQuickAdd(b: { title: string; date: string; time?: string; kind: string; reminders?: Reminder[] }) { await calQuickAdd(b); setSheet(null); reload(); }
  async function doReschedule(ev: CalEvent, to_date: string, to_time?: string) { await calReschedule(ev.id, to_date, to_time); setSheet(null); reload(); }
  async function doCancel(ev: CalEvent) { await calCancel(ev.id); setSheet(null); reload(); }

  return (
    <div className="tool-body cal">
      <div className="cal-bar">
        <div className="seg" role="group" aria-label="Calendar view">
          {(["Day", "Week", "Month", "List"] as const).map((s) => {
            const v = s.toLowerCase() as View;
            return <button key={v} className={view === v ? "on" : ""} aria-pressed={view === v}
              onClick={() => switchView(v)}>{s}</button>;
          })}
        </div>
        <div className="cal-nav">
          {view !== "list"
            ? <>
                <button className="icon-btn" aria-label="Previous period" onClick={() => step(-1)}>‹</button>
                <span className="cal-period">{periodLabel}</span>
                <button className="icon-btn" aria-label="Next period" onClick={() => step(1)}>›</button>
                <span className="spacer" />
                <button className="cal-today" onClick={goToday}>Today</button>
              </>
            : <div className="seg cal-sub" role="group" aria-label="List scope">
                <button className={listTab === "upcoming" ? "on" : ""} aria-pressed={listTab === "upcoming"}
                  onClick={() => setListTab("upcoming")}>Upcoming</button>
                <button className={listTab === "history" ? "on" : ""} aria-pressed={listTab === "history"}
                  onClick={() => setListTab("history")}>Past</button>
              </div>}
        </div>
      </div>

      <ReviewBanner refreshKey={reloadNonce} />
      {err && <p style={{ color: "var(--danger)", fontSize: 13 }}>{err}</p>}
      {loading && <p className="muted">Loading…</p>}

      {!loading && view === "list" && <ListView byDay={byDay} today={today} onPick={(ev) => setSheet({ ev })} />}
      {!loading && view === "month" && (
        <MonthView cursor={cursor} byDay={byDay} today={today} sel={selDay}
          onDay={(k) => setSelDay(k)} onAdd={(k) => setSheet({ addDate: k })}
          onPick={(ev) => setSheet({ ev })} onOpenDay={(k) => { setCursor(parseYmd(k)); switchView("day"); }} />
      )}
      {!loading && view === "week" && (
        <WeekView cursor={cursor} byDay={byDay} today={today}
          onAdd={(k) => setSheet({ addDate: k })} onPick={(ev) => setSheet({ ev })} />
      )}
      {!loading && view === "day" && (
        <DayView cursor={cursor} byDay={byDay} today={today} nowMin={nowMinutes(tz)}
          onAdd={(k, t) => setSheet({ addDate: k, addTime: t })} onPick={(ev) => setSheet({ ev })} />
      )}

      {sheet && (sheet.ev
        ? <EventSheet ev={sheet.ev} onClose={() => setSheet(null)} onReschedule={doReschedule} onCancel={doCancel} />
        : <QuickAddSheet date={sheet.addDate!} time={sheet.addTime} onClose={() => setSheet(null)} onAdd={doQuickAdd} />)}
    </div>
  );
}

// ---------- shared event row (a real <button> for keyboard + Space) ----------
function Row({ e, onPick, chevron = true }: { e: CalEvent; onPick: (e: CalEvent) => void; chevron?: boolean }) {
  const tag = kindTag(e);
  return (
    <button className={"cal-row" + (isCancelled(e) ? " cancelled" : "")} onClick={() => onPick(e)}>
      <span className="when">{timeLabel(e)}</span>
      <span className={"ttl" + (e.kind === "deadline" ? " deadline" : "")}>{e.title}</span>
      <span className="meta">
        {e.recurring && <span aria-label="recurring" title="recurring">↻</span>}
        {tag && <span>{tag}</span>}
        {chevron && <span aria-hidden="true" style={{ color: "var(--text-dim)" }}>›</span>}
      </span>
    </button>
  );
}

function groupHeader(k: string, today: string): string {
  if (k === today) return "TODAY · " + fmtDate(k);
  if (ymd(addDays(parseYmd(today), 1)) === k) return "TOMORROW · " + fmtDate(k);
  return fmtDate(k).toUpperCase();
}
function fmtDate(k: string): string { const d = parseYmd(k); return `${DOW[d.getDay()]}, ${MON[d.getMonth()]} ${d.getDate()}`; }

// ---------- List ----------
function ListView({ byDay, today, onPick }: { byDay: Map<string, CalEvent[]>; today: string; onPick: (e: CalEvent) => void }) {
  const days = [...byDay.keys()].sort();
  if (days.length === 0) return <p className="muted">Nothing scheduled.</p>;
  return <>{days.map((k) => (
    <div key={k}>
      <div className="cal-grp">{groupHeader(k, today)}</div>
      <div className="cal-card">{byDay.get(k)!.map((e) => <Row key={evKey(e)} e={e} onPick={onPick} />)}</div>
    </div>
  ))}</>;
}

// ---------- Month ----------
function MonthView({ cursor, byDay, today, sel, onDay, onAdd, onPick, onOpenDay }: {
  cursor: Date; byDay: Map<string, CalEvent[]>; today: string; sel: string;
  onDay: (k: string) => void; onAdd: (k: string) => void; onPick: (e: CalEvent) => void; onOpenDay: (k: string) => void;
}) {
  const first = startOfWeek(startOfMonth(cursor));
  const cells = Array.from({ length: 42 }, (_, i) => addDays(first, i));
  const mo = cursor.getMonth();
  const selEvents = byDay.get(sel) || [];
  return <>
    <div className="cal-wd" aria-hidden="true">{WD.map((d, i) => <span key={i}>{d}</span>)}</div>
    <div className="cal-month">
      {cells.map((d) => {
        const k = ymd(d); const evs = byDay.get(k) || [];
        const isToday = k === today, isSel = k === sel;
        const cls = "cal-cell" + (d.getMonth() !== mo ? " dim" : "") + (isSel ? " sel" : "") + (isToday ? " today" : "");
        const lbl = `${fmtDate(k)}, ${evs.length} event${evs.length === 1 ? "" : "s"}` + (isToday ? ", today" : "") + (isSel ? ", selected" : "");
        const act = () => (evs.length ? onDay(k) : onAdd(k));
        return (
          <div key={k} className={cls} role="button" tabIndex={0} aria-label={lbl} aria-pressed={isSel}
            onClick={act} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); act(); } }}>
            <span className="num">{d.getDate()}</span>
            <span className="cal-dots" aria-hidden="true">
              {evs.slice(0, 3).map((_, i) => <i key={i} />)}
              {evs.length > 3 && <span className="more">+{evs.length - 3}</span>}
            </span>
          </div>
        );
      })}
    </div>
    <div className="cal-grp">{fmtDate(sel).toUpperCase()}
      <button className="open-day" onClick={() => onAdd(sel)}>+ Add</button>
      <button className="open-day" onClick={() => onOpenDay(sel)}>Open day →</button></div>
    {selEvents.length === 0
      ? <p className="cal-empty"><button className="linkish" onClick={() => onAdd(sel)}>Nothing scheduled — tap to add.</button></p>
      : <div className="cal-card">{selEvents.map((e) => <Row key={evKey(e)} e={e} onPick={onPick} chevron={false} />)}</div>}
  </>;
}

// ---------- Week (stacked day-sections) ----------
function WeekView({ cursor, byDay, today, onAdd, onPick }: {
  cursor: Date; byDay: Map<string, CalEvent[]>; today: string; onAdd: (k: string) => void; onPick: (e: CalEvent) => void;
}) {
  const start = startOfWeek(cursor);
  const days = Array.from({ length: 7 }, (_, i) => addDays(start, i));
  return <>{days.map((d) => {
    const k = ymd(d); const evs = byDay.get(k) || [];
    return (
      <div key={k}>
        <div className={"cal-dhdr" + (k === today ? " today" : "")}>
          <span>{DOW[d.getDay()].toUpperCase()} {MON[d.getMonth()]} {d.getDate()}{k === today ? " · today" : ""}</span>
          <span className="rule" aria-hidden="true" />
          <button className="open-day" onClick={() => onAdd(k)} aria-label={`Add event on ${fmtDate(k)}`}>+</button>
        </div>
        {evs.length === 0
          ? <button className="cal-empty linkish" onClick={() => onAdd(k)}>—</button>
          : <div className="cal-card">{evs.map((e) => <Row key={evKey(e)} e={e} onPick={onPick} />)}</div>}
      </div>
    );
  })}</>;
}

// ---------- Day (time-axis grid) ----------
interface Laid { e: CalEvent; top: number; height: number; col: number; cols: number; }
function layoutDay(timed: CalEvent[], loMin: number): Laid[] {
  const items = timed.map((e) => {
    const s = minutesOf(e.starts_at);
    const en = e.ends_at && e.ends_at.includes("T") ? minutesOf(e.ends_at) : s + 60;
    return { e, s, en: Math.max(en, s + 20) };
  }).sort((a, b) => a.s - b.s || b.en - a.en);
  const out: Laid[] = [];
  let i = 0;
  while (i < items.length) {
    let j = i, clusterEnd = items[i].en;
    while (j + 1 < items.length && items[j + 1].s < clusterEnd) { j++; clusterEnd = Math.max(clusterEnd, items[j].en); }
    const cluster = items.slice(i, j + 1);
    const colEnds: number[] = [], cols: number[] = [];
    for (const it of cluster) {
      let c = colEnds.findIndex((end) => end <= it.s);
      if (c === -1) { c = colEnds.length; colEnds.push(it.en); } else colEnds[c] = it.en;
      cols.push(c);
    }
    const ncols = colEnds.length;
    cluster.forEach((it, idx) => out.push({
      e: it.e, top: Math.max(0, (it.s - loMin) / 60 * ROWH),
      height: Math.max((it.en - it.s) / 60 * ROWH - 2, 16), col: cols[idx], cols: ncols,
    }));
    i = j + 1;
  }
  return out;
}
function DayView({ cursor, byDay, today, nowMin, onAdd, onPick }: {
  cursor: Date; byDay: Map<string, CalEvent[]>; today: string; nowMin: number;
  onAdd: (k: string, t?: string) => void; onPick: (e: CalEvent) => void;
}) {
  const k = ymd(cursor);
  const all = byDay.get(k) || [];
  const allDay = all.filter((e) => !isTimed(e));
  const timed = all.filter(isTimed);
  let lo = 7, hi = 19;
  for (const e of timed) {
    lo = Math.min(lo, Math.floor(minutesOf(e.starts_at) / 60));
    const en = e.ends_at && e.ends_at.includes("T") ? minutesOf(e.ends_at) : minutesOf(e.starts_at) + 60;
    hi = Math.max(hi, Math.ceil(en / 60));
  }
  const hours = Array.from({ length: hi - lo + 1 }, (_, i) => lo + i);
  const laid = layoutDay(timed, lo * 60);
  const showNow = k === today && nowMin >= lo * 60 && nowMin <= hi * 60;
  const hlabel = (h: number) => h === 0 ? "12 AM" : h < 12 ? `${h} AM` : h === 12 ? "12 PM" : `${h - 12} PM`;
  return <>
    <div className="row" style={{ justifyContent: "flex-end", marginBottom: 6 }}>
      <button className="cal-today" onClick={() => onAdd(k)}>+ Add event</button>
    </div>
    <div className="cal-allday">
      <span className="lbl">ALL DAY</span>
      <div className="lane">
        {allDay.length === 0 ? <span className="muted" style={{ fontSize: 12 }}>—</span>
          : allDay.map((e) => <button key={evKey(e)} className={"cal-pill" + (isCancelled(e) ? " cancelled" : "")}
              onClick={() => onPick(e)}>{e.recurring ? "↻ " : ""}{e.title}</button>)}
      </div>
    </div>
    <div className="cal-tg">
      {hours.map((h) => (
        <div className="cal-hour" key={h}>
          <span className="hl" aria-hidden="true">{hlabel(h)}</span>
          <span className="slot" onClick={() => onAdd(k, `${pad(h)}:00`)} />
        </div>
      ))}
      <div className="cal-blocks">
        {laid.map((l) => (
          <button key={evKey(l.e)} className={"cal-block" + (isCancelled(l.e) ? " cancelled" : "")} onClick={() => onPick(l.e)}
            style={{ top: l.top, height: l.height, left: `${(l.col / l.cols) * 100}%`, width: `calc(${100 / l.cols}% - 3px)` }}>
            <span className="bt">{l.e.recurring ? "↻ " : ""}{l.e.title}</span>
            <span className="bs">{timeLabel(l.e)}{kindTag(l.e) ? ` · ${kindTag(l.e)}` : ""}</span>
          </button>
        ))}
      </div>
      {showNow && <div className="cal-now" aria-hidden="true" style={{ top: (nowMin - lo * 60) / 60 * ROWH }} />}
    </div>
  </>;
}

// ---------- sheets (use the app's Modal: Esc / focus / portal / scroll-lock) ----------
function EventSheet({ ev, onClose, onReschedule, onCancel }: {
  ev: CalEvent; onClose: () => void;
  onReschedule: (e: CalEvent, d: string, t?: string) => Promise<void>; onCancel: (e: CalEvent) => Promise<void>;
}) {
  const [mode, setMode] = useState<"view" | "resched" | "confirm">("view");
  const [date, setDate] = useState((ev.starts_at || "").slice(0, 10));
  const [time, setTime] = useState(isTimed(ev) ? ev.starts_at!.slice(11, 16) : "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [savedRem, setSavedRem] = useState(false);
  const inflight = useRef(false);
  const pendingVal = useRef<Reminder[] | null>(null);
  useEffect(() => { let go = true; calGetReminders(ev.id).then((r) => { if (go) setReminders(r.reminders); }).catch(() => {}); return () => { go = false; }; }, [ev.id]);
  // Serialize saves: rapid toggles coalesce to the latest set, sent in order — so the
  // server's full-replace can't be left in a stale state by an out-of-order response.
  async function flushSave() {
    if (inflight.current) return;
    while (pendingVal.current) {
      const v = pendingVal.current; pendingVal.current = null; inflight.current = true;
      try { await calSetReminders(ev.id, v); setSavedRem(true); setTimeout(() => setSavedRem(false), 2000); }
      catch (e: any) { setErr(String(e?.message || e)); }
      finally { inflight.current = false; }
    }
  }
  function changeReminders(v: Reminder[]) { setReminders(v); setSavedRem(false); pendingVal.current = v; flushSave(); }
  async function run(fn: () => Promise<void>) {
    setBusy(true); setErr("");
    try { await fn(); } catch (e: any) { setErr(String(e?.message || e)); setBusy(false); }
  }
  const footer = ev.recurring
    ? <span className="cal-recnote">Recurring — edit the source note to change the pattern.</span>
    : mode === "view"
      ? <>
          <button className="ghost" onClick={() => setMode("resched")} disabled={busy}>Reschedule</button>
          <button className="ghost" style={{ color: "var(--danger)" }} onClick={() => setMode("confirm")} disabled={busy}>Cancel event</button>
        </>
      : mode === "confirm"
        ? <>
            <button className="ghost" onClick={() => setMode("view")} disabled={busy}>Keep</button>
            <button className="primary" style={{ background: "var(--danger)", borderColor: "var(--danger)" }}
              disabled={busy} onClick={() => run(() => onCancel(ev))}>Confirm cancel</button>
          </>
        : <>
            <button className="ghost" onClick={() => setMode("view")} disabled={busy}>Back</button>
            <button className="primary" disabled={busy || !date}
              onClick={() => run(() => onReschedule(ev, date, time || undefined))}>Save</button>
          </>;
  return (
    <Modal title={ev.title} size="compact" onClose={onClose} footer={footer}>
      <p className="muted" style={{ margin: "0 0 6px" }}>
        {timeLabel(ev) === "all-day" ? fmtDate(dayKey(ev)) : `${fmtDate(dayKey(ev))} · ${timeLabel(ev)}`}
        {ev.kind !== "event" ? ` · ${ev.kind}` : ""}{ev.recurring ? " · recurring" : ""}{isCancelled(ev) ? " · cancelled" : ""}
      </p>
      {ev.location_label && <p className="muted" style={{ margin: "0 0 6px" }}>◇ {ev.location_label}</p>}
      {err && <p style={{ color: "var(--danger)", fontSize: 13 }}>{err}</p>}
      {mode === "confirm" && <p style={{ fontSize: 13 }}>Cancel “{ev.title}”? This writes a cancellation note.</p>}
      {mode === "resched" && <>
        <div className="cal-field"><label>NEW DATE</label>
          <input type="date" value={date} onChange={(e) => setDate(e.target.value)} /></div>
        <div className="cal-field"><label>NEW TIME (blank = all-day)</label>
          <input type="time" value={time} onChange={(e) => setTime(e.target.value)} /></div>
      </>}
      {mode === "view" && <>
        <RemindChips allDay={!isTimed(ev)} value={reminders} onChange={changeReminders} />
        {ev.recurring && <p className="cal-recnote">Applies to the whole series.</p>}
        {savedRem && <p className="cal-recnote" style={{ color: "var(--ok)" }}>Reminders saved.</p>}
      </>}
      {ev.note_slug && <p style={{ marginTop: 8 }}><Link className="ghost" to={`/note/${ev.note_slug}`}>Open note</Link></p>}
      <p className="cal-recnote">Edits write a note — the calendar re-derives.</p>
    </Modal>
  );
}

function QuickAddSheet({ date, time, onClose, onAdd }: {
  date: string; time?: string; onClose: () => void;
  onAdd: (b: { title: string; date: string; time?: string; kind: string; reminders?: Reminder[] }) => Promise<void>;
}) {
  const [title, setTitle] = useState("");
  const [d, setD] = useState(date);
  const [t, setT] = useState(time || "");
  const [kind, setKind] = useState("event");
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  // All-day reminders need a clock anchor; switching to/from a time flips the preset set,
  // so clear any reminders that no longer match the new anchor.
  const allDay = !t;
  function setTimeAndFix(v: string) {
    setT(v);
    const want = v ? "start" : "day_of";
    setReminders((rs) => rs.filter((r) => (r.anchor || "start") === want));
  }
  async function add() {
    if (!title.trim() || !d) return;
    setBusy(true); setErr("");
    try { await onAdd({ title: title.trim(), date: d, time: t || undefined, kind, reminders: reminders.length ? reminders : undefined }); }
    catch (e: any) { setErr(String(e?.message || e)); setBusy(false); }
  }
  return (
    <Modal title="New event" size="compact" onClose={onClose}
      footer={<button className="primary" disabled={busy || !title.trim() || !d} onClick={add}>Add</button>}>
      {err && <p style={{ color: "var(--danger)", fontSize: 13 }}>{err}</p>}
      <div className="cal-field"><label>TITLE</label>
        <input autoFocus placeholder="What…" value={title} onChange={(e) => setTitle(e.target.value)} /></div>
      <div className="row" style={{ gap: 8 }}>
        <div className="cal-field" style={{ flex: 1 }}><label>DATE</label>
          <input type="date" value={d} onChange={(e) => setD(e.target.value)} /></div>
        <div className="cal-field" style={{ flex: 1 }}><label>TIME</label>
          <input type="time" value={t} onChange={(e) => setTimeAndFix(e.target.value)} /></div>
      </div>
      <div className="cal-field"><label>KIND</label>
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
        </select></div>
      <RemindChips allDay={allDay} value={reminders} onChange={setReminders} />
      <p className="cal-recnote">Writes a dated note — your notes stay the source of truth.</p>
    </Modal>
  );
}

// ---------- in-calendar review of overnight auto-extracted events ----------
function addedWhen(e: AddedEvent): string {
  const s = e.starts_at || "";
  if (!s) return "(undated)";
  return (e.all_day || !s.includes("T")) ? s.slice(0, 10) : s.slice(0, 16).replace("T", " ");
}
function ReviewBanner({ refreshKey }: { refreshKey: number }) {
  const [items, setItems] = useState<AddedEvent[]>([]);
  const [open, setOpen] = useState(false);
  const [revoked, setRevoked] = useState<Record<number, string>>({});   // id -> identity_key (for Undo)
  useEffect(() => {
    let go = true;
    calRecentlyAdded().then((r) => { if (go) setItems(r.events); }).catch(() => {});
    return () => { go = false; };
  }, [refreshKey]);
  if (items.length === 0) return null;
  async function revoke(e: AddedEvent) {
    try { const r = await calDismiss(e.id); setRevoked((m) => ({ ...m, [e.id]: r.identity_key })); } catch { /* ignore */ }
  }
  async function undo(e: AddedEvent) {
    const ik = revoked[e.id]; if (!ik) return;
    try { await calUndismiss(ik); setRevoked((m) => { const n = { ...m }; delete n[e.id]; return n; }); } catch { /* ignore */ }
  }
  async function done() { try { await calMarkReviewed(); } catch { /* ignore */ } setItems([]); }
  return (
    <div className="cal-review">
      <button className="cal-review-bar" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span>{items.length} added by auto-extraction</span>
        <span className="muted">{open ? "Hide" : "Review"}</span>
      </button>
      {open && <div className="cal-card" style={{ marginTop: 6 }}>
        {items.map((e) => {
          const gone = !!revoked[e.id];
          return (
            <div className="cal-row" key={e.id} style={{ cursor: "default" }}>
              <span className="when">{addedWhen(e)}</span>
              <span className="ttl" style={gone ? { textDecoration: "line-through", color: "var(--text-dim)" } : undefined}>{e.title}</span>
              <span className="meta" style={{ gap: 8 }}>
                {e.note_slug && <Link className="open-day" to={`/note/${e.note_slug}`}>note</Link>}
                {gone ? <button className="open-day" onClick={() => undo(e)}>Undo</button>
                      : <button className="open-day" style={{ color: "var(--danger)" }} onClick={() => revoke(e)}>Revoke</button>}
              </span>
            </div>
          );
        })}
        <div className="row" style={{ justifyContent: "flex-end", padding: 8 }}>
          <button className="cal-today" onClick={done}>Done</button>
        </div>
      </div>}
    </div>
  );
}
