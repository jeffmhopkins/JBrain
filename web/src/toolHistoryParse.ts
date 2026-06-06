// Pure parsing helpers for the per-reply tool-call history. Kept dependency-free (no React)
// so they're easy to reason about and test: this is where untrusted tool output is turned
// into safe, structured UI data. The raw result text is NEVER interpreted as markup — only
// explicit [[wiki]] references are lifted out of it into clickable chips.
import { slugify, wikiLabel } from "./util";

const WIKILINK_RE = /\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g;

// Unique [[note/kb]] references in a tool result, as {slug,label} for chips. Deduped by slug
// and capped so a 50-hit search doesn't carpet the panel.
export function noteRefs(resultText: string, cap = 24): { slug: string; label: string }[] {
  const seen = new Map<string, string>();
  let m: RegExpExecArray | null;
  WIKILINK_RE.lastIndex = 0;
  while ((m = WIKILINK_RE.exec(resultText)) && seen.size < cap) {
    const title = m[1].trim();
    if (!title) continue;
    const slug = slugify(title);
    if (!seen.has(slug)) seen.set(slug, wikiLabel(m[2]?.trim() || title));
  }
  return [...seen.entries()].map(([slug, label]) => ({ slug, label }));
}

// A one-line summary of the staging/applied/chart event a tool emitted, if any.
export function eventSummary(eventJson?: string | null): string | null {
  if (!eventJson) return null;
  try {
    const ev = JSON.parse(eventJson);
    if (ev?.type === "applied") return `Applied: ${ev.action?.summary ?? "change"}`;
    if (ev?.type === "staging") {
      const n = Array.isArray(ev.actions) ? ev.actions.length : 0;
      return n ? `Staged ${n} change(s) for review` : "Staged changes for review";
    }
    if (ev?.type === "chart") return `Charted ${ev.chart?.analyte ?? "a lab"}`;
    if (ev?.type === "external_proposal") return `Proposed an external lookup: ${ev.term ?? ""}`.trim();
    return typeof ev?.type === "string" ? ev.type : null;
  } catch { return null; }
}

// Pretty-print a tool's input args; null when there's nothing worth showing ({} or unparseable-empty).
export function prettyArgs(argsJson: string): string | null {
  try {
    const a = JSON.parse(argsJson);
    if (a && typeof a === "object" && !Array.isArray(a) && Object.keys(a).length === 0) return null;
    return JSON.stringify(a, null, 2);
  } catch { return argsJson || null; }
}
