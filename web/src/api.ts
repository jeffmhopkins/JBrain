// Thin API client. Every request carries the access key (the "cert") as a
// Bearer token; it is stored on-device and pasted in once on first run.
import { demoResponse, demoStream, isDemo } from "./demo";

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
  if (isDemo()) return demoResponse(path, (opts.method as string) || "GET", opts.body) as T;
  const res = await fetch(u(path), {
    ...opts,
    headers: authHeaders(opts.headers),
  });
  if (res.status === 401) throw new ApiError("Not authenticated", 401);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* ignore */ }
    throw new ApiError(detail, res.status);
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

// Public, UNAUTHENTICATED share endpoints — no bearer key (a recipient has none).
// Uses default same-origin credentials so the bind cookie rides along (recipients
// always open the canonical JBRAIN_DOMAIN share URL = same origin as the API). Do
// NOT switch to credentials:"include" without also enabling credentialed CORS with
// an explicit origin list — `*` + credentials is spec-incompatible and would brick bind.
async function publicApi<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(u(path), { ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* ignore */ }
    throw new ApiError(detail, res.status);
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
// Lab-share — owner management.
export const createLabShare = (analytes: string[], opts: { window_from?: string | null; window_to?: string | null;
    allow_chat?: boolean; intro?: string; label?: string;
    bind?: boolean; single_use?: boolean; ttl_days?: number; max_turns?: number; max_total_replies?: number }) =>
  post<{ token: string; link_id: number; url: string }>("/api/shares/labs", { analytes, ...opts });
// Research links — owner management.
export const researchDetail = <T = any>(linkId: number) => get<T>(`/api/shares/research/${linkId}`);
export const researchSetScope = (linkId: number, prefixes: string[], kinds: string[] = []) =>
  post(`/api/shares/research/${linkId}/scope`, { prefixes, kinds });
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

// Multipart upload: must NOT set Content-Type (browser sets the boundary), so
// we call fetch directly with only the Authorization header.
export const MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024;

// XHR (not fetch) so we get real upload progress. onProgress reports 0–100 for
// bytes sent; the server then extracts text/embeds before responding, so callers
// can show a "processing" phase once it hits 100.
export function uploadAttachment<T = any>(
  slug: string, file: File, onProgress?: (pct: number) => void, analyze = true,
): Promise<T> {
  if (file.size > MAX_ATTACHMENT_BYTES) return Promise.reject(new ApiError("File too large (10 MB max).", 413));
  if (isDemo()) { onProgress?.(100); return Promise.resolve({ id: 1, filename: file.name } as T); }
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

// AI image analysis: kick off (or re-run) and poll status. Demo-guarded so the
// PWA's offline demo never hits a real server.
export interface AnalysisStatus { status: "none" | "pending" | "done" | "error"; detail?: string | null; analyzed_at?: string | null; }
export const analyzeAttachment = (id: number, force = false) =>
  isDemo() ? Promise.resolve({ status: "done" } as AnalysisStatus)
           : post<AnalysisStatus>(`/api/attachments/${id}/analyze`, { force });
export const getAnalysisStatus = (id: number) =>
  isDemo() ? Promise.resolve({ status: "done" } as AnalysisStatus)
           : get<AnalysisStatus>(`/api/attachments/${id}/analysis-status`);

// Force-recompute one note's AI analysis sidecar (ignores the content-hash cache).
export const refreshNoteAnalysis = (slug: string) =>
  post<any>(`/api/notes/${encodeURIComponent(slug)}/analysis`);

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
  if (isDemo()) return;
  const url = URL.createObjectURL(await attachmentBlob(id));
  const a = document.createElement("a");
  a.href = url; a.download = filename || "file";
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

// Download a full DB backup (auth header can't ride on a plain <a>, so fetch+blob).
export async function downloadBackup(): Promise<void> {
  if (isDemo()) { alert("Demo mode — backup is disabled."); return; }
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
  if (isDemo()) { alert("Demo mode — export is disabled."); return; }
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
  if (isDemo()) return { ok: true } as T;
  const fd = new FormData();
  fd.append("file", file);
  const headers: Record<string, string> = {};
  if (accessKey) headers["Authorization"] = `Bearer ${accessKey}`;
  const res = await fetch(u("/api/system/restore"), { method: "POST", headers, body: fd });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* ignore */ }
    throw new ApiError(detail, res.status);
  }
  return res.json();
}

export interface ChatEvent {
  type: "token" | "tool" | "staging" | "applied" | "chart" | "done" | "error";
  chart?: { analyte: string; unit?: string | null; from?: string | null; to?: string | null; title?: string };
  text?: string;
  tool?: string;
  actions?: any[];
  action?: { id: number; summary: string };
  message?: string;
}

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
export interface Owner { id: number | null; name: string; display: string; is_set: boolean; }
export const getOwner = () => get<Owner>("/api/people/owner");
export const setOwner = (name: string) => put<Owner>("/api/people/owner", { name });

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
  text: string, title?: string, loc?: { lat: number; lon: number } | null, dest?: string,
) =>
  post<T>("/api/notes/entry", { text, title: title || undefined, dest: dest || undefined, lat: loc?.lat, lon: loc?.lon });

// Medical-mode capture destinations (the picklist Medical mode offers; entries file
// under notes/medical/<dest>/NN).
export const getMedicalDests = () => get<{ names: string[] }>("/api/medical/destinations");
export const setMedicalDests = (names: string[]) =>
  put<{ names: string[] }>("/api/medical/destinations", { names });
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
): Promise<void> {
  if (isDemo()) { await demoStream(text, onEvent, mode); return; }
  const body: any = { text, mode };
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
