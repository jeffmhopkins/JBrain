// Thin API client. Cookies (the session) ride along automatically.

export async function api<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    credentials: "same-origin",
    ...opts,
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
export const del = <T = any>(p: string) => api<T>(p, { method: "DELETE" });

export interface ChatEvent {
  type: "token" | "staging" | "done" | "error";
  text?: string;
  actions?: any[];
  message?: string;
}

// Stream the architect's reply over SSE (POST + ReadableStream, so we can send
// a body and rely on the session cookie).
export async function streamChat(
  conversationId: number,
  text: string,
  onEvent: (e: ChatEvent) => void,
): Promise<void> {
  const res = await fetch(`/api/chat/conversations/${conversationId}/message`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ text }),
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
