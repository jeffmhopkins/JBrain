import { useState } from "react";
import { Link } from "react-router-dom";
import { createChatShare, getAccessKey, post } from "../api";
import { cryptoAvailable, generateChannelKey, guestPassword, newFragmentSecret, newOtp, ownerPassword, wrapKey } from "../crypto";
import { fmtTs } from "../time";
import { useAuth } from "../App";

export interface ChatLink {
  link_id: number; persist: boolean; otp_required: boolean; status: "active" | "closed";
  guest_name: string | null; created_at: string; closed_at: string | null;
  last_guest_at: string | null; last_owner_at: string | null;
  url: string; token: string; label: string | null; expires_at: string | null; saved_note_slug: string | null;
}

// The recipient link's secret fragment is never stored server-side (zero-knowledge), so the
// full URL is cached on THIS device at creation for re-copy. Cleared with the browser.
const linkKey = (id: number) => `jbrain_chat_link_${id}`;
const storedLink = (id: number) => { try { return localStorage.getItem(linkKey(id)) || ""; } catch { return ""; } };

export default function ChatShareLinks({ links, reload }: { links: ChatLink[]; reload: () => void }) {
  const { appTz } = useAuth();
  const [open, setOpen] = useState(false);
  const [persist, setPersist] = useState(true);
  const [otp, setOtp] = useState(false);
  const [label, setLabel] = useState("");
  const [ttl, setTtl] = useState(7);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ url: string; otp: string } | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  async function copy(text: string, tag: string) {
    try { await navigator.clipboard.writeText(text); setCopied(tag); setTimeout(() => setCopied(null), 1500); }
    catch { prompt("Copy:", text); }
  }

  async function create() {
    if (!cryptoAvailable()) { alert("This browser can't generate the encryption key here (needs a secure https connection)."); return; }
    const accessKey = getAccessKey();
    if (!accessKey) { alert("No access key on this device."); return; }
    setBusy(true);
    try {
      // Generate the channel key and seal it twice — once for owner devices (access key),
      // once for the recipient (link fragment [+ OTP]). The raw key never leaves this tab.
      const { raw } = await generateChannelKey();
      const secret = newFragmentSecret();
      const code = otp ? newOtp() : "";
      const owner_wrap = await wrapKey(raw, ownerPassword(accessKey));
      const guest_wrap = await wrapKey(raw, guestPassword(secret, code || undefined));
      const r = await createChatShare({ owner_wrap, guest_wrap, persist, otp_required: otp,
                                        label: label.trim() || undefined, ttl_days: ttl > 0 ? ttl : undefined });
      const full = `${r.url}#s=${secret}`;
      try { localStorage.setItem(linkKey(r.link_id), full); } catch { /* private mode */ }
      setResult({ url: full, otp: code });
      setLabel("");
      reload();
    } catch (e: any) { alert(e?.message || "Couldn't create the chat link."); }
    finally { setBusy(false); }
  }

  async function revoke(l: ChatLink) {
    if (!confirm("Revoke this chat link? It stops working immediately.")) return;
    try { await post(`/api/shares/${l.link_id}/revoke`); } catch { /* ignore */ }
    reload();
  }

  return (
    <>
      <div className="adv-section row" style={{ alignItems: "center" }}>
        Encrypted chats
        <span className="spacer" />
        <button className="ghost" style={{ fontSize: 13 }} onClick={() => { setOpen((o) => !o); setResult(null); }}>
          {open ? "Close" : "New encrypted chat"}
        </button>
      </div>

      {open && (
        <div className="card">
          {result ? (
            <div>
              <p className="muted" style={{ marginTop: 0 }}>
                Share this link with the person you want to chat with. <strong>Copy it now</strong> — for security it
                isn't stored on the server and can only be re-copied from this browser.
              </p>
              <div className="row" style={{ gap: 6 }}>
                <input readOnly value={result.url} onFocus={(e) => e.currentTarget.select()} style={{ fontSize: 12 }} />
                <button className="ghost" onClick={() => copy(result.url, "url")}>{copied === "url" ? "Copied" : "Copy link"}</button>
              </div>
              {result.otp && (
                <div className="row" style={{ gap: 6, marginTop: 8, alignItems: "center" }}>
                  <span className="muted" style={{ fontSize: 13 }}>One-time code (send this <strong>separately</strong>):</span>
                  <code className="echat-otp">{result.otp}</code>
                  <button className="ghost" onClick={() => copy(result.otp, "otp")}>{copied === "otp" ? "Copied" : "Copy code"}</button>
                </div>
              )}
              <div className="row" style={{ marginTop: 12 }}>
                <button className="primary" onClick={() => setResult(null)}>Create another</button>
              </div>
            </div>
          ) : (
            <div>
              <label className="share-field">Label (for your own reference)
                <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Chat with Sarah" /></label>
              <div className="row" style={{ gap: 16, flexWrap: "wrap", marginTop: 8 }}>
                <label className="row" style={{ gap: 6 }}>
                  <input type="checkbox" className="share-check" checked={persist} onChange={(e) => setPersist(e.target.checked)} />
                  Keep history (off = ephemeral)</label>
                <label className="row" style={{ gap: 6 }}>
                  <input type="checkbox" className="share-check" checked={otp} onChange={(e) => setOtp(e.target.checked)} />
                  Require a one-time code</label>
                <label className="row" style={{ gap: 6 }}>Expires in
                  <input type="number" min={0} style={{ width: 64 }} value={ttl}
                         onChange={(e) => setTtl(Math.max(0, +e.target.value))} /> days (0 = never)</label>
              </div>
              <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
                End-to-end encrypted, strictly 1:1 (locks to the first browser that joins). When it ends, the
                conversation is saved to your brain for analysis.
              </p>
              <div className="row" style={{ marginTop: 8 }}>
                <button className="primary" disabled={busy} onClick={create}>{busy ? "Creating…" : "Create chat link"}</button>
              </div>
            </div>
          )}
        </div>
      )}

      {links.map((l) => {
        const waiting = !!l.last_guest_at && l.status === "active";
        const cached = storedLink(l.link_id);
        return (
          <div className="card" key={"chat" + l.link_id}>
            <div className="row">
              <strong>{l.label || l.guest_name || "Encrypted chat"}</strong>
              <span className={"badge " + (l.status === "active" ? "prio" : "")}>{l.status === "active" ? "open" : "ended"}</span>
              <span className="badge">{l.persist ? "history" : "ephemeral"}</span>
              {l.otp_required ? <span className="badge" title="Requires a one-time code">🔑 code</span> : null}
              {l.guest_name && <span className="badge">{l.guest_name}</span>}
              <span className="spacer" />
              <span className="muted" style={{ fontSize: 12 }}>{fmtTs(l.created_at, appTz)}</span>
            </div>
            <div className="row" style={{ marginTop: 6, gap: 6, alignItems: "center" }}>
              <Link className="primary" to={`/shares/chat/${l.link_id}`} style={{ padding: "4px 10px", textDecoration: "none" }}>
                {waiting ? "Join — waiting" : "Open chat"}
              </Link>
              {cached && l.status === "active" && (
                <button className="ghost" onClick={() => copy(cached, "l" + l.link_id)}>{copied === "l" + l.link_id ? "Copied" : "Copy link"}</button>
              )}
              {l.saved_note_slug && <Link className="ghost" to={`/note/${l.saved_note_slug}`}>Saved note</Link>}
              {l.status === "active" && <button className="ghost danger-hover" onClick={() => revoke(l)}>Revoke</button>}
            </div>
          </div>
        );
      })}
    </>
  );
}
