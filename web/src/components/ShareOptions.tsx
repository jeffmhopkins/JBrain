// Standardized share-link options, reused by every share kind (note / guided / research / labs)
// so "lock to browser", "single use", expiry, and run caps look and behave the same everywhere.
// Each kind picks which controls apply via `show`; feature-specific options live alongside it.

export interface ShareCommonOptions {
  bind: boolean;            // lock to the first browser that opens it
  single_use: boolean;     // close after one completed use
  ttl_days: number;        // expiry; 0 = never (unless a minimum is enforced)
  max_turns?: number;      // per-session AI replies (AI links only)
  max_total_replies?: number; // cumulative AI replies across the link (cost cap)
}

export const DEFAULT_SHARE_OPTIONS: ShareCommonOptions = {
  bind: false, single_use: false, ttl_days: 0, max_turns: 30, max_total_replies: 200,
};

export default function ShareOptions({ value, onChange, show, disabled }: {
  value: ShareCommonOptions;
  onChange: (patch: Partial<ShareCommonOptions>) => void;
  // Which controls to show + constraints. `ttlMin > 0` removes the "never" option (e.g. PHI links).
  show?: { singleUse?: boolean; caps?: boolean; ttlMin?: number };
  disabled?: boolean;
}) {
  const s = show || {};
  const ttlMin = s.ttlMin ?? 0;
  return (
    <div className="share-options">
      <div className="row" style={{ gap: 14, flexWrap: "wrap", marginTop: 4 }}>
        <label className="row" style={{ gap: 6 }}>
          <input type="checkbox" style={{ width: "auto" }} checked={value.bind} disabled={disabled}
                 onChange={(e) => onChange({ bind: e.target.checked })} /> Lock to first browser</label>
        {s.singleUse !== false && (
          <label className="row" style={{ gap: 6 }}>
            <input type="checkbox" style={{ width: "auto" }} checked={value.single_use} disabled={disabled}
                   onChange={(e) => onChange({ single_use: e.target.checked })} /> Single use</label>
        )}
        <label className="row" style={{ gap: 6 }}>Expires in
          <input type="number" min={ttlMin} style={{ width: 64 }} value={value.ttl_days} disabled={disabled}
                 onChange={(e) => onChange({ ttl_days: Math.max(ttlMin, +e.target.value) })} />
          days{ttlMin > 0 ? "" : " (0 = never)"}</label>
      </div>
      {s.caps && (
        <details style={{ marginTop: 6 }}>
          <summary className="muted" style={{ fontSize: 12 }}>Usage caps</summary>
          <div className="row" style={{ gap: 14, flexWrap: "wrap", marginTop: 6 }}>
            <label className="row" style={{ gap: 6 }}>Replies per session
              <input type="number" min={1} style={{ width: 64 }} value={value.max_turns ?? 30} disabled={disabled}
                     onChange={(e) => onChange({ max_turns: Math.max(1, +e.target.value) })} /></label>
            <label className="row" style={{ gap: 6 }}>Total replies
              <input type="number" min={1} style={{ width: 72 }} value={value.max_total_replies ?? 200} disabled={disabled}
                     onChange={(e) => onChange({ max_total_replies: Math.max(1, +e.target.value) })} /></label>
          </div>
        </details>
      )}
    </div>
  );
}
