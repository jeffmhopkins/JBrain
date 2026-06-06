import type { NavigateFunction } from "react-router-dom";
import { createElement } from "react";

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

// Shared ReactMarkdown `a` renderer: internal /note/ links use the router,
// external links open in a new tab. Reused by NotePage and the version viewer.
export function makeLinkRenderer(navigate: NavigateFunction) {
  return ({ href, children, title }: any) => {
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
