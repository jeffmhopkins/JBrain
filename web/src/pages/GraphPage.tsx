import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import ForceGraph2D from "react-force-graph-2d";
import { get } from "../api";

interface GNode { id: number; title: string; slug: string; kind: string; val: number }
interface GLink { source: number | GNode; target: number | GNode }
interface GraphData { nodes: GNode[]; links: GLink[] }

const NODE_REL = 5;   // node radius scale; labels are positioned from this

// Colour nodes by kind. Entries keep the sky accent; KB articles are amber so the
// two layers read apart at a glance; anything else falls back to slate.
const KIND_COLOR: Record<string, string> = { entry: "#38bdf8", kb: "#f59e0b", list: "#a78bfa" };
const colorOf = (kind: string) => KIND_COLOR[kind] ?? "#94a3b8";

type Kind = "all" | "kb" | "entry" | "list";
const linkEnd = (e: number | GNode) => (typeof e === "object" ? e.id : e);

export default function GraphPage() {
  const navigate = useNavigate();
  const [data, setData] = useState<GraphData>({ nodes: [], links: [] });
  const [kind, setKind] = useState<Kind>("all");
  const [focusId, setFocusId] = useState<number | null>(null);
  const [depth, setDepth] = useState(2);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 600, h: 600 });

  useEffect(() => { get<GraphData>("/api/graph").then(setData).catch(() => {}); }, []);

  useEffect(() => {
    const measure = () => {
      if (wrapRef.current) setSize({ w: wrapRef.current.clientWidth, h: wrapRef.current.clientHeight });
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, []);

  // Apply the kind filter, then (if a node is focused) keep only its neighbourhood
  // within `depth` hops. Fresh objects each time so the force sim owns its own
  // copies and never mutates the source data.
  const view = useMemo<GraphData>(() => {
    const ids = new Set(data.nodes.filter((n) => kind === "all" || n.kind === kind).map((n) => n.id));
    let links = data.links
      .map((l) => ({ source: linkEnd(l.source), target: linkEnd(l.target) }))
      .filter((l) => ids.has(l.source as number) && ids.has(l.target as number));

    let keep = ids;
    if (focusId != null && ids.has(focusId)) {
      const adj = new Map<number, number[]>();
      for (const l of links) {
        const s = l.source as number, t = l.target as number;
        (adj.get(s) ?? adj.set(s, []).get(s)!).push(t);
        (adj.get(t) ?? adj.set(t, []).get(t)!).push(s);
      }
      keep = new Set([focusId]);
      let frontier = [focusId];
      for (let d = 0; d < depth; d++) {
        const next: number[] = [];
        for (const u of frontier) for (const v of adj.get(u) ?? []) if (!keep.has(v)) { keep.add(v); next.push(v); }
        frontier = next;
      }
      links = links.filter((l) => keep.has(l.source as number) && keep.has(l.target as number));
    }
    return { nodes: data.nodes.filter((n) => keep.has(n.id)).map((n) => ({ ...n })), links };
  }, [data, kind, focusId, depth]);

  const focusNode = focusId != null ? data.nodes.find((n) => n.id === focusId) : undefined;
  const kinds: { key: Kind; label: string }[] = [
    { key: "all", label: "All" }, { key: "kb", label: "KB" },
    { key: "entry", label: "Entries" }, { key: "list", label: "Lists" },
  ];

  return (
    <div className="content" style={{ maxWidth: "none", height: "100%", display: "flex", flexDirection: "column" }}>
      <h2 style={{ marginTop: 0 }}>Knowledge graph</h2>

      <div className="graph-controls">
        <span className="seg">
          {kinds.map((k) => (
            <button key={k.key} className={kind === k.key ? "primary" : "ghost"} onClick={() => setKind(k.key)}>{k.label}</button>
          ))}
        </span>

        <label className="graph-depth">
          Depth
          <select className="graph-select" value={depth} onChange={(e) => setDepth(Number(e.target.value))} disabled={focusId == null}>
            <option value={1}>1 hop</option>
            <option value={2}>2 hops</option>
            <option value={3}>3 hops</option>
            <option value={99}>All hops</option>
          </select>
        </label>

        <span className="spacer" />

        <span className="graph-legend">
          <span><i className="legend-dot" style={{ background: KIND_COLOR.entry }} /> Entry</span>
          <span><i className="legend-dot" style={{ background: KIND_COLOR.list }} /> List</span>
          <span><i className="legend-dot" style={{ background: KIND_COLOR.kb }} /> KB</span>
        </span>
      </div>

      {focusNode && (
        <div className="graph-focus">
          <i className="legend-dot" style={{ background: colorOf(focusNode.kind) }} />
          <strong>{focusNode.title}</strong>
          <span className="muted" style={{ fontSize: 12 }}>+{depth >= 99 ? "all" : depth} hops</span>
          <span className="spacer" />
          <button className="ghost" onClick={() => navigate(`/note/${focusNode.slug}`)}>Open note</button>
          <button className="ghost" onClick={() => setFocusId(null)}>Clear ✕</button>
        </div>
      )}

      {data.nodes.length === 0 && <p className="muted">No notes to graph yet.</p>}

      <div ref={wrapRef} style={{ flex: 1, minHeight: 0 }}>
        <ForceGraph2D
          width={size.w}
          height={size.h}
          graphData={view as any}
          nodeId="id"
          nodeLabel="title"
          nodeVal="val"
          nodeRelSize={NODE_REL}
          nodeColor={(n: any) => colorOf(n.kind)}
          linkColor={() => "rgba(148,163,184,0.4)"}
          backgroundColor="transparent"
          onBackgroundClick={() => setFocusId(null)}
          onNodeClick={(n: any) => (focusId === n.id ? navigate(`/note/${n.slug}`) : setFocusId(n.id))}
          nodeCanvasObjectMode={() => "after"}
          nodeCanvasObject={(node: any, ctx, scale) => {
            const r = Math.sqrt(Math.max(node.val || 1, 1)) * NODE_REL;
            // Ring the focused node so the selection is obvious.
            if (node.id === focusId) {
              ctx.beginPath();
              ctx.arc(node.x, node.y, r + 3 / scale, 0, 2 * Math.PI);
              ctx.lineWidth = 2 / scale;
              ctx.strokeStyle = "#e6edf3";
              ctx.stroke();
            }
            const fontSize = 13 / scale;
            ctx.font = `600 ${fontSize}px system-ui, sans-serif`;
            ctx.textAlign = "center";
            ctx.textBaseline = "top";
            const y = node.y + r + 4 / scale;
            ctx.lineWidth = 3 / scale;
            ctx.lineJoin = "round";
            ctx.strokeStyle = "rgba(8,10,14,0.92)";
            ctx.strokeText(node.title, node.x, y);
            ctx.fillStyle = "#e6edf3";
            ctx.fillText(node.title, node.x, y);
          }}
        />
      </div>
    </div>
  );
}
