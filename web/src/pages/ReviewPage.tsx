import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { get, post } from "../api";
import { useAuth } from "../App";
import { fmtTs } from "../time";
import { reviewHref } from "../util";

interface ReviewItem {
  id: number;
  title: string;
  message: string;
  link_slug: string | null;
  created_at: string;
}

export default function ReviewPage() {
  const { appTz } = useAuth();
  const [items, setItems] = useState<ReviewItem[]>([]);

  async function load() {
    try { setItems(await get("/api/reviews")); } catch { /* ignore */ }
  }
  useEffect(() => { load(); }, []);

  async function dismiss(id: number) {
    await post(`/api/reviews/${id}/dismiss`);
    setItems((xs) => xs.filter((x) => x.id !== id));
  }

  return (
    <div className="content">
      <h2>Review</h2>
      <p className="muted" style={{ fontSize: 13 }}>
        Items triggers have flagged for your attention (e.g. daily reviews). Dismiss when handled.
      </p>
      {items.length === 0 && <p className="muted">Nothing to review. 🎉</p>}
      {items.map((r) => (
        <div className="card" key={r.id}>
          <div className="row">
            <strong>{r.title}</strong>
            <span className="spacer" />
            <span className="muted" style={{ fontSize: 11 }}>{fmtTs(r.created_at, appTz)}</span>
          </div>
          {r.message && <p style={{ margin: "6px 0", whiteSpace: "pre-wrap" }}>{r.message}</p>}
          <div className="row" style={{ gap: 8, marginTop: 8 }}>
            {r.link_slug && <Link className="primary" style={{ padding: "8px 14px", borderRadius: 8 }} to={reviewHref(r.link_slug)}>Open entry</Link>}
            <button className="ghost" onClick={() => dismiss(r.id)}>Dismiss</button>
          </div>
        </div>
      ))}
    </div>
  );
}
