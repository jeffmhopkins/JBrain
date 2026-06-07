import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { chatDetail, chatOwnerClose, chatOwnerFetchFile, chatOwnerSave, chatOwnerSend, chatOwnerStreamPath, chatOwnerUploadFile, getAccessKey } from "../api";
import { ChannelKey, cryptoAvailable, ownerPassword, unwrapKey } from "../crypto";
import EncryptedChat, { ChatHandle, ChatTransport } from "../components/EncryptedChat";
import { useAuth } from "../App";

interface ChatDetail {
  link_id: number; token: string; url: string; owner_wrap: string; persist: boolean;
  otp_required: boolean; status: "active" | "closed"; guest_name: string | null;
  saved_note_slug: string | null; expires_at: string | null;
  present: { owner: boolean; guest: boolean };
}

// The owner's side of an encrypted chat. The channel key is recovered from owner_wrap with a
// key derived from this device's access key (the server never sees it). On close the transcript
// is decrypted here and committed to the brain (auto-save), which is the deliberate
// "declassify" step that keeps the channel zero-knowledge while still feeding analysis.
export default function OwnerChatPage() {
  const { linkId = "" } = useParams();
  const id = parseInt(linkId, 10);
  const { brainName } = useAuth();
  const [detail, setDetail] = useState<ChatDetail | null>(null);
  const [channel, setChannel] = useState<ChannelKey | null>(null);
  const [err, setErr] = useState("");
  const [savedSlug, setSavedSlug] = useState<string | null>(null);
  const [closed, setClosed] = useState(false);
  const [saving, setSaving] = useState(false);
  const chatRef = useRef<ChatHandle>(null);
  const savingGuard = useRef(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const d = await chatDetail<ChatDetail>(id);
        if (!alive) return;
        setDetail(d); setSavedSlug(d.saved_note_slug); setClosed(d.status === "closed");
        if (!cryptoAvailable()) { setErr("This browser can't do end-to-end encryption here (it needs a secure https connection)."); return; }
        const key = getAccessKey();
        if (!key) { setErr("No access key on this device."); return; }
        const ch = await unwrapKey(d.owner_wrap, ownerPassword(key));
        if (alive) setChannel(ch);
      } catch (e: any) {
        if (alive) setErr(e?.message || "Couldn't open this chat.");
      }
    })();
    return () => { alive = false; };
  }, [id]);

  const transport: ChatTransport | null = channel && {
    auth: true,
    streamPath: (after) => chatOwnerStreamPath(id, after),
    send: (iv, ct) => chatOwnerSend(id, iv, ct),
    uploadFile: (iv, blob) => chatOwnerUploadFile(id, iv, blob),
    fetchFile: (fid) => chatOwnerFetchFile(id, fid),
  };

  async function doSave() {
    if (savingGuard.current || !chatRef.current) return;
    savingGuard.current = true; setSaving(true);
    try {
      const data = await chatRef.current.getSaveData();
      const r = await chatOwnerSave(id, { transcript_md: data.transcript_md, attachments: data.attachments,
                                          guest_name: detail?.guest_name || undefined });
      setSavedSlug(r.note_slug);
    } catch { /* leave it to a retry/manual save */ savingGuard.current = false; }
    finally { setSaving(false); }
  }

  async function endChat() {
    if (!confirm("End this chat? The recipient is disconnected and the conversation is saved to your brain.")) return;
    try { await chatOwnerClose(id); } catch { /* ignore */ }
    setClosed(true);
    setTimeout(doSave, 600);                 // let the final messages settle, then save
  }

  // Reconcile path: the channel was closed while no owner device was attached (persisted
  // chats only). Once the backlog has streamed in, auto-save it.
  function onClosed() {
    setClosed(true);
    if (!savedSlug) setTimeout(doSave, 1200);
  }

  if (err) return (
    <div className="content"><div className="card"><h3>Couldn't open chat</h3>
      <p className="muted">{err}</p>
      <Link className="ghost" to="/shares">Back to Shares</Link></div></div>
  );
  if (!detail || !channel || !transport) return <div className="content muted">Opening secure channel…</div>;

  const savedBanner = savedSlug && (
    <Link className="badge prio" to={`/note/${savedSlug}`} style={{ textDecoration: "none" }}>Saved → note</Link>
  );

  return (
    <div className="content echat-content">
      <EncryptedChat ref={chatRef} channel={channel} role="owner"
                     meName={brainName} peerName={detail.guest_name || "Guest"}
                     brandName={brainName} persist={detail.persist} initialClosed={closed}
                     transport={transport} onClosed={onClosed}
                     headerExtra={
                       <div className="row" style={{ gap: 6 }}>
                         {savedBanner}
                         {!closed
                           ? <button className="ghost danger-hover" onClick={endChat}>End &amp; save</button>
                           : (!savedSlug && <button className="ghost" disabled={saving} onClick={doSave}>{saving ? "Saving…" : "Save to brain"}</button>)}
                       </div>
                     } />
    </div>
  );
}
