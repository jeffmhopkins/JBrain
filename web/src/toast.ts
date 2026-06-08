// Lightweight, dependency-free toast system: a module-singleton store (same
// useSyncExternalStore pattern as health.ts) + a Toaster view. Replaces blocking
// window.alert() for in-the-moment errors so a failure is surfaced without freezing
// the page, and is de-duped so a burst of identical errors shows once.
import { useSyncExternalStore } from "react";
import { ApiError } from "./api";

export type ToastTone = "error" | "info" | "success";
export interface Toast { id: number; message: string; tone: ToastTone }

const AUTO_DISMISS_MS = 6000;
// Short window: collapse a programmatic BURST of the identical message (which fires
// in milliseconds) WITHOUT suppressing a deliberate user retry a couple seconds later.
const DEDUPE_MS = 1500;
const MAX_TOASTS = 4;

let toasts: Toast[] = [];
let nextId = 1;
const recent = new Map<string, number>();   // per-key last-shown time (burst collapse)
const timers = new Map<number, ReturnType<typeof setTimeout>>();
const listeners = new Set<() => void>();
let _now = () => Date.now();

function emit() { listeners.forEach((l) => l()); }

export function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}
export function getToasts(): Toast[] { return toasts; }

export function showToast(message: string, tone: ToastTone = "error"): number | null {
  const now = _now();
  const key = `${tone}:${message}`;
  // Purge stale dedupe keys so the map can't grow unbounded.
  for (const [k, t] of recent) if (now - t >= DEDUPE_MS) recent.delete(k);
  const last = recent.get(key);
  if (last != null && now - last < DEDUPE_MS) return null;   // collapse identical burst
  recent.set(key, now);
  const id = nextId++;
  toasts = [...toasts, { id, message, tone }].slice(-MAX_TOASTS);
  emit();
  timers.set(id, setTimeout(() => dismissToast(id), AUTO_DISMISS_MS));
  return id;
}

export function dismissToast(id: number): void {
  const timer = timers.get(id);
  if (timer) { clearTimeout(timer); timers.delete(id); }   // no leaked/duplicate timers
  const next = toasts.filter((t) => t.id !== id);
  if (next.length !== toasts.length) { toasts = next; emit(); }
}

export function useToasts(): Toast[] {
  return useSyncExternalStore(subscribe, getToasts, getToasts);
}

// Turn an unknown thrown value into a user-facing message. Prefers the server's
// `detail` (already parsed into ApiError.message). Phase 7 extends this with
// capability-aware copy (CAP_COPY) for failures that slip past pre-flight gating.
export function explainError(err: unknown, fallback = "Something went wrong."): string {
  if (err instanceof ApiError) return err.message || fallback;
  if (err instanceof Error) return err.message || fallback;
  if (typeof err === "string" && err.trim()) return err;
  return fallback;
}

/** Test-only reset. */
export function __resetToasts(nowFn?: () => number): void {
  if (nowFn) _now = nowFn;
  timers.forEach((t) => clearTimeout(t));
  timers.clear(); recent.clear();
  toasts = []; nextId = 1; emit();
}
