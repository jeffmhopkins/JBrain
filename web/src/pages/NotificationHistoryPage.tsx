import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { get } from "../api";
import { useAuth } from "../App";
import { fmtTsShort } from "../time";
import { reviewHref } from "../util";

interface HistoryItem {
  id: number;
  title: string;
  message: string;
  link_slug: string | null;
  created_at: string;
  dismissed_at: string | null;
}

// Notifications dismissed from the bell in the last 24h — a short-lived archive so a
// just-cleared alert can be reopened. The server enforces the 24h window; this is
// read-only (no re-dismiss), so the bell's pending count is untouched.
export default function NotificationHistoryPage() {
  const { appTz } = useAuth();
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    get("/api/reviews/history")
      .then((xs) => setItems(xs))
      .catch(() => { /* offline / error → empty */ })
      .finally(() => setLoaded(true));
  }, []);

  return (
    <div className="content">
      <h2>Notification History</h2>
      <p className="muted" style={{ fontSize: 13 }}>
        Notifications you dismissed in the last 24 hours.
      </p>
      {loaded && items.length === 0 && <p className="muted">Nothing dismissed in the last 24 hours.</p>}
      {items.map((r) => (
        <div className="card" key={r.id}>
          <div className="row">
            <strong>{r.title}</strong>
            <span className="spacer" />
            <span className="muted" style={{ fontSize: 11 }}>{fmtTsShort(r.dismissed_at || r.created_at, appTz)}</span>
          </div>
          {r.message && <p style={{ margin: "6px 0", whiteSpace: "pre-wrap" }}>{r.message}</p>}
          {r.link_slug && (
            <div className="row" style={{ gap: 8, marginTop: 8 }}>
              <Link className="primary" style={{ padding: "8px 14px", borderRadius: 8 }} to={reviewHref(r.link_slug)}>Open</Link>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
