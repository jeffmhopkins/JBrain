// Thin API client. Every request carries the access key (the "cert") as a
// Bearer token; it is stored on-device and pasted in once on first run.
const KEY_STORAGE = "jbrain_access_key";
const SERVER_STORAGE = "jbrain_server";
let accessKey: string | null = localStorage.getItem(KEY_STORAGE);
// Server base URL. Empty = same origin (PWA served by the API itself). Set when
// the PWA is hosted separately (e.g. GitHub Pages) and talks to a remote server.
let serverBase: string = (localStorage.getItem(SERVER_STORAGE) || "").replace(/\/+$/, "");

export function setAccessKey(key: string) {
  accessKey = key;
  localStorage.setItem(KEY_STORAGE, key);
}
export function getAccessKey(): string | null {
  return accessKey;
}
export function setServer(url: string) {
  serverBase = (url || "").trim().replace(/\/+$/, "");
  localStorage.setItem(SERVER_STORAGE, serverBase);
}
export function getServer(): string {
  return serverBase;
}
export function clearAccessKey() {
  accessKey = null;
  localStorage.removeItem(KEY_STORAGE);
}

// Resolve an API path against the configured server (or same origin).
export function u(path: string): string {
  return serverBase + path;
}

function authHeaders(extra: HeadersInit = {}): HeadersInit {
  const h: Record<string, string> = { "Content-Type": "application/json", ...(extra as any) };
  if (accessKey) h["Authorization"] = `Bearer ${accessKey}`;
  return h;
}

export async function api<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(u(path), {
    ...opts,
    headers: authHeaders(opts.headers),
  });
  if (res.status === 401) throw new ApiError("Not authenticated", 401);
  if (!res.ok) {
    let detail: any = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* ignore */ }
    // FastAPI/Pydantic validation errors return `detail` as an array of objects —
    // turn those into a readable string instead of "[object Object]".
    if (Array.isArray(detail)) detail = detail.map((d: any) => d?.msg || JSON.stringify(d)).join("; ");
    else if (detail && typeof detail === "object") detail = JSON.stringify(detail);
    throw new ApiError(String(detail), res.status);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export const get = <T = any>(p: string) => api<T>(p);
export const post = <T = any>(p: string, body?: unknown) =>
  api<T>(p, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
export const put = <T = any>(p: string, body?: unknown) =>
  api<T>(p, { method: "PUT", body: body === undefined ? undefined : JSON.stringify(body) });
export const del = <T = any>(p: string) => api<T>(p, { method: "DELETE" });

// Wiki-link label audit: links whose shortened [[Target|Display]] label names a different
// article than the target (e.g. a stale alias left behind by a rename). Fix = correct the
// label to the target's bare name (deterministic, undoable).
export interface LinkAuditFinding {
  source_id: number; source_title: string; source_slug: string;
  raw: string; target: string; target_title: string; target_slug: string;
  display: string; desired_display: string; fixed: string;
  resolved_title: string | null; resolved_slug: string | null; reason: string;
}
export const auditLinks = () => get<{ findings: LinkAuditFinding[] }>("/api/notes/links/audit");
// Fire an allow-listed UI event (e.g. "wiki_viewed") so subscribed event-workflows run.
// Server-debounced; fire-and-forget from the client.
export const fireEvent = (name: string) => post<{ fired: boolean }>(`/api/events/${name}`);
export const fixLinkLabel = (note_id: number, target: string, display: string) =>
  post<{ fixed: boolean }>("/api/notes/links/audit/fix", { note_id, target, display });
export const fixAllLinkLabels = () => post<{ fixed: number }>("/api/notes/links/audit/fix-all");

// Calendar (temporal projection) — read + the note-writing edit paths.
export interface CalEvent {
  id: number;
  title: string;
  kind: string;
  starts_at: string | null;
  ends_at?: string | null;
  all_day: number;
  status?: string;
  location_label?: string | null;
  note_id?: number;
  note_title?: string;
  note_slug?: string | null;
  recurring?: boolean;
}
export const calUpcoming = (withinDays = 90) =>
  get<CalEvent[]>(`/api/calendar/upcoming?within_days=${withinDays}`);
export const calHistory = () => get<CalEvent[]>("/api/calendar/history");
export const calRange = (start: string, end: string) =>
  get<CalEvent[]>(`/api/calendar/range?start=${start}&end=${end}`);
export interface Reminder { offset_minutes: number; anchor?: string }
export interface AddedEvent {
  id: number; identity_key: string; title: string; kind: string;
  starts_at: string | null; all_day: number; source: string; note_slug: string | null; note_title?: string;
}
export const calQuickAdd = (b: { title: string; date: string; time?: string; kind?: string; detail?: string; reminders?: Reminder[] }) =>
  post<{ note_id: number; note_title: string; event: CalEvent | null }>("/api/calendar/quick-add", b);
export const calGetReminders = (id: number) => get<{ reminders: Reminder[] }>(`/api/calendar/events/${id}/reminders`);
export const calSetReminders = (id: number, reminders: Reminder[]) =>
  post<{ count: number }>(`/api/calendar/events/${id}/reminders`, { reminders });
export const calRecentlyAdded = () => get<{ since: string; events: AddedEvent[] }>("/api/calendar/recently-added");
export const calMarkReviewed = () => post("/api/calendar/reviewed", {});
export const calDismiss = (id: number) => post<{ identity_key: string }>(`/api/calendar/events/${id}/dismiss`, {});
export const calUndismiss = (identity_key: string) => post("/api/calendar/undismiss", { identity_key });

// Public, UNAUTHENTICATED share endpoints — no bearer key (a recipient has none).
// Uses default same-origin credentials so the bind cookie rides along (recipients
// always open the canonical JBRAIN_DOMAIN share URL = same origin as the API). Do
// NOT switch to credentials:"include" without also enabling credentialed CORS with
// an explicit origin list — `*` + credentials is spec-incompatible and would brick bind.
async function publicApi<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(u(path), { ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } });
  if (!res.ok) {
    let detail: any = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* ignore */ }
    // FastAPI/Pydantic validation errors return `detail` as an array of objects —
    // turn those into a readable string instead of "[object Object]".
    if (Array.isArray(detail)) detail = detail.map((d: any) => d?.msg || JSON.stringify(d)).join("; ");
    else if (detail && typeof detail === "object") detail = JSON.stringify(detail);
    throw new ApiError(String(detail), res.status);
  }
  return res.json();
}
export const getShare = <T = any>(token: string) => publicApi<T>(`/api/share/${encodeURIComponent(token)}`);
export const claimShare = <T = any>(token: string, name?: string) =>
  publicApi<T>(`/api/share/${encodeURIComponent(token)}/claim`, { method: "POST", body: JSON.stringify({ name }) });
export const proposeShareEdit = (token: string, content_md: string, name?: string, note?: string) =>
  publicApi(`/api/share/${encodeURIComponent(token)}/propose`, { method: "POST", body: JSON.stringify({ content_md, name, note }) });
export const shareAttachmentUrl = (token: string, id: number) =>
  u(`/api/share/${encodeURIComponent(token)}/attachments/${id}`);

// Guided AI intake — recipient side (public; the session rides on a same-origin cookie).
export const guidedStart = <T = any>(token: string, name?: string) =>
  publicApi<T>(`/api/share/${encodeURIComponent(token)}/guided/start`, { method: "POST", body: JSON.stringify({ name }) });
export const guidedTurn = <T = any>(token: string, message: string) =>
  publicApi<T>(`/api/share/${encodeURIComponent(token)}/guided/turn`, { method: "POST", body: JSON.stringify({ message }) });
export const guidedSubmit = <T = any>(token: string) =>
  publicApi<T>(`/api/share/${encodeURIComponent(token)}/guided/submit`, { method: "POST" });
// Guided — owner side (authenticated approvals).
export const guidedActivate = (linkId: number) => post(`/api/shares/guided/${linkId}/activate`);
export const guidedAccept = (sid: number) => post(`/api/shares/guided/sessions/${sid}/accept`);
export const guidedReject = (sid: number) => post(`/api/shares/guided/sessions/${sid}/reject`);
export const guidedOptions = (linkId: number, bind: boolean, single_use: boolean) =>
  post(`/api/shares/guided/${linkId}/options`, { bind, single_use });
export const guidedResetBind = (linkId: number) => post(`/api/shares/guided/${linkId}/reset-bind`);
export const guidedReopen = (sid: number) => post(`/api/shares/guided/sessions/${sid}/reopen`);
export const guidedAcknowledge = (sid: number) => post(`/api/shares/guided/sessions/${sid}/acknowledge`);
// All recipient sessions for a guided link (incl. in-progress/abandoned), and one transcript.
export const guidedSessions = <T = any>(linkId: number) => get<T>(`/api/shares/guided/${linkId}/sessions`);
export const guidedSession = <T = any>(linkId: number, sid: number) =>
  get<T>(`/api/shares/guided/${linkId}/sessions/${sid}`);
export const guidedDeleteSession = (sid: number) => del(`/api/shares/guided/sessions/${sid}`);
export const researchDeleteSession = (linkId: number, sid: number) =>
  del(`/api/shares/research/${linkId}/sessions/${sid}`);

// Research links — public (recipient) Q&A.
export const researchStart = <T = any>(token: string, name?: string) =>
  publicApi<T>(`/api/share/${encodeURIComponent(token)}/research/start`, { method: "POST", body: JSON.stringify({ name }) });
export const researchTurn = <T = any>(token: string, message: string) =>
  publicApi<T>(`/api/share/${encodeURIComponent(token)}/research/turn`, { method: "POST", body: JSON.stringify({ message }) });
// Lab-share links (kind='labs') — recipient side. Series come ONLY through the token-scoped,
// allow-list-rechecked, no-store endpoint (never the owner /api/medical/* path).
export const labsStart = <T = any>(token: string, name?: string) =>
  publicApi<T>(`/api/share/${encodeURIComponent(token)}/labs/start`, { method: "POST", body: JSON.stringify({ name }) });
export const labsTurn = <T = any>(token: string, message: string) =>
  publicApi<T>(`/api/share/${encodeURIComponent(token)}/labs/turn`, { method: "POST", body: JSON.stringify({ message }) });
export const getShareLabSeries = (token: string, analyte: string) =>
  publicApi<LabSeries>(`/api/share/${encodeURIComponent(token)}/labs/series?analyte=${encodeURIComponent(analyte)}`);
// Assisted (research) share with attached labs — the AI surfaces a chart on demand; the recipient
// fetches its series through this token-scoped, allow-list-rechecked, no-store endpoint.
export const getResearchLabSeries = (token: string, analyte: string) =>
  publicApi<LabSeries>(`/api/share/${encodeURIComponent(token)}/research/labs/series?analyte=${encodeURIComponent(analyte)}`);
// Lab-share — owner management.
export const createLabShare = (analytes: string[], opts: { window_from?: string | null; window_to?: string | null;
    allow_chat?: boolean; intro?: string; label?: string;
    bind?: boolean; single_use?: boolean; ttl_days?: number; max_turns?: number; max_total_replies?: number }) =>
  post<{ token: string; link_id: number; url: string }>("/api/shares/labs", { analytes, ...opts });
// Research links — owner management.
export const researchDetail = <T = any>(linkId: number) => get<T>(`/api/shares/research/${linkId}`);
export const researchSetScope = (linkId: number, prefixes: string[], kinds: string[] = []) =>
  post(`/api/shares/research/${linkId}/scope`, { prefixes, kinds });
// Attach/adjust a scoped LABS allow-list (+ optional date window) on an assisted link. Empty = detach.
export const researchSetLabScope = (linkId: number,
    body: { analytes: string[]; window_from?: string | null; window_to?: string | null }) =>
  post(`/api/shares/research/${linkId}/labs`, body);
export const researchSetDetails = (linkId: number, body: any) => post(`/api/shares/research/${linkId}/details`, body);
export const researchApprove = (linkId: number, ids: number[]) => post(`/api/shares/research/${linkId}/approve`, { ids });
export const researchDismiss = (linkId: number, ids: number[]) => post(`/api/shares/research/${linkId}/dismiss`, { ids });
export const researchRemove = (linkId: number, ids: number[]) => post(`/api/shares/research/${linkId}/remove`, { ids });
export const researchActivate = (linkId: number) => post(`/api/shares/research/${linkId}/activate`);
export const researchResetBind = (linkId: number) => post(`/api/shares/research/${linkId}/reset-bind`);
export const researchSession = <T = any>(linkId: number, sid: number) =>
  get<T>(`/api/shares/research/${linkId}/sessions/${sid}`);
export const guidedSetDetails = (linkId: number, body: Record<string, unknown>) =>
  post(`/api/shares/guided/${linkId}/details`, body);
export const setLinkExpiry = (linkId: number, ttl_days: number) =>
  post(`/api/shares/${linkId}/expiry`, { ttl_days });

// --- Encrypted chat (kind='chat') -------------------------------------------
// The server is a blind relay: only opaque ciphertext + wrapped keys cross it. Encryption
// lives entirely in web/src/crypto.ts; these helpers just move sealed bytes around.
export interface ChatStreamEvent {
  type: "message" | "presence" | "closed";
  seq?: number; sender?: "owner" | "guest"; iv?: string; ct?: string; at?: string;
  owner?: boolean; guest?: boolean;
}

// Open a long-lived SSE read. `auth` picks the owner (bearer) vs recipient (cookie) channel.
// Returns close(); the caller handles reconnection (re-open with a higher ?after=).
export function openChatStream(path: string, auth: boolean, onEvent: (e: ChatStreamEvent) => void): { close: () => void; done: Promise<void> } {
  const ctrl = new AbortController();
  const done = (async () => {
    try {
      const res = await fetch(u(path), {
        headers: auth ? authHeaders() : {},
        credentials: auth ? "same-origin" : "include",   // recipient: carry the bind cookie
        signal: ctrl.signal,
      });
      if (!res.body) return;
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        let r: ReadableStreamReadResult<Uint8Array>;
        try { r = await reader.read(); } catch { break; }
        if (r.done) break;
        buf += dec.decode(r.value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop() ?? "";
        for (const chunk of parts) {
          const dl = chunk.split("\n").find((l) => l.startsWith("data: "));
          if (!dl) continue;
          try { onEvent(JSON.parse(dl.slice(6)) as ChatStreamEvent); } catch { /* ignore */ }
        }
      }
    } catch { /* aborted / network drop — caller reconnects */ }
  })();
  return { close: () => ctrl.abort(), done };
}

// Owner side (authenticated).
export const createChatShare = (body: { owner_wrap: string; guest_wrap: string; persist: boolean;
    otp_required: boolean; label?: string | null; owner_name?: string | null;
    ttl_days?: number | null; single_use?: boolean }) =>
  post<{ token: string; link_id: number; url: string }>("/api/shares/chat", body);
export const chatDetail = <T = any>(linkId: number) => get<T>(`/api/shares/chat/${linkId}`);
export const chatOwnerSend = (linkId: number, iv: string, ct: string) =>
  post<{ ok: boolean; seq: number }>(`/api/shares/chat/${linkId}/send`, { iv, ct });
export const chatOwnerClose = (linkId: number) => post(`/api/shares/chat/${linkId}/close`);
export const chatOwnerSave = (linkId: number, body: { transcript_md: string; title?: string | null;
    guest_name?: string | null; attachments: { name: string; mime: string; data: string }[] }) =>
  post<{ ok: boolean; note_slug: string; already_saved: boolean }>(`/api/shares/chat/${linkId}/save`, body);
export const chatOwnerStreamPath = (linkId: number, after = 0) => `/api/shares/chat/${linkId}/stream?after=${after}`;
export async function chatOwnerUploadFile(linkId: number, iv: string, blob: Blob): Promise<number> {
  const fd = new FormData();
  fd.append("iv", iv); fd.append("file", blob, "blob");
  // Multipart: send only Authorization — the browser sets Content-Type + boundary itself.
  const headers: Record<string, string> = {};
  if (accessKey) headers["Authorization"] = `Bearer ${accessKey}`;
  const res = await fetch(u(`/api/shares/chat/${linkId}/file`), { method: "POST", headers, body: fd });
  if (!res.ok) throw new ApiError("Upload failed", res.status);
  return (await res.json()).file_id;
}
export async function chatOwnerFetchFile(linkId: number, fileId: number): Promise<{ iv: string; buf: ArrayBuffer }> {
  const res = await fetch(u(`/api/shares/chat/${linkId}/file/${fileId}`), { headers: authHeaders() });
  if (!res.ok) throw new ApiError("Fetch failed", res.status);
  return { iv: res.headers.get("X-Chat-IV") || "", buf: await res.arrayBuffer() };
}

// Recipient side (public; cookie-bound after join).
export const chatJoin = <T = any>(token: string, name?: string) =>
  publicApi<T>(`/api/share/${encodeURIComponent(token)}/chat/join`, { method: "POST", body: JSON.stringify({ name }) });
export const chatGuestSend = (token: string, iv: string, ct: string) =>
  publicApi<{ ok: boolean; seq: number }>(`/api/share/${encodeURIComponent(token)}/chat/send`, { method: "POST", body: JSON.stringify({ iv, ct }) });
export const chatGuestStreamPath = (token: string, after = 0) =>
  `/api/share/${encodeURIComponent(token)}/chat/stream?after=${after}`;
export async function chatGuestUploadFile(token: string, iv: string, blob: Blob): Promise<number> {
  const fd = new FormData();
  fd.append("iv", iv); fd.append("file", blob, "blob");
  const res = await fetch(u(`/api/share/${encodeURIComponent(token)}/chat/file`), { method: "POST", credentials: "include", body: fd });
  if (!res.ok) throw new ApiError("Upload failed", res.status);
  return (await res.json()).file_id;
}
export async function chatGuestFetchFile(token: string, fileId: number): Promise<{ iv: string; buf: ArrayBuffer }> {
  const res = await fetch(u(`/api/share/${encodeURIComponent(token)}/chat/file/${fileId}`), { credentials: "include" });
  if (!res.ok) throw new ApiError("Fetch failed", res.status);
  return { iv: res.headers.get("X-Chat-IV") || "", buf: await res.arrayBuffer() };
}

// Multipart upload: must NOT set Content-Type (browser sets the boundary), so
// we call fetch directly with only the Authorization header.
export const MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024;

// XHR (not fetch) so we get real upload progress. onProgress reports 0–100 for
// bytes sent; the server then extracts text/embeds before responding, so callers
// can show a "processing" phase once it hits 100.
export function uploadAttachment<T = any>(
  slug: string, file: File, onProgress?: (pct: number) => void, analyze = true,
): Promise<T> {
  if (file.size > MAX_ATTACHMENT_BYTES) return Promise.reject(new ApiError("File too large (100 MB max).", 413));
  return new Promise<T>((resolve, reject) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("analyze", analyze ? "true" : "false");   // images auto-analyze unless opted out
    const xhr = new XMLHttpRequest();
    xhr.open("POST", u(`/api/notes/${encodeURIComponent(slug)}/attachments`));
    if (accessKey) xhr.setRequestHeader("Authorization", `Bearer ${accessKey}`);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress?.(Math.round((e.loaded / e.total) * 100)); };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(100);
        try { resolve(JSON.parse(xhr.responseText)); } catch { resolve({} as T); }
      } else {
        let detail = xhr.statusText;
        try { detail = JSON.parse(xhr.responseText).detail ?? detail; } catch { /* ignore */ }
        reject(new ApiError(detail, xhr.status));
      }
    };
    xhr.onerror = () => reject(new ApiError("Network error during upload", 0));
    xhr.send(fd);
  });
}

// AI image analysis: kick off (or re-run) and poll status.
export interface AnalysisStatus { status: "none" | "pending" | "done" | "error"; detail?: string | null; analyzed_at?: string | null; }
export const analyzeAttachment = (id: number, force = false) =>
  post<AnalysisStatus>(`/api/attachments/${id}/analyze`, { force });
// Local speech-to-text for audio attachments (no API key). Shares the analysis_* status
// columns, so getAnalysisStatus polls it the same way images are polled.
export const transcribeAttachment = (id: number, force = false) =>
  post<AnalysisStatus>(`/api/attachments/${id}/transcribe`, { force });
export const getAnalysisStatus = (id: number) =>
  get<AnalysisStatus>(`/api/attachments/${id}/analysis-status`);

// A tiny title + excerpt for a note, to preview a [[citation]] on hover in chat.
export interface NotePreview { found: boolean; title?: string; excerpt?: string; }
export const getNotePreview = (slug: string) =>
  get<NotePreview>(`/api/notes/${encodeURIComponent(slug)}/preview`);

// Force-recompute one note's AI analysis sidecar (ignores the content-hash cache).
export const refreshNoteAnalysis = (slug: string) =>
  post<any>(`/api/notes/${encodeURIComponent(slug)}/analysis`);

// --- Article talk (KB maintenance conversation) -----------------------------
// Reply to a talk item; dismiss one (owner judgement — refused on 'correction'); or run the
// surgical maintenance pass on this article on demand (consumes open items + replies now).
export const talkReply = (slug: string, talkId: number, body: string) =>
  post<{ id: number }>(`/api/notes/${encodeURIComponent(slug)}/talk/${talkId}/reply`, { body });
export const talkDismiss = (slug: string, talkId: number, reason = "") =>
  post<{ ok: boolean }>(`/api/notes/${encodeURIComponent(slug)}/talk/${talkId}/dismiss`, { reason });
export const talkMaintainNow = (slug: string) =>
  post<{ ok: boolean; title: string; changed?: boolean; resolved?: number;
         examined?: number; kept_open?: number; reason?: string }>(
    `/api/notes/${encodeURIComponent(slug)}/talk/maintain-now`);

// Attachments need the auth header, so a plain <a>/<img> won't work — fetch+blob.
async function attachmentBlob(id: number): Promise<Blob> {
  const headers: Record<string, string> = {};
  if (accessKey) headers["Authorization"] = `Bearer ${accessKey}`;
  // cache:no-store — never serve a possibly-stale/partial cached copy of the binary.
  const res = await fetch(u(`/api/attachments/${id}/download`), { headers, cache: "no-store" });
  if (!res.ok) throw new ApiError("Failed to load attachment", res.status);
  return res.blob();
}

export async function attachmentObjectUrl(id: number): Promise<string> {
  return URL.createObjectURL(await attachmentBlob(id));
}

// A short-lived signed, SAME-ORIGIN URL an <img>/<audio>/<video> can load directly —
// no blob:, no Authorization header, so no `media-src blob:` CSP dependency and real
// range-streaming/seeking. Authed mint, then the element streams it on its own.
export async function attachmentMediaUrl(id: number): Promise<string> {
  const r = await get<{ url: string }>(`/api/attachments/${id}/media-url`);
  return u(r.url);
}

// Like attachmentObjectUrl but verifies the bytes are actually an image. If the
// server returns 200 with a non-image body (e.g. an older backend whose SPA
// fallback serves index.html for /api/attachments/.../download), surface a clear
// reason instead of a silently broken <img>.
export async function attachmentImageUrl(id: number, expectedBytes?: number): Promise<string> {
  const blob = await attachmentBlob(id);
  if (!blob.type.startsWith("image/")) {
    throw new Error(
      blob.type.includes("html") || blob.type === ""
        ? "server returned a page, not the image — the backend is likely out of date (rebuild it)"
        : `unexpected response type “${blob.type}”`,
    );
  }
  // Sniff the magic bytes so a non-image / corrupted body gives a precise reason —
  // the leading bytes pinpoint the cause (e.g. "3c 21" = "<!" = an HTML page).
  const head = new Uint8Array(await blob.slice(0, 12).arrayBuffer());
  const hex = Array.from(head, (b) => b.toString(16).padStart(2, "0")).join(" ");
  const sig =
    head[0] === 0xff && head[1] === 0xd8 && head[2] === 0xff ? "jpeg" :
    head[0] === 0x89 && head[1] === 0x50 && head[2] === 0x4e && head[3] === 0x47 ? "png" :
    head[0] === 0x47 && head[1] === 0x49 && head[2] === 0x46 ? "gif" :
    head[0] === 0x52 && head[1] === 0x49 && head[2] === 0x46 && head[3] === 0x46 ? "webp" : null;
  if (!sig) {
    throw new Error(`got ${blob.size} bytes (type ${blob.type || "?"}) but it isn't a known image — starts with: ${hex}`);
  }
  // Body far smaller than the known file size ⇒ truncated/missing bytes on the server.
  if (expectedBytes && expectedBytes > 4096 && blob.size < expectedBytes * 0.5) {
    throw new Error(`server sent ${blob.size} of ~${expectedBytes} bytes — the image data looks truncated/missing on the server`);
  }
  // Return a data: URL, not a blob: URL: the app's CSP allows `img-src data:` but
  // not `blob:`, so a blob URL would be silently blocked by the browser. data:
  // also needs no revoke (GC'd normally).
  return await blobToDataUrl(blob);
}

function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(fr.result as string);
    fr.onerror = () => reject(new Error("couldn't read the image data"));
    fr.readAsDataURL(blob);
  });
}

export async function downloadAttachment(id: number, filename: string): Promise<void> {
  const url = URL.createObjectURL(await attachmentBlob(id));
  const a = document.createElement("a");
  a.href = url; a.download = filename || "file";
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

// Download a full DB backup (auth header can't ride on a plain <a>, so fetch+blob).
export async function downloadBackup(): Promise<void> {
  const headers: Record<string, string> = {};
  if (accessKey) headers["Authorization"] = `Bearer ${accessKey}`;
  const res = await fetch(u("/api/system/backup"), { headers });
  if (!res.ok) throw new ApiError("Backup failed", res.status);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = res.headers.get("Content-Disposition")?.match(/filename="?([^"]+)"?/)?.[1]
    || "jbrain-backup.db";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// Download just the original user-uploaded note content (first user-authored version of
// each note), as a JSON file — same auth+blob dance as the DB backup.
export async function downloadOriginalNotes(): Promise<void> {
  const headers: Record<string, string> = {};
  if (accessKey) headers["Authorization"] = `Bearer ${accessKey}`;
  const res = await fetch(u("/api/system/export/original-notes"), { headers });
  if (!res.ok) throw new ApiError("Export failed", res.status);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = res.headers.get("Content-Disposition")?.match(/filename="?([^"]+)"?/)?.[1]
    || "jbrain-original-notes.json";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export async function restoreBackup<T = any>(file: File): Promise<T> {
  const fd = new FormData();
  fd.append("file", file);
  const headers: Record<string, string> = {};
  if (accessKey) headers["Authorization"] = `Bearer ${accessKey}`;
  const res = await fetch(u("/api/system/restore"), { method: "POST", headers, body: fd });
  if (!res.ok) {
    let detail: any = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* ignore */ }
    // FastAPI/Pydantic validation errors return `detail` as an array of objects —
    // turn those into a readable string instead of "[object Object]".
    if (Array.isArray(detail)) detail = detail.map((d: any) => d?.msg || JSON.stringify(d)).join("; ");
    else if (detail && typeof detail === "object") detail = JSON.stringify(detail);
    throw new ApiError(String(detail), res.status);
  }
  return res.json();
}

export interface ChatEvent {
  // "replace_text" carries the server-verified final reply (dead [[links]] / unsourced URLs removed) —
  // the client swaps the streamed text for it so a fabricated link is never left on screen.
  // "external_proposal" = the assistant wants to send a term to an external service (MedlinePlus);
  // nothing is sent until the owner approves the exact term via the inline confirm chip.
  type: "token" | "tool" | "staging" | "applied" | "chart" | "replace_text" | "external_proposal" | "done" | "error";
  chart?: { analyte: string; unit?: string | null; from?: string | null; to?: string | null; title?: string };
  text?: string;
  tool?: string;
  term?: string;            // external_proposal: the exact term that would be sent out
  id?: number;              // external_proposal: the lookup id to approve/deny
  actions?: any[];
  action?: { id: number; summary: string };
  message?: string;
}

// A persisted chat message as returned by GET /chat/conversations/{id}/messages.
// `step_count` (0 for non-AI / no-tool replies) drives the reply's tool-call history pill;
// `id` lets the client lazily fetch that reply's full step log.
export interface ChatMessage { id: number; role: "user" | "assistant" | "event"; content: string; step_count?: number; }

// One tool call + its raw result from an assistant reply's history. `event_json` is the
// staging/applied/chart event the call emitted (if any); `result_text` is the verbatim,
// untrusted tool output — render it as PLAIN text, never as markdown/HTML.
export interface MessageStep {
  step_index: number;
  tool_name: string;
  args_json: string;
  result_text: string;
  is_error: number;
  event_json?: string | null;
  created_at?: string;
}
export const getMessageSteps = (messageId: number) =>
  get<MessageStep[]>(`/api/chat/messages/${messageId}/steps`);
// Wipe a conversation's stored tool-call history (invoked by /clear).
export const clearConversationSteps = (conversationId: number) =>
  del<{ ok: boolean }>(`/api/chat/conversations/${conversationId}/steps`);

// External-lookup approval (the medical_reference gate): nothing is sent externally until approved.
// `term` lets the owner EDIT what's sent (e.g. trim a PHI-laden question down to a clean topic).
export const approveExternalLookup = (id: number, term?: string) =>
  post<{ ok: boolean; found: boolean; term: string }>(`/api/external-lookups/${id}/approve`, term ? { term } : undefined);
export const denyExternalLookup = (id: number) => post<{ ok: boolean }>(`/api/external-lookups/${id}/deny`);

// Stream the architect's reply over SSE (POST + ReadableStream, so we can send
// a body and rely on the session cookie).
// Named geofences ("places") — drive the location tools + triggers.
export interface Place { id: number; name: string; lat: number; lon: number; radius_m: number; note_slug: string | null; }
export const getPlaces = () => get<Place[]>("/api/places");
export const addPlace = (p: { name: string; lat: number; lon: number; radius_m?: number }) =>
  post<{ id: number; name: string }>("/api/places", p);
export const updatePlace = (id: number, body: { name?: string; radius_m?: number }) =>
  api(`/api/places/${id}`, { method: "PATCH", body: JSON.stringify(body) });
export const ensurePlaceNote = (id: number) => post<{ slug: string }>(`/api/places/${id}/note`);
export const deletePlace = (id: number) => del(`/api/places/${id}`);

export interface Person { id: number; name: string; color: string; is_default: boolean; aliases: string; note_slug: string | null; location_key: string | null; }
export const getPeople = () => get<Person[]>("/api/people");
export const generateLocationKey = (id: number) => post<{ location_key: string }>(`/api/people/${id}/location-key`, {});
export const revokeLocationKey = (id: number) => del(`/api/people/${id}/location-key`);
export const addPerson = (p: { name: string; color?: string; aliases?: string; is_default?: boolean }) =>
  post<{ id: number; name: string }>("/api/people", p);
export const updatePerson = (id: number, body: Partial<{ name: string; color: string; aliases: string; note_slug: string | null; is_default: boolean }>) =>
  api(`/api/people/${id}`, { method: "PATCH", body: JSON.stringify(body) });
export const deletePerson = (id: number) => del(`/api/people/${id}`);
export const personFromNote = (slug: string) => post<{ id: number; name: string }>("/api/people/from-note", { slug });

// The brain owner = the default person. Their name anchors first-person notes and titles
// their kb/People/<name> page. is_set is false while it's still the unnamed default.
export interface Owner { id: number | null; name: string; display: string; aliases?: string; is_set: boolean; }
export const getOwner = () => get<Owner>("/api/people/owner");
export const setOwner = (name: string, aliases?: string) =>
  put<Owner>("/api/people/owner", aliases === undefined ? { name } : { name, aliases });

export interface MediaSettings {
  audio_model: string; audio_compute_type: string;
  video_frame_interval: string; video_frame_max: number;
  audio_model_options: string[]; compute_type_options: string[];
}
export const getMediaSettings = () => get<MediaSettings>("/api/system/settings/media");
export const setMediaSettings = (s: Partial<MediaSettings>) =>
  put<MediaSettings>("/api/system/settings/media", s);

export interface AutoAnalyzeSettings { enabled: boolean }
export const getAutoAnalyze = () => get<AutoAnalyzeSettings>("/api/system/settings/auto-analyze");
export const setAutoAnalyze = (enabled: boolean) =>
  put<AutoAnalyzeSettings>("/api/system/settings/auto-analyze", { enabled });

// Notes that carry a capture coordinate — the Map's note pins.
export interface LocatedNote { slug: string; title: string; lat: number; lon: number; location_label: string | null; kind: string; created_at: string; }
export const getLocatedNotes = (since?: string, until?: string) => {
  const p = new URLSearchParams();
  if (since) p.set("since", since);
  if (until) p.set("until", until);
  const qs = p.toString();
  return get<LocatedNote[]>(`/api/notes/located${qs ? `?${qs}` : ""}`);
};

export interface LocPoint { id: number; lat: number; lon: number; accuracy_m: number | null; recorded_at: string; source?: string | null; }
export const getLocations = (since?: string, until?: string) => {
  const p = new URLSearchParams();
  if (since) p.set("since", since);
  if (until) p.set("until", until);
  const qs = p.toString();
  return get<LocPoint[]>(`/api/locations${qs ? `?${qs}` : ""}`);
};

// (The PWA no longer logs a continuous trail — the native tracker owns that. Posts
// are still location-STAMPED via useGeo's coords at capture time.)

// ---- Labs (trend chart) ----------------------------------------------------
export interface LabAnalyte {
  analyte: string; test_name: string; unit: string | null;
  n: number; first_at: string; last_at: string; last_value: string | null; last_status: string;
}
export interface LabPoint {
  t: string; time?: string | null; source?: string | null;
  v: number | null; vtext: string; unit: string | null; status: string; censored: boolean;
  ref_low: number | null; ref_high: number | null; ref_text: string | null; flag: string | null;
  note_id: number | null; note_slug: string | null; note_title: string | null; encounter_id: number | null;
}
export interface LabSegment { from: string; to: string; low: number | null; high: number | null; }
export interface LabEncounter { id: number; kind: string; label: string; from: string; to: string | null; }
export interface LabSeries {
  analyte: string; test_name: string; unit: string | null;
  points: LabPoint[]; segments: LabSegment[];
  domain: { from: string; to: string } | null; value_range: { min: number; max: number } | null;
  encounters: LabEncounter[]; other_units: string[];
}
export const getLabAnalytes = () => get<{ analytes: LabAnalyte[] }>("/api/medical/labs/analytes");
export const getLabSeries = (analyte: string, unit?: string) =>
  get<LabSeries>(`/api/medical/labs/series?analyte=${encodeURIComponent(analyte)}` +
    (unit ? `&unit=${encodeURIComponent(unit)}` : ""));

// Staged lab ingestion (extract -> preview -> approve/revoke/re-analyze, per attachment).
export interface LabSkip { analyte: string; date: string | null; value: string; reason: string; }
export interface LabIdentity { name?: string; dob?: string; }
export interface StagedLab {
  status: string | null; imported?: number; doc_type?: string; results: LabPoint[] & any[];
  skipped?: number; skips?: LabSkip[]; low_confidence?: number;
  identity?: LabIdentity; identity_state?: "" | "mismatch" | "unverified";
}
export interface MedicalOwner { name?: string; dob?: string; }
export const getMedicalOwner = () => get<MedicalOwner>("/api/medical/owner");
export const setMedicalOwner = (name: string, dob: string) =>
  put<MedicalOwner>("/api/medical/owner", { name, dob });
export const getAttachmentLabs = (id: number) => get<StagedLab>(`/api/medical/attachments/${id}/labs`);
export const getAttachmentLabSeries = (id: number, analyte: string, unit?: string) =>
  get<LabSeries>(`/api/medical/attachments/${id}/labs/series?analyte=${encodeURIComponent(analyte)}` +
    (unit ? `&unit=${encodeURIComponent(unit)}` : ""));
export const approveLabs = (id: number) => post<{ approved: number }>(`/api/medical/attachments/${id}/labs/approve`);
export const revokeLabs = (id: number) => post<{ removed: number }>(`/api/medical/attachments/${id}/labs/revoke`);
export const reanalyzeLabs = (id: number) => post<{ status: string; n: number; analytes: number }>(`/api/medical/attachments/${id}/labs/reanalyze`);
export const getPendingLabs = () =>
  get<{ pending: { attachment_id: number; note_id: number; note_slug: string; note_title: string }[] }>("/api/medical/labs/pending");

export const createEntry = <T = any>(
  text: string, title?: string, loc?: { lat: number; lon: number } | null, dest?: string, destRoot?: string,
) =>
  post<T>("/api/notes/entry", { text, title: title || undefined, dest: dest || undefined, root: destRoot || undefined, lat: loc?.lat, lon: loc?.lon });

// Entry sub-selector capture destinations (the picklists the Medical / Financial sub-types
// offer; entries file under notes/<root>/<dest>/NN).
export const getMedicalDests = () => get<{ names: string[] }>("/api/medical/destinations");
export const setMedicalDests = (names: string[]) =>
  put<{ names: string[] }>("/api/medical/destinations", { names });
export const getFinancialDests = () => get<{ names: string[] }>("/api/financial/destinations");
export const setFinancialDests = (names: string[]) =>
  put<{ names: string[] }>("/api/financial/destinations", { names });
// Parse any lab-result PDF(s) on a note into the lab_results table (deterministic, no LLM).
export const extractLabs = (slug: string) =>
  post<{ doc_type: string; staged: number; skipped: number; analytes: number }>(
    `/api/medical/notes/${encodeURIComponent(slug)}/extract-labs`);

export async function streamChat(
  conversationId: number,
  text: string,
  onEvent: (e: ChatEvent) => void,
  location?: { lat: number; lon: number } | null,
  mode: "assisted" | "research" = "assisted",
  freshContext = false,
  deep = false,
): Promise<void> {
  const body: any = { text, mode };
  if (freshContext) body.fresh_context = true;   // user left/returned/changed topic → re-ground fresh
  if (deep) body.deep = true;   // read-only "Deep" opt-in: bigger budget, same strict posture
  if (location) { body.lat = location.lat; body.lon = location.lon; }
  const ctrl = new AbortController();
  const res = await fetch(u(`/api/chat/conversations/${conversationId}/message`), {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify(body),
    signal: ctrl.signal,
  });
  if (!res.body) throw new ApiError("No response stream", 500);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  // Stall watchdog: if the stream goes fully silent past this window (a stuck/half-open
  // connection that never closes), abort so streamChat resolves and the caller re-syncs
  // from the server (which already saved the full reply). Generous so a long tool run
  // never trips it; reset on every byte received.
  let idle: number | undefined;
  const STALL_MS = 90000;
  const arm = () => { if (idle) clearTimeout(idle); idle = window.setTimeout(() => ctrl.abort(), STALL_MS); };
  arm();
  try {
    for (;;) {
      let r: ReadableStreamReadResult<Uint8Array>;
      try { r = await reader.read(); }
      catch { break; }   // aborted (stall) or network drop → finalize; caller reloads
      if (r.done) break;
      arm();
      buffer += decoder.decode(r.value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() ?? "";
      for (const chunk of chunks) {
        const dataLine = chunk.split("\n").find((l) => l.startsWith("data: "));
        if (!dataLine) continue;
        try { onEvent(JSON.parse(dataLine.slice(6)) as ChatEvent); } catch { /* ignore */ }
      }
    }
  } finally {
    if (idle) clearTimeout(idle);
  }
}

// --- Live page rebuild (KB-only, two-stage: gather → curate → draft) -------------
export interface RebuildCandidate {
  note_id: number; title: string; date: string; reason: string;
  on: boolean; private: boolean; added?: boolean;
}
export interface RebuildSkipped { note_id: number; title: string; date: string; reason: string; }

// SSE events from the rebuild engine — same wire envelope as streamChat.
export type RebuildEvent =
  | { type: "run_started"; run_id: string; slug: string; title: string; base_rev: string }
  | { type: "tool_use"; tool: string; query?: string }                 // Stage 1: gather agent
  | { type: "tool_result"; tool: string; summary: string; items?: string[] }
  | { type: "sources_proposed"; candidates: RebuildCandidate[]; skipped: RebuildSkipped[] }
  | { type: "thinking_delta"; text: string }                           // Stage 2: drafting
  | { type: "content_delta"; text: string }
  | { type: "lint"; ok: boolean; message: string }
  | { type: "done"; draft: string; truncated?: boolean;
      lint?: { ok: boolean; errors: string[]; warnings: string[]; stub: boolean } }
  | { type: "error"; message: string };

export interface SSEHandle { done: Promise<void>; abort: () => void; }

// Open a POST SSE stream and dispatch parsed events. Returns an abort() so closing the
// rebuild panel cancels the run server-side (lean in-session: close/refresh = cancel).
function streamSSE(path: string, body: unknown, onEvent: (e: RebuildEvent) => void): SSEHandle {
  const ctrl = new AbortController();
  const done = (async () => {
    const res = await fetch(u(path), {
      method: "POST", headers: authHeaders(),
      body: JSON.stringify(body ?? {}), signal: ctrl.signal,
    });
    if (!res.body) throw new ApiError("No response stream", 500);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let idle: number | undefined;
    const STALL_MS = 90000;
    const arm = () => { if (idle) clearTimeout(idle); idle = window.setTimeout(() => ctrl.abort(), STALL_MS); };
    arm();
    try {
      for (;;) {
        let r: ReadableStreamReadResult<Uint8Array>;
        try { r = await reader.read(); } catch { break; }
        if (r.done) break;
        arm();
        buffer += decoder.decode(r.value, { stream: true });
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop() ?? "";
        for (const chunk of chunks) {
          const dataLine = chunk.split("\n").find((l) => l.startsWith("data: "));
          if (!dataLine) continue;
          try { onEvent(JSON.parse(dataLine.slice(6)) as RebuildEvent); } catch { /* ignore */ }
        }
      }
    } finally {
      if (idle) clearTimeout(idle);
    }
  })();
  return { done, abort: () => ctrl.abort() };
}

// Stage 1: gather sources (streams the agent's tool use, ends with sources_proposed).
export const rebuildStream = (slug: string, onEvent: (e: RebuildEvent) => void): SSEHandle =>
  streamSSE(`/api/kb/rebuild/start/${encodeURIComponent(slug)}`, {}, onEvent);

// Stage 1 again: find more sources for a hint, appended to the current set.
export const regatherStream = (runId: string, hint: string, onEvent: (e: RebuildEvent) => void): SSEHandle =>
  streamSSE(`/api/kb/rebuild/${runId}/regather`, { hint }, onEvent);

// Stage 2: draft the article from the curated source ids (streams thinking + body).
export const draftStream = (runId: string, sourceIds: number[], onEvent: (e: RebuildEvent) => void): SSEHandle =>
  streamSSE(`/api/kb/rebuild/${runId}/draft`, { source_ids: sourceIds }, onEvent);

// Re-run the last drafting turn at a larger, approved budget after a truncation.
export const redraftStream = (runId: string, maxTokens: number, onEvent: (e: RebuildEvent) => void): SSEHandle =>
  streamSSE(`/api/kb/rebuild/${runId}/redraft`, { max_tokens: maxTokens }, onEvent);

export const guideStream = (runId: string, text: string, onEvent: (e: RebuildEvent) => void): SSEHandle =>
  streamSSE(`/api/kb/rebuild/${runId}/guide`, { text }, onEvent);

// Add-a-source picker on the curate screen (primary notes only).
export const searchRebuildSources = (runId: string, q: string) =>
  get<{ note_id: number; title: string; date: string }[]>(
    `/api/kb/rebuild/${runId}/search?q=${encodeURIComponent(q)}`);

export const acceptRebuild = (runId: string, renameTo?: string) =>
  post<{ ok: boolean; slug: string }>(`/api/kb/rebuild/${runId}/accept`, { rename_to: renameTo ?? null });

export const rejectRebuild = (runId: string) =>
  post<{ ok: boolean }>(`/api/kb/rebuild/${runId}/reject`);
