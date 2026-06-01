import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import ForceGraph2D from "react-force-graph-2d";
import { get } from "../api";

interface GraphData {
  nodes: { id: number; title: string; slug: string; val: number }[];
  links: { source: number; target: number }[];
}

const NODE_REL = 5;   // node radius scale; labels are positioned from this

export default function GraphPage() {
  const navigate = useNavigate();
  const [data, setData] = useState<GraphData>({ nodes: [], links: [] });
  const wrapRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 600, h: 600 });

  useEffect(() => { get<GraphData>("/api/graph").then(setData).catch(() => {}); }, []);

  useEffect(() => {
    const measure = () => {
      if (wrapRef.current) {
        setSize({ w: wrapRef.current.clientWidth, h: wrapRef.current.clientHeight });
      }
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, []);

  return (
    <div className="content" style={{ maxWidth: "none", height: "100%", display: "flex", flexDirection: "column" }}>
      <h2 style={{ marginTop: 0 }}>Knowledge graph</h2>
      {data.nodes.length === 0 && <p className="muted">No notes to graph yet.</p>}
      <div ref={wrapRef} style={{ flex: 1, minHeight: 0 }}>
        <ForceGraph2D
          width={size.w}
          height={size.h}
          graphData={data as any}
          nodeId="id"
          nodeLabel="title"
          nodeVal="val"
          nodeRelSize={NODE_REL}
          nodeColor={() => "#38bdf8"}
          linkColor={() => "rgba(148,163,184,0.4)"}
          backgroundColor="transparent"
          onNodeClick={(n: any) => navigate(`/note/${n.slug}`)}
          nodeCanvasObjectMode={() => "after"}
          nodeCanvasObject={(node: any, ctx, scale) => {
            const label = node.title as string;
            const fontSize = 13 / scale;
            ctx.font = `600 ${fontSize}px system-ui, sans-serif`;
            ctx.textAlign = "center";
            ctx.textBaseline = "top";
            // Drop the label clear of the node, below its rim.
            const r = Math.sqrt(Math.max(node.val || 1, 1)) * NODE_REL;
            const y = node.y + r + 3 / scale;
            // Dark outline so the bright text stays readable over nodes/edges.
            ctx.lineWidth = 3 / scale;
            ctx.lineJoin = "round";
            ctx.strokeStyle = "rgba(8,10,14,0.92)";
            ctx.strokeText(label, node.x, y);
            ctx.fillStyle = "#e6edf3";
            ctx.fillText(label, node.x, y);
          }}
        />
      </div>
    </div>
  );
}
