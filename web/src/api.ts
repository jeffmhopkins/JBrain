// Thin API client. Every request carries the access key (the "cert") as a
// Bearer token; it is stored on-device and pasted in once on first run.

const KEY_STORAGE = "jbrain_access_key";
let accessKey: string | null = localStorage.getItem(KEY_STORAGE);

export function setAccessKey(key: string) {
  accessKey = key;
  localStorage.setItem(KEY_STORAGE, key);
}
export function getAccessKey(): string | null {
  return accessKey;
}
export function clearAccessKey() {
  accessKey = null;
  localStorage.removeItem(KEY_STORAGE);
}

function authHeaders(extra: HeadersInit = {}): HeadersInit {
  const h: Record<string, string> = { "Content-Type": "application/json", ...(extra as any) };
  if (accessKey) h["Authorization"] = `Bearer ${accessKey}`;
  return h;
}

export async function api<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
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

// Multipart upload: must NOT set Content-Type (browser sets the boundary), so
// we call fetch directly with only the Authorization header.
export async function uploadAttachment<T = any>(slug: string, file: File): Promise<T> {
  const fd = new FormData();
  fd.append("file", file);
  const headers: Record<string, string> = {};
  if (accessKey) headers["Authorization"] = `Bearer ${accessKey}`;
  const res = await fetch(`/api/notes/${encodeURIComponent(slug)}/attachments`, {
    method: "POST",
    headers,
    body: fd,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* ignore */ }
    throw new ApiError(detail, res.status);
  }
  return res.json();
}

// Download a full DB backup (auth header can't ride on a plain <a>, so fetch+blob).
export async function downloadBackup(): Promise<void> {
  const headers: Record<string, string> = {};
  if (accessKey) headers["Authorization"] = `Bearer ${accessKey}`;
  const res = await fetch("/api/system/backup", { headers });
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
  const fd = new FormData();
  fd.append("file", file);
  const headers: Record<string, string> = {};
  if (accessKey) headers["Authorization"] = `Bearer ${accessKey}`;
  const res = await fetch("/api/system/restore", { method: "POST", headers, body: fd });
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
export async function streamChat(
  conversationId: number,
  text: string,
  onEvent: (e: ChatEvent) => void,
  location?: { lat: number; lon: number } | null,
): Promise<void> {
  const body: any = { text };
  if (location) { body.lat = location.lat; body.lon = location.lon; }
  const res = await fetch(`/api/chat/conversations/${conversationId}/message`, {
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
