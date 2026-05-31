import type { NavigateFunction } from "react-router-dom";
import { createElement } from "react";

// Mirror of the backend slugify so [[wiki-links]] can resolve to /note/<slug>.
export function slugify(title: string): string {
  const s = title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  return s || "note";
}

// Rewrite [[Title]] and [[Title|display]] into markdown links to the note route.
export function renderWikiLinks(md: string): string {
  return (md || "").replace(/\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]/g, (_m, title, disp) => {
    const t = String(title).trim();
    const label = (disp ?? title).trim();
    return `[${label}](/note/${slugify(t)})`;
  });
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
