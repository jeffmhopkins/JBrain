import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import ForceGraph2D from "react-force-graph-2d";
import { forceCollide } from "d3-force-3d";
import { get } from "../api";
import { leaf, truncate } from "../util";

interface GNode { id: number; title: string; slug: string; kind: string; val: number; _label?: string }
interface GLink { source: number | GNode; target: number | GNode }
interface GraphData { nodes: GNode[]; links: GLink[] }

const NODE_REL = 5;   // node radius scale; labels are positioned from this

// Colour nodes by kind. Entries keep the sky accent; KB articles are amber so the
// two layers read apart at a glance; anything else falls back to slate.
const KIND_COLOR: Record<string, string> = { entry: "#38bdf8", kb: "#f59e0b", list: "#a78bfa" };
const colorOf = (kind: string) => KIND_COLOR[kind] ?? "#94a3b8";

// The canvas is transparent over the page background, so the focus ring must
// contrast with whatever theme is active (near-white on dark, near-black on the
// light tiers). Read the live --text token rather than baking a colour in.
const cssVar = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

type Kind = "all" | "kb" | "entry" | "list";
const linkEnd = (e: number | GNode) => (typeof e === "object" ? e.id : e);

// On-canvas label: strip the root, then show the DISTINGUISHING tail (the last path
// segment) so siblings under a shared folder don't all read "People/Family/…" and get
// clipped right where the name begins. Daily rollups show their date, not a bare day.
function graphLabel(title: string): string {
  const parts = leaf(title).split("/").filter(Boolean);
  if (parts.length === 0) return title;
  if (/^daily$/i.test(parts[0]) && parts.length >= 2) return parts.slice(1).join("-");
  return parts[parts.length - 1];
}

// Rounded-rect path (canvas roundRect isn't universal on older mobile WebViews).
function roundRect(ctx: any, x: number, y: number, w: number, h: number, r: number) {
  const rr = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

export default function GraphPage() {
  const navigate = useNavigate();
  const [data, setData] = useState<GraphData>({ nodes: [], links: [] });
  const [kind, setKind] = useState<Kind>("all");
  const [focusId, setFocusId] = useState<number | null>(null);
  const [depth, setDepth] = useState(1);
  const wrapRef = useRef<HTMLDivElement>(null);
  const fgRef = useRef<any>(null);
  const drawnRef = useRef<{ x: number; y: number; w: number; h: number }[]>([]);   // label boxes drawn this frame
  const [size, setSize] = useState({ w: 600, h: 600 });

  useEffect(() => { get<GraphData>("/api/graph").then(setData).catch(() => {}); }, []);

  // Standard hop budget per layer: KB reads best at 2 hops (its articles connect via
  // shared entries) while the others want 1. Resets on a layer switch, still adjustable.
  useEffect(() => { setDepth(kind === "kb" ? 2 : 1); }, [kind]);

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
  const view = useMemo<{ nodes: GNode[]; links: { source: number; target: number }[]; neighbors: Set<number> }>(() => {
    const ids = new Set(data.nodes.filter((n) => kind === "all" || n.kind === kind).map((n) => n.id));

    // Edges among the kept nodes. Under a single-kind filter we PROJECT through the
    // hidden layers: KB articles mostly cite ENTRIES (not each other), so two KB
    // nodes linked only via a shared entry would otherwise drop and the KB view
    // becomes disconnected dust. We connect two kept nodes when a path exists
    // between them through hidden (other-kind) nodes only.
    let links: { source: number; target: number }[];
    if (kind === "all") {
      links = data.links
        .map((l) => ({ source: linkEnd(l.source), target: linkEnd(l.target) }))
        .filter((l) => ids.has(l.source) && ids.has(l.target) && l.source !== l.target);
    } else {
      const adj = new Map<number, Set<number>>();
      for (const l of data.links) {
        const s = linkEnd(l.source), t = linkEnd(l.target);
        if (s === t) continue;
        (adj.get(s) ?? adj.set(s, new Set()).get(s)!).add(t);
        (adj.get(t) ?? adj.set(t, new Set()).get(t)!).add(s);
      }
      const seen = new Set<string>();
      links = [];
      // Connect two kept nodes when a path runs between them through hidden
      // (other-kind) nodes within `depth` hops — so KB articles linked only via a
      // shared entry still join up, but only inside the chosen hop budget.
      for (const start of ids) {
        const dist = new Map<number, number>([[start, 0]]);
        const queue: number[] = [start];
        while (queue.length) {
          const u = queue.shift()!;
          const d = dist.get(u)!;
          if (d >= depth) continue;                          // don't expand past the hop budget
          for (const v of adj.get(u) ?? []) {
            if (dist.has(v)) continue;
            dist.set(v, d + 1);
            if (ids.has(v)) {                                // a kept node within depth hops → link
              const key = start < v ? `${start}-${v}` : `${v}-${start}`;
              if (start !== v && !seen.has(key)) { seen.add(key); links.push({ source: start, target: v }); }
            } else {                                          // hidden node → keep walking through it
              queue.push(v);
            }
          }
        }
      }
    }

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
    // Precompute the on-canvas label: the distinguishing tail (or date), clipped so a
    // box stays bounded — focused node gets a longer cap. graphLabel() is what stops
    // sibling nodes all reading the same clipped folder prefix.
    const nodes = data.nodes.filter((n) => keep.has(n.id)).map((n) => ({
      ...n,
      _label: truncate(graphLabel(n.title), n.id === focusId ? 30 : 20),
    }));
    // Neighbours of the focused node: their labels are always drawn (never culled).
    const neighbors = new Set<number>();
    if (focusId != null) for (const l of links) {
      if (l.source === focusId) neighbors.add(l.target);
      else if (l.target === focusId) neighbors.add(l.source);
    }
    // Draw order = label priority (first drawn wins the overlap test): the focused
    // node, then its neighbours, then bigger (more-connected) nodes.
    const pri = (n: GNode) => (n.id === focusId ? 3e9 : neighbors.has(n.id) ? 2e9 : 0) + (n.val || 0);
    nodes.sort((a, b) => pri(b) - pri(a));
    return { nodes, links, neighbors };
  }, [data, kind, focusId, depth]);

  // Spread nodes apart so labels have room: a stronger charge plus a collision
  // force sized to the node radius + label padding. Reapply when the view set
  // changes (force-graph rebuilds its sim on new data).
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    fg.d3Force("charge")?.strength(-180);
    fg.d3Force("collide", forceCollide((n: any) => Math.sqrt(Math.max(n.val, 1)) * NODE_REL + 12));
  }, [view]);

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
          Hops
          <select className="graph-select" value={depth} onChange={(e) => setDepth(Number(e.target.value))}>
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

      {data.nodes.length === 0
        ? <p className="muted">No notes to graph yet.</p>
        : !focusNode && <p className="muted" style={{ fontSize: 12, margin: "2px 0 6px" }}>Tap a node to focus · tap again to open</p>}

      <div ref={wrapRef} style={{ flex: 1, minHeight: 0 }}>
        <ForceGraph2D
          ref={fgRef}
          width={size.w}
          height={size.h}
          graphData={{ nodes: view.nodes, links: view.links } as any}
          nodeId="id"
          nodeLabel="title"
          nodeVal="val"
          nodeRelSize={NODE_REL}
          nodeColor={(n: any) => colorOf(n.kind)}
          linkColor={() => "rgba(148,163,184,0.4)"}
          backgroundColor="transparent"
          cooldownTicks={120}
          onEngineStop={() => fgRef.current?.zoomToFit(400, 40)}
          onBackgroundClick={() => setFocusId(null)}
          onNodeClick={(n: any) => (focusId === n.id ? navigate(`/note/${n.slug}`) : setFocusId(n.id))}
          onRenderFramePre={() => { drawnRef.current = []; }}   // reset per-frame label boxes
          nodeCanvasObjectMode={() => "after"}
          nodeCanvasObject={(node: any, ctx, scale) => {
            const r = Math.sqrt(Math.max(node.val || 1, 1)) * NODE_REL;
            // Ring the focused node so the selection is obvious.
            if (node.id === focusId) {
              ctx.beginPath();
              ctx.arc(node.x, node.y, r + 3 / scale, 0, 2 * Math.PI);
              ctx.lineWidth = 2 / scale;
              ctx.strokeStyle = cssVar("--text") || "#e6edf3";
              ctx.stroke();
            }
            const label = node._label ?? graphLabel(node.title);
            if (!label) return;
            const fontSize = 13 / scale;
            ctx.font = `600 ${fontSize}px system-ui, sans-serif`;
            const tw = ctx.measureText(label).width;
            const padX = 4 / scale, padY = 2.5 / scale;
            const top = node.y + r + 4 / scale;
            const box = { x: node.x - tw / 2 - padX, y: top - padY, w: tw + 2 * padX, h: fontSize + 2 * padY };
            // Cull overlap: a label is drawn only if it clears every higher-priority
            // label already placed this frame. The focused node and its neighbours are
            // exempt (always shown). Zoom in → boxes shrink in graph-space → more fit.
            const must = node.id === focusId || view.neighbors.has(node.id);
            const hit = drawnRef.current.some((d) =>
              box.x < d.x + d.w && box.x + box.w > d.x && box.y < d.y + d.h && box.y + box.h > d.y);
            if (!must && hit) return;
            drawnRef.current.push(box);
            ctx.fillStyle = "rgba(8,10,14,0.82)";   // pill for contrast over nodes/edges
            roundRect(ctx, box.x, box.y, box.w, box.h, 3 / scale);
            ctx.fill();
            ctx.fillStyle = node.id === focusId ? "#ffffff" : "#dfe7ef";
            ctx.textAlign = "center";
            ctx.textBaseline = "top";
            ctx.fillText(label, node.x, top);
          }}
        />
      </div>
    </div>
  );
}
