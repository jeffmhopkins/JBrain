// Display-time helpers for time. The backend stores everything in UTC (no 'Z'
// suffix); fmtTs localizes those strings to the owner's configured app timezone
// (labeled), and expandTimeTokens renders the live @t[...] values in note bodies.
//
// expandTimeTokens is the browser twin of server/app/services/clock.py
// expand_tokens — keep the two in sync (shared fixture: server/tests/fixtures/
// time_tokens.json pins the deterministic cases).

// Localize a stored UTC timestamp ("YYYY-MM-DD HH:MM:SS[.fff]", no zone) to the
// app timezone, e.g. "Jun 1, 2026, 7:00 AM EDT". Falls back to the device zone
// when appTz is unknown, and returns the raw string if it can't be parsed.
export function fmtTs(raw: string, appTz?: string): string {
  if (!raw) return "";
  const hasZone = /[zZ]|[+-]\d\d:?\d\d$/.test(raw);
  const d = new Date(raw.replace(" ", "T") + (hasZone ? "" : "Z"));
  if (isNaN(d.getTime())) return raw;
  return d.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit",
    ...(appTz ? { timeZone: appTz, timeZoneName: "short" } : {}),
  });
}

// Compact variant for dense lists (history rows): no timezone label, and the year
// is dropped when it's the current year, e.g. "Jun 1, 9:41 PM".
export function fmtTsShort(raw: string, appTz?: string): string {
  if (!raw) return "";
  const hasZone = /[zZ]|[+-]\d\d:?\d\d$/.test(raw);
  const d = new Date(raw.replace(" ", "T") + (hasZone ? "" : "Z"));
  if (isNaN(d.getTime())) return raw;
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric",
    ...(d.getFullYear() !== new Date().getFullYear() ? { year: "numeric" } : {}),
    hour: "numeric", minute: "2-digit",
    ...(appTz ? { timeZone: appTz } : {}),
  });
}

// Compact elapsed duration for live "running for…" / "last output…" indicators:
// "3s", "1m 05s", "2h 09m". Takes milliseconds; clamps negatives to 0.
export function fmtElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  if (total < 60) return `${total}s`;
  const m = Math.floor(total / 60);
  if (m < 60) return `${m}m ${String(total % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

// Parse a stored/ISO UTC timestamp (with or without a zone suffix) to epoch ms,
// or NaN if unparseable. Shared by the live elapsed indicators.
export function parseUtcMs(raw?: string | null): number {
  if (!raw) return NaN;
  const hasZone = /[zZ]|[+-]\d\d:?\d\d$/.test(raw);
  return new Date(raw.replace(" ", "T") + (hasZone ? "" : "Z")).getTime();
}

const TOKEN_RE = /@t\[(age|until|since):([^\]]+)\]/g;
const UNITS: [string, number][] = [
  ["year", 31536000], ["month", 2592000], ["week", 604800],
  ["day", 86400], ["hour", 3600], ["minute", 60],
];

function humanize(seconds: number): string {
  const secs = Math.abs(Math.trunc(seconds));
  for (const [unit, n] of UNITS) {
    if (secs >= n) { const v = Math.floor(secs / n); return `${v} ${unit}${v !== 1 ? "s" : ""}`; }
  }
  return "less than a minute";
}

function ymdInTz(d: Date, tz?: string): [number, number, number] {
  const p = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(d);
  const get = (t: string) => Number(p.find((x) => x.type === t)!.value);
  return [get("year"), get("month"), get("day")];
}

// Expand @t[age:DATE] / @t[until:ISO] / @t[since:ISO]. Malformed tokens are left
// verbatim (never throws). Display-only; storage keeps the raw token.
export function expandTimeTokens(md: string, appTz?: string, nowMs?: number): string {
  if (!md || !md.includes("@t[")) return md;
  const now = nowMs != null ? new Date(nowMs) : new Date();
  return md.replace(TOKEN_RE, (whole, kind, arg) => {
    const a = String(arg).trim();
    if (kind === "age") {
      const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(a);
      if (!m) return whole;
      const [by, bm, bd] = [Number(m[1]), Number(m[2]), Number(m[3])];
      const [ty, tm, td] = ymdInTz(now, appTz);
      let years = ty - by - ((tm < bm || (tm === bm && td < bd)) ? 1 : 0);
      if (years < 0) return whole;
      return String(years);
    }
    const norm = a.replace(" ", "T");
    const anchor = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(norm) ? norm : norm + "Z");
    if (isNaN(anchor.getTime())) return whole;
    const delta = (anchor.getTime() - now.getTime()) / 1000;
    if (kind === "until") {
      return Math.abs(delta) < 60 ? "now" : (delta > 0 ? `in ${humanize(delta)}` : `${humanize(delta)} ago`);
    }
    return Math.abs(delta) < 60 ? "just now" : (delta < 0 ? `${humanize(delta)} ago` : `in ${humanize(delta)}`);
  });
}

function dynTooltip(kind: string, arg: string): string {
  const a = String(arg).trim();
  if (kind === "age") return `Live value — age from ${a}; updates over time`;
  if (kind === "until") return `Live value — countdown to ${a}; updates over time`;
  return `Live value — elapsed since ${a}; updates over time`;
}

// Display-only: expand @t[...] tokens AND wrap each DYNAMIC value in a #dyn: link
// carrier so the markdown link renderer can mark it (dotted underline + tooltip with
// the anchor). Kept SEPARATE from expandTimeTokens — which stays the byte-for-byte
// twin of clock.py — by reusing it per-token, so a frozen number stays plain and only
// live values get marked. Run BEFORE renderWikiLinks (it only touches [[...]]).
export function expandTimeTokensMarked(md: string, appTz?: string, nowMs?: number): string {
  if (!md || !md.includes("@t[")) return md;
  return md.replace(TOKEN_RE, (whole, kind, arg) => {
    const value = expandTimeTokens(whole, appTz, nowMs);   // reuse the twin
    if (value === whole) return whole;                     // unparseable → leave token verbatim
    return `[${value}](#dyn:${encodeURIComponent(dynTooltip(kind, String(arg)))})`;
  });
}
