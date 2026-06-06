import { createElement, useRef, useState } from "react";
import type { NavigateFunction } from "react-router-dom";
import { getNotePreview, NotePreview } from "../api";
import { mapsUrl } from "../util";
import { Icon } from "./Icon";

// A chat [[citation]] link that REVEALS its source on hover — so you can verify the cite without
// leaving the conversation. (Dead links are already stripped server-side, so a citation that renders
// here resolves to a real note.) Clicking still opens the note. Fetched once, then cached per slug.
const _cache = new Map<string, NotePreview>();

export function CitationLink({ href, navigate, children }: { href: string; navigate: NavigateFunction; children: any }) {
  const slug = decodeURIComponent(href.replace(/^\/note\//, ""));
  const [preview, setPreview] = useState<NotePreview | null>(_cache.get(slug) || null);
  const [open, setOpen] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  function show() {
    timer.current = window.setTimeout(() => {
      setOpen(true);
      if (!_cache.has(slug)) {
        getNotePreview(slug).then((p) => { _cache.set(slug, p); setPreview(p); }).catch(() => { /* ignore */ });
      }
    }, 250);   // a small delay so a passing cursor doesn't flash popovers
  }
  function hide() { if (timer.current) clearTimeout(timer.current); setOpen(false); }

  return (
    <span className="cite-wrap" onMouseEnter={show} onMouseLeave={hide}>
      <a className="wikilink" href={href} onClick={(e) => { e.preventDefault(); navigate(href); }}>{children}</a>
      {open && (
        <span className="cite-pop" role="tooltip">
          {preview == null ? (
            <span className="muted">Loading source…</span>
          ) : preview.found ? (
            <>
              <span className="cite-pop-head">✓ <strong>{preview.title}</strong></span>
              <span className="cite-pop-body">{preview.excerpt || "(no preview)"}</span>
            </>
          ) : (
            <span className="muted">That note no longer exists.</span>
          )}
        </span>
      )}
    </span>
  );
}

// Chat-specific ReactMarkdown `a` renderer: wikilink citations get the hover-preview; everything
// else behaves like the shared renderer (router nav for /note/, new tab for external).
export function makeChatLinkRenderer(navigate: NavigateFunction) {
  return ({ href, children }: any) => {
    if (href?.startsWith("#map:")) {
      const q = (() => { try { return decodeURIComponent(href.slice(5)); } catch { return ""; } })();
      return createElement(
        "a",
        { className: "map-link", href: mapsUrl(q), target: "_blank", rel: "noreferrer",
          title: `Open in Google Maps: ${q}`, "aria-label": "Open in Google Maps" },
        createElement(Icon, { name: "pin", size: 14 }),
      );
    }
    if (href?.startsWith("/note/")) {
      return createElement(CitationLink, { href, navigate, children });
    }
    if (href?.startsWith("#dyn:")) {
      const tip = (() => { try { return decodeURIComponent(href.slice(5)); } catch { return "Live value"; } })();
      return createElement("span", { className: "dyn-date", title: tip }, children);
    }
    return createElement("a", { href, target: "_blank", rel: "noreferrer" }, children);
  };
}
