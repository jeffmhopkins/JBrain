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

// Attachments need the auth header, so a plain <a>/<img> won't work — fetch+blob.
async function attachmentBlob(id: number): Promise<Blob> {
  const headers: Record<string, string> = {};
  if (accessKey) headers["Authorization"] = `Bearer ${accessKey}`;
  const res = await fetch(u(`/api/attachments/${id}/download`), { headers });
  if (!res.ok) throw new ApiError("Failed to load attachment", res.status);
  return res.blob();
}

export async function attachmentObjectUrl(id: number): Promise<string> {
  return URL.createObjectURL(await attachmentBlob(id));
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
  type: "token" | "staging" | "applied" | "done" | "error";
  text?: string;
  actions?: any[];
  action?: { id: number; summary: string };
  message?: string;
}

// Stream the architect's reply over SSE (POST + ReadableStream, so we can send
// a body and rely on the session cookie).
export const createEntry = <T = any>(text: string, title?: string, loc?: { lat: number; lon: number } | null) =>
  post<T>("/api/notes/entry", { text, title: title || undefined, lat: loc?.lat, lon: loc?.lon });

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
  const res = await fetch(u(`/api/chat/conversations/${conversationId}/message`), {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify(body),
  });
  if (!res.body) throw new ApiError("No response stream", 500);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";
    for (const chunk of chunks) {
      const dataLine = chunk.split("\n").find((l) => l.startsWith("data: "));
      if (!dataLine) continue;
      try { onEvent(JSON.parse(dataLine.slice(6)) as ChatEvent); } catch { /* ignore */ }
    }
  }
}
