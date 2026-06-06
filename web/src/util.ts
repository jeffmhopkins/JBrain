import type { NavigateFunction } from "react-router-dom";
import { createElement } from "react";
import { Icon } from "./components/Icon";

// Mirror of the backend slugify so [[wiki-links]] can resolve to /note/<slug>.
export function slugify(title: string): string {
  const s = title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  return s || "note";
}

// Resolve a review item's link_slug to an app route. Slugs are usually bare note
// slugs (→ /note/<slug>), but a few are absolute paths ("/shares") or the
// "__shares__" sentinel. Shared by the bell dropdown, the Review inbox, and the
// Notification History page so the three never drift.
export function reviewHref(linkSlug: string): string {
  if (linkSlug.startsWith("/")) return linkSlug;
  if (linkSlug === "__shares__") return "/shares";
  return `/note/${linkSlug}`;
}

// Strip the root prefix (notes/ kb/ lists/ logs/) so a title reads as its bare
// leaf. Titles are stored root-prefixed; this is the human-facing short form.
export const leaf = (t: string) => (t || "").replace(/^(notes|kb|lists|logs)\//i, "");

// Clip to n chars with an ellipsis. Used for graph labels so long titles don't
// overlap; the focused node still shows its full (leaf) title elsewhere.
export const truncate = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

// Display label for a wiki-link with no explicit alias. For kb/ titles the
// category folders are taxonomy, not the article's name, so show the last path
// segment ("kb/People/Jeffrey Mark Hopkins" → "Jeffrey Mark Hopkins", not
// "People/Jeffrey Mark Hopkins"). Other roots keep their root-stripped leaf.
export function wikiLabel(title: string): string {
  const t = (title || "").trim();
  if (/^kb\//i.test(t)) {
    const segs = t.split("/").filter(Boolean);
    return segs[segs.length - 1] || leaf(t);
  }
  return leaf(t);
}

// Rewrite [[Title]] and [[Title|display]] into markdown links to the note route.
export function renderWikiLinks(md: string): string {
  return (md || "").replace(/\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]/g, (_m, title, disp) => {
    const t = String(title).trim();
    const explicit = (disp ?? "").trim();
    const label = explicit || wikiLabel(t);
    // When we shortened a bare link, keep the full title as a hover tooltip so the
    // namespace stays discoverable. Markdown link title: [label](url "title").
    const tip = !explicit && label !== t ? ` "${t.replace(/"/g, "")}"` : "";
    return `[${label}](/note/${slugify(t)}${tip})`;
  });
}

// Remove the AI-image-summary anchor comments for DISPLAY. They're stored in
// content_md as <!-- jbrain:image-summary att=N --> sentinels (so re-analysis can
// find/replace the block), but react-markdown (no rehype-raw) renders raw HTML
// comments as visible text — strip them at render so only the summary body shows.
export function stripSummarySentinels(md: string): string {
  return (md || "").replace(/[ \t]*<!-- \/?jbrain:image-summary att=\d+ -->\n?/g, "");
}

// Build a Google Maps search URL for a free-text address or "lat,lng" pair.
export function mapsUrl(query: string): string {
  return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(query)}`;
}

// Match a US-style street address ("234 E Merritt Island Cswy, Merritt Island, FL 32952"):
// house number, street, city, two-letter state, ZIP (+optional +4). The street/city parts
// are lazy so we stop at the state+ZIP tail rather than swallowing trailing prose.
const _ADDR_RE = /\b\d{1,6}\s+[A-Za-z0-9.'#\-/ ]+?,\s*[A-Za-z.'\- ]+?,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?/g;
// Match a decimal coordinate pair ("28.46388, -80.82609"). Both halves must carry a
// fractional part (≥3 digits) so prices/quantities like "1,234.50" don't match; ranges
// are validated in the callback.
const _COORD_RE = /(?<![\d.\-])(-?\d{1,3}\.\d{3,})\s*,\s*(-?\d{1,3}\.\d{3,})(?![\d.])/g;
// Skip tokens we must never inject inside: fenced/inline code, [[wiki]] links, and existing
// [text](url) / images — so a map pin is only ever appended after plain-prose locations.
const _SKIP_RE = /(```[\s\S]*?```|`[^`]*`|!?\[\[[^\]]*\]\]|!?\[[^\]]*\]\([^)]*\))/g;

// Append a Google-Maps pin link after each address / coordinate pair found in a run of
// plain text. The pin is carried as a `#map:<query>` sentinel that the link renderers
// below turn into an external Google Maps anchor.
function linkifyPlain(text: string): string {
  const pin = (q: string) => ` [map](#map:${encodeURIComponent(q)})`;
  return text
    .replace(_ADDR_RE, (m) => m + pin(m.replace(/\s+/g, " ").trim()))
    .replace(_COORD_RE, (m, lat, lng) =>
      Math.abs(parseFloat(lat)) > 90 || Math.abs(parseFloat(lng)) > 180 ? m : m + pin(`${lat},${lng}`));
}

// Walk the Markdown, linkifying addresses/coordinates in plain-prose segments only and
// passing code spans and existing links through untouched (so we never break a URL or a
// link label). Run on the raw content, before renderWikiLinks.
export function linkifyAddresses(md: string): string {
  if (!md) return md;
  let out = "", last = 0;
  _SKIP_RE.lastIndex = 0;
  for (let m: RegExpExecArray | null; (m = _SKIP_RE.exec(md)); ) {
    out += linkifyPlain(md.slice(last, m.index)) + m[0];
    last = m.index + m[0].length;
  }
  return out + linkifyPlain(md.slice(last));
}

// A `#map:<query>` sentinel → external Google Maps link rendered as a pin icon (text
// children dropped). Returns null for any other href so callers can fall through.
function mapEl(href?: string) {
  if (!href?.startsWith("#map:")) return null;
  const q = (() => { try { return decodeURIComponent(href.slice(5)); } catch { return ""; } })();
  return createElement(
    "a",
    { className: "map-link", href: mapsUrl(q), target: "_blank", rel: "noreferrer",
      title: `Open in Google Maps: ${q}`, "aria-label": "Open in Google Maps" },
    createElement(Icon, { name: "pin", size: 14 }),
  );
}

// Plain ReactMarkdown `a` renderer for contexts without router note-links (shares, guided
// chat): map sentinels become pins, everything else opens in a new tab.
export function mapLinkRenderer({ href, children }: any) {
  return mapEl(href) || createElement("a", { href, target: "_blank", rel: "noreferrer" }, children);
}

// Shared ReactMarkdown `a` renderer: internal /note/ links use the router,
// external links open in a new tab. Reused by NotePage and the version viewer.
export function makeLinkRenderer(navigate: NavigateFunction) {
  return ({ href, children, title }: any) => {
    const map = mapEl(href);
    if (map) return map;
    if (href?.startsWith("#dyn:")) {
      // A dynamic (live) time value — render as a marked span with the anchor tooltip,
      // not a link. The carrier comes from expandTimeTokensMarked.
      const tip = (() => { try { return decodeURIComponent(href.slice(5)); } catch { return "Live value"; } })();
      return createElement("span", { className: "dyn-date", title: tip }, children);
    }
    if (href?.startsWith("/note/")) {
      return createElement(
        "a",
        { className: "wikilink", href, title, onClick: (e: any) => { e.preventDefault(); navigate(href); } },
        children,
      );
    }
    return createElement("a", { href, target: "_blank", rel: "noreferrer", title }, children);
  };
}
