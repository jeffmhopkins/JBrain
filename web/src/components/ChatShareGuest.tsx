import { useMemo, useState } from "react";
import { chatGuestFetchFile, chatGuestSend, chatGuestStreamPath, chatGuestUploadFile, chatJoin } from "../api";
import { ChannelKey, cryptoAvailable, guestPassword, unwrapKey } from "../crypto";
import EncryptedChat, { ChatTransport } from "./EncryptedChat";

interface ChatLanding {
  brain_name: string; otp_required?: boolean; persist?: boolean;
  requires_claim?: boolean; guest_wrap?: string; guest_name?: string | null;
}

// The recipient side of an encrypted chat link. The channel key is derived entirely here
// from the URL fragment secret (#s=…) [+ the out-of-band OTP] — neither ever reaches the
// server. Joining claims the link to this browser (1:1).
export default function ChatShareGuest({ token, landing }: { token: string; landing: ChatLanding }) {
  const [name, setName] = useState(() => localStorage.getItem("jbrain_share_name") || "");
  const [otp, setOtp] = useState("");
  const [channel, setChannel] = useState<ChannelKey | null>(null);
  const [myName, setMyName] = useState(landing.guest_name || "");
  const [persist, setPersist] = useState(!!landing.persist);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const secret = useMemo(() => {
    try { return new URLSearchParams(location.hash.slice(1)).get("s") || ""; } catch { return ""; }
  }, []);

  const transport: ChatTransport | null = channel && {
    auth: false,
    streamPath: (after) => chatGuestStreamPath(token, after),
    send: (iv, ct) => chatGuestSend(token, iv, ct),
    uploadFile: (iv, blob) => chatGuestUploadFile(token, iv, blob),
    fetchFile: (id) => chatGuestFetchFile(token, id),
  };

  async function join() {
    setErr("");
    if (!cryptoAvailable()) { setErr("This browser can't do end-to-end encryption here (it needs a secure https connection)."); return; }
    if (!secret) { setErr("This link is incomplete — the secret part after # is missing. Ask the sender to re-copy the whole link."); return; }
    if (landing.requires_claim && !name.trim()) { setErr("Please enter your name."); return; }
    if (landing.otp_required && !otp.trim()) { setErr("Enter the one-time code the sender gave you."); return; }
    setBusy(true);
    try {
      let wrap = landing.guest_wrap;
      let who = myName;
      if (landing.requires_claim) {
        const r = await chatJoin<{ guest_wrap: string; persist: boolean; guest_name: string | null }>(token, name.trim() || undefined);
        wrap = r.guest_wrap; setPersist(!!r.persist);
        who = r.guest_name || name.trim();
        if (name.trim()) localStorage.setItem("jbrain_share_name", name.trim());
      }
      if (!wrap) { setErr("This chat isn't available."); return; }
      const ch = await unwrapKey(wrap, guestPassword(secret, landing.otp_required ? otp : undefined));
      setMyName(who); setChannel(ch);
    } catch (e: any) {
      // unwrap throwing = wrong OTP or tampered/incomplete link (GCM auth failed).
      setErr(landing.otp_required
        ? "That code didn't work. Double-check the one-time code and try again."
        : (e?.status === 403 ? "This chat is already open in another browser." : "Couldn't open this chat — the link may be incomplete."));
    } finally { setBusy(false); }
  }

  if (channel && transport) {
    return (
      <div className="share-page echat-page">
        <EncryptedChat channel={channel} role="guest" meName={myName || "You"} peerName={landing.brain_name}
                       brandName={landing.brain_name} persist={persist} transport={transport} />
      </div>
    );
  }

  return (
    <div className="share-page"><div className="share-card">
      <div className="share-head">
        <span className="brand">{landing.brain_name}<span className="dot">.</span></span>
        <span className="badge echat-lock">🔒 encrypted chat</span>
      </div>
      <h2 style={{ marginTop: 12 }}>You've been invited to a private chat</h2>
      <p className="muted">
        Messages and files are <strong>end-to-end encrypted</strong> — only you and {landing.brain_name} can read them.
        {landing.persist ? "" : " This chat is ephemeral: nothing is stored after it ends."}
      </p>
      {landing.requires_claim && (
        <input placeholder="Your name *" value={name} onChange={(e) => setName(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter" && !landing.otp_required) join(); }} />
      )}
      {landing.otp_required && (
        <input placeholder="One-time code *" value={otp} autoCapitalize="characters" style={{ marginTop: 8, letterSpacing: 1 }}
               onChange={(e) => setOtp(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") join(); }} />
      )}
      {err && <p className="share-err" style={{ color: "var(--danger)", fontSize: 13, marginTop: 8 }}>{err}</p>}
      <div className="row" style={{ marginTop: 12 }}>
        <button className="primary" disabled={busy} onClick={join}>{busy ? "Connecting…" : "Join securely"}</button>
      </div>
      <p className="muted" style={{ fontSize: 12, marginTop: 16 }}>
        The link locks to this browser once you join. If it lands on the wrong one, ask the sender to start a new chat.
      </p>
    </div></div>
  );
}
