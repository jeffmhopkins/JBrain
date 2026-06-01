import type { NavigateFunction } from "react-router-dom";
import { createElement } from "react";

// Mirror of the backend slugify so [[wiki-links]] can resolve to /note/<slug>.
export function slugify(title: string): string {
  const s = title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  return s || "note";
}

// Strip the root prefix (notes/ kb/ lists/ logs/) so a title reads as its bare
// leaf. Titles are stored root-prefixed; this is the human-facing short form.
export const leaf = (t: string) => (t || "").replace(/^(notes|kb|lists|logs)\//i, "");

// Clip to n chars with an ellipsis. Used for graph labels so long titles don't
// overlap; the focused node still shows its full (leaf) title elsewhere.
export const truncate = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

// Rewrite [[Title]] and [[Title|display]] into markdown links to the note route.
export function renderWikiLinks(md: string): string {
  return (md || "").replace(/\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]/g, (_m, title, disp) => {
    const t = String(title).trim();
    const label = (disp ?? title).trim();
    return `[${label}](/note/${slugify(t)})`;
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
  return ({ href, children }: any) => {
    if (href?.startsWith("/note/")) {
      return createElement(
        "a",
        { className: "wikilink", href, onClick: (e: any) => { e.preventDefault(); navigate(href); } },
        children,
      );
    }
    return createElement("a", { href, target: "_blank", rel: "noreferrer" }, children);
  };
}
