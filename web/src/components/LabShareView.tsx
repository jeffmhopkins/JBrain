import { KeyboardEvent, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { labsStart, labsTurn, getShareLabSeries, LabSeries, LabPoint } from "../api";
import LabChart from "./LabChart";
import { Icon } from "./Icon";

const OUT = new Set(["high", "low", "abnormal"]);
const statusColor = (s: string) => OUT.has(s) ? "var(--danger)" : s === "normal" ? "var(--ok)" : "var(--text-dim)";

// Recipient TABLE view: a trend table (rows = collection dates, columns = shared analytes) so a
// recipient can read the exact value of any shared result at any time. Values are status-coloured.
function LabShareTable({ token, analytes }: { token: string; analytes: { analyte: string; test_name: string; unit: string | null }[] }) {
  const [series, setSeries] = useState<Record<string, LabSeries>>({});
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    Promise.all(analytes.map((a) =>
      getShareLabSeries(token, a.analyte).then((s) => [a.analyte, s] as const).catch(() => [a.analyte, null] as const)))
      .then((pairs) => {
        const m: Record<string, LabSeries> = {};
        pairs.forEach(([k, s]) => { if (s) m[k] = s; });
        setSeries(m); setLoading(false);
      });
  }, [token]);
  if (loading) return <div className="muted" style={{ fontSize: 13, padding: 8 }}>Loading…</div>;
  const cols = analytes.filter((a) => series[a.analyte]?.points.length);
  const lookup: Record<string, Record<string, LabPoint>> = {};
  const dateSet = new Set<string>();
  cols.forEach((a) => { lookup[a.analyte] = {}; series[a.analyte].points.forEach((p) => { lookup[a.analyte][p.t] = p; dateSet.add(p.t); }); });
  const dates = [...dateSet].sort().reverse();   // newest first
  if (!dates.length) return <div className="muted" style={{ fontSize: 13 }}>No results to show.</div>;
  return (
    <div className="lab-share-table-wrap">
      <table className="lab-share-table">
        <thead><tr><th>Date</th>{cols.map((a) => (
          <th key={a.analyte}>{a.test_name}{a.unit ? <span className="muted"> {a.unit}</span> : ""}</th>))}</tr></thead>
        <tbody>
          {dates.map((d) => (
            <tr key={d}>
              <td className="muted" style={{ whiteSpace: "nowrap" }}>{d}</td>
              {cols.map((a) => { const p = lookup[a.analyte][d];
                return <td key={a.analyte} style={{ color: p ? statusColor(p.status) : undefined, fontWeight: p && OUT.has(p.status) ? 600 : undefined }}>
                  {p ? p.vtext : "—"}</td>; })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Recipient view of a lab-share link (kind='labs'): a fixed selection of trend charts the owner
// chose to share, plus an optional scoped AI chat. ALL series come through the token-scoped,
// allow-list-rechecked, no-store endpoint — the recipient never touches /api/medical/*.

type Analyte = { analyte: string; test_name: string; unit: string | null };
type ChartSpec = { analyte: string; unit: string | null; from: string; to: string; title: string };

// One chart: fetches its scoped series for THIS token and renders the shared LabChart SVG.
function ShareChart({ token, analyte, from, to, title }:
  { token: string; analyte: string; from?: string; to?: string; title?: string }) {
  const [series, setSeries] = useState<LabSeries | null>(null);
  const [err, setErr] = useState(false);
  useEffect(() => { getShareLabSeries(token, analyte).then(setSeries).catch(() => setErr(true)); }, [token, analyte]);
  // A failed/empty chart shows a quiet placeholder rather than vanishing silently.
  if (err) return <div className="lab-chart-wrap"><div className="lab-head"><strong>{title || analyte}</strong></div>
    <p className="muted" style={{ fontSize: 12, margin: 0 }}>Couldn’t load this result.</p></div>;
  if (!series) return <div className="muted" style={{ fontSize: 12, padding: 8 }}>Loading {title || analyte}…</div>;
  if (!series.points.length) return null;
  return (
    <div className="lab-chart-wrap">
      <div className="lab-head"><strong>{series.test_name}</strong>
        {series.unit && <span className="muted" style={{ fontSize: 12 }}> ({series.unit})</span>}</div>
      <LabChart series={series} from={from} to={to} height={170} />
    </div>
  );
}

interface Msg { role: "ai" | "me"; text: string; charts?: ChartSpec[]; }

export default function LabShareView({ token, brainName, intro, consent, allowChat }: {
  token: string; brainName: string; intro?: string; consent?: string; allowChat?: boolean;
}) {
  const [phase, setPhase] = useState<"consent" | "viewing">("consent");
  const [view, setView] = useState<"charts" | "table">("charts");
  const [name, setName] = useState(localStorage.getItem("jbrain_share_name") || "");
  const [analytes, setAnalytes] = useState<Analyte[]>([]);
  const [win, setWin] = useState<{ from: string | null; to: string | null }>({ from: null, to: null });
  const [chatOn, setChatOn] = useState(false);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [err, setErr] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [msgs, thinking]);
  useEffect(() => {                                         // grow the composer with its content
    const el = taRef.current;
    if (!el) return;
    el.style.height = "auto";
    const max = Math.round((window.visualViewport?.height ?? window.innerHeight) * 0.4);
    el.style.height = Math.min(el.scrollHeight, max) + "px";
    el.style.overflowY = el.scrollHeight > max ? "auto" : "hidden";
  }, [input]);

  async function begin() {
    if (!name.trim()) { alert("Please enter your name."); return; }
    localStorage.setItem("jbrain_share_name", name.trim());
    setBusy(true); setErr("");
    try {
      const r = await labsStart(token, name.trim());
      setAnalytes(r.analytes || []);
      setWin(r.window || { from: null, to: null });
      setChatOn(!!r.allow_chat);
      setMsgs((r.transcript || []).map((t: any) => ({ role: t.role === "assistant" ? "ai" : "me", text: t.content })));
      setPhase("viewing");
    } catch (e: any) { setErr(e?.message || "Couldn't open this link."); }
    finally { setBusy(false); }
  }

  async function send() {
    const text = input.trim();
    if (!text || thinking) return;
    setInput(""); setMsgs((m) => [...m, { role: "me", text }]); setThinking(true); setErr("");
    try {
      const r = await labsTurn(token, text);
      setMsgs((m) => [...m, { role: "ai", text: r.message, charts: r.charts || [] }]);
    } catch (e: any) { setErr(e?.message || "Couldn't send — please try again."); }
    finally { setThinking(false); }
  }
  function onKey(e: KeyboardEvent) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }

  if (phase === "consent") return (
    <div className="share-page"><div className="share-card">
      <div className="share-head">
        <span className="brand">{brainName}<span className="dot">.</span></span>
        <span className="badge">Shared · Lab results</span>
      </div>
      <h2 style={{ marginTop: 12 }}>{brainName}’s shared lab results</h2>
      {intro && <p>{intro}</p>}
      <div className="share-banner">⚕️ Not a diagnosis. Some values may have been read from a photo —
        always confirm against the original lab reports. This is a fixed selection, not a full record,
        and may not be current.</div>
      {consent && <p className="muted" style={{ fontSize: 13 }}>{consent}</p>}
      <input placeholder="Your name *" value={name} onChange={(e) => setName(e.target.value)}
             onKeyDown={(e) => { if (e.key === "Enter") begin(); }} />
      <div className="row" style={{ marginTop: 12 }}>
        <button className="primary" disabled={busy} onClick={begin}>{busy ? "…" : "View results"}</button>
      </div>
      {err && <p className="share-error">{err}</p>}
    </div></div>
  );

  return (
    <div className="guided-wrap">
      <div className="guided-head">
        <span className="brand">{brainName}<span className="dot">.</span></span>
        <span className="badge">Shared · Lab results</span>
      </div>
      <div className="messages" style={{ paddingBottom: chatOn ? undefined : 24 }}>
        <div className="share-banner">⚕️ Not a diagnosis — always confirm against the source reports.
          Some values may have been read from a photo.</div>
        {analytes.length > 1 && (
          <div className="row" style={{ gap: 6, marginBottom: 4 }}>
            <button className={"chip" + (view === "charts" ? " active" : "")} onClick={() => setView("charts")}>Charts</button>
            <button className={"chip" + (view === "table" ? " active" : "")} onClick={() => setView("table")}>Table</button>
          </div>
        )}
        {view === "table"
          ? <LabShareTable token={token} analytes={analytes} />
          : analytes.map((a) => (
              <ShareChart key={a.analyte} token={token} analyte={a.analyte}
                          from={win.from || undefined} to={win.to || undefined} title={a.test_name} />
            ))}
        {analytes.length === 0 && <div className="muted" style={{ fontSize: 13 }}>No results to show.</div>}

        {chatOn && msgs.length === 0 && (
          <div className="msg assistant muted">Ask a question about these results — e.g. “anything abnormal?”</div>
        )}
        {chatOn && msgs.map((m, i) => (
          <div key={i} className={`msg ${m.role === "me" ? "user" : "assistant"}`}>
            {m.role === "ai" ? <div className="md msg-md"><ReactMarkdown>{m.text}</ReactMarkdown></div> : m.text}
            {m.charts?.map((c, j) => (
              <ShareChart key={j} token={token} analyte={c.analyte} from={c.from} to={c.to} title={c.title} />
            ))}
          </div>
        ))}
        {thinking && (
          <div className="chat-status" aria-live="polite">
            <span className="typing-dots"><span /><span /><span /></span>
            <span className="chat-status-text">Looking…</span>
          </div>
        )}
        {err && <div className="msg assistant share-error">{err}</div>}
        <div ref={endRef} />
      </div>
      {chatOn && (
        <div className="composer-box">
          <textarea ref={taRef} rows={1} placeholder="Ask about these results…" value={input}
                    onChange={(e) => setInput(e.target.value)} onKeyDown={onKey} />
          <div className="composer-row">
            <span className="spacer" />
            <button className="icon-btn send" title="Send" onClick={send}
                    disabled={thinking || !input.trim()}><Icon name="send" /></button>
          </div>
        </div>
      )}
    </div>
  );
}
