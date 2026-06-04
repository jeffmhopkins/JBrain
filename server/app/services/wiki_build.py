"""Knowledge-base BUILD engine (component 2) — the automatic rebuild.

Composition lives in the wiki_build action recipe; the heavy lifting is here:

  reset()         soft-delete every kb/ article EXCEPT protected kb/_* pages, and clear
                  the synthesis watermark/markers so a fresh build re-reads everything.
  corpus_digest() the compact survey the outline reads — gist + domain + entities per
                  note, from the analysis sidecar (falling back to a content snippet).
  outline()       LLM → the entity-first taxonomy: articles (kb/<Domain>/<Name>) each
                  with a scope and the source-note ids assigned to it, plus the _index.
  write_one()     write ONE article from its raw source notes + the domain guide, then a
                  single self-critique/revise pass against the structure lint.
  write_batch()   write every article; split into valid (saved by the recipe) vs
                  quarantined (failed the lint — surfaced, not saved).

Raw source notes stay the ground truth — the analysis sidecar only guides organisation.
"""
from __future__ import annotations

import json
import logging
import re

from . import llm, prompts, wiki_guides

log = logging.getLogger("jbrain")

_FENCE_RE = re.compile(r"^\s*```(?:markdown|md)?\s*\n(.*?)\n```\s*$", re.DOTALL)
_TALK_RE = re.compile(r"\n?```talk\s*\n(.*?)```[ \t]*\n?", re.DOTALL)


def _extract_talk(text: str):
    """Pull a trailing ```talk JSON block out of the writer's output. Returns
    (article_without_block, [entries])."""
    m = _TALK_RE.search(text or "")
    if not m:
        return text, []
    body = text[:m.start()] + text[m.end():]
    try:
        data = json.loads(m.group(1))
        entries = [e for e in data if isinstance(e, dict) and e.get("body")] if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        entries = []
    return body, entries


def reset(conn) -> dict:
    """Soft-delete all kb/ articles except protected kb/_* pages (guides, index), and
    clear the synthesis watermark + per-entry evaluated markers. Undoable (soft delete +
    versioning). Returns {deleted, kept}."""
    from . import notes as notes_svc
    rows = conn.execute(
        "SELECT id, title FROM notes WHERE kind = 'kb' AND deleted_at IS NULL"
    ).fetchall()
    deleted = kept = 0
    for r in rows:
        if wiki_guides.is_protected(r["title"]):
            kept += 1
            continue
        notes_svc.soft_delete(conn, r["id"])
        deleted += 1
    conn.execute("DELETE FROM meta WHERE key LIKE 'wiki_synth:evaluated:%'")
    conn.execute("DELETE FROM meta WHERE key = 'wiki_synth:since'")
    conn.commit()
    return {"deleted": deleted, "kept": kept}


def corpus_digest(conn, limit: int = 3000) -> list[dict]:
    """The survey the outline reads: one compact record per entry/daily note —
    {id, title, gist, domain, entities}. Uses the analysis sidecar; falls back to a
    content snippet for notes not yet analyzed."""
    rows = conn.execute(
        "SELECT n.id, n.title, n.content_md, a.gist, a.domain, a.entities_json "
        "FROM notes n LEFT JOIN note_analysis a ON a.note_id = n.id "
        "WHERE n.deleted_at IS NULL AND n.kind IN ('entry','daily') "
        "ORDER BY n.updated_at DESC LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    out = []
    for r in rows:
        gist = (r["gist"] or "").strip()
        if not gist:
            gist = re.sub(r"\s+", " ", (r["content_md"] or "")).strip()[:160]
        ents = []
        try:
            ents = [e.get("name") for e in json.loads(r["entities_json"] or "[]")
                    if isinstance(e, dict) and e.get("name")]
        except Exception:  # noqa: BLE001
            pass
        out.append({"id": r["id"], "title": r["title"], "gist": gist,
                    "domain": r["domain"] or "Unsure", "entities": ents[:8]})
    return out


def _survey_text(digest: list[dict], cap: int = 800) -> str:
    lines = []
    for d in digest[:cap]:
        ents = ", ".join(d.get("entities") or [])
        line = f"#{d['id']} [{d.get('domain', '?')}] {d['gist']}"
        if ents:
            line += f" — entities: {ents}"
        lines.append(line)
    return "\n".join(lines)


def build_index_md(articles: list[dict]) -> str:
    """The kb/_index org map — articles grouped by domain, linked. Protected, so the
    rebuild never deletes it and synthesis never feeds it back."""
    by_dom: dict[str, list[dict]] = {}
    for a in articles:
        by_dom.setdefault(a.get("domain") or "Other", []).append(a)
    lines = ["# Knowledge base index", "",
             "Auto-generated map of the knowledge base, organised by domain. "
             "Rebuilt whenever the knowledge base is rebuilt.", ""]
    ordered = [*wiki_guides.DOMAINS, *(d for d in by_dom if d not in wiki_guides.DOMAINS)]
    for dom in ordered:
        arts = by_dom.get(dom)
        if not arts:
            continue
        lines.append(f"## {dom}")
        for a in sorted(arts, key=lambda x: x["title"]):
            scope = (a.get("scope") or "").strip()
            lines.append(f"- [[{a['title']}]]" + (f" — {scope}" if scope else ""))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _isint(x) -> bool:
    try:
        int(x); return True
    except (TypeError, ValueError):
        return False


def outline(conn, digest: list[dict], instructions: str | None = None) -> dict:
    """Survey → taxonomy. Returns {articles: [{title, domain, scope, sources}], index_md}.
    The canonical entity roster (recurring people/orgs/places + co-occurrence) is fed in
    alongside the per-note survey so the outline reliably makes one article per entity and
    clusters co-occurring people into Groups; entity mentions then backfill each article's
    sources so no note about an entity is missed."""
    if not llm.has_credentials() or not digest:
        return {"articles": [], "index_md": ""}
    from . import entity_index
    extra = f"\nAdditional guidance: {instructions}\n" if instructions else ""
    roster = entity_index.roster(conn) or "(none yet)"
    prompt = (prompts.get("actions.wiki_outline", "")
              .replace("{survey}", _survey_text(digest))
              .replace("{roster}", roster)
              .replace("{instructions}", extra))
    try:
        text = llm.complete([{"role": "user", "content": prompt}], max_tokens=4000)
    except Exception as exc:  # noqa: BLE001
        log.info("wiki_outline failed: %s", exc)
        return {"articles": [], "index_md": ""}

    from .workflows import _parse_json_array
    articles, seen = [], set()
    for a in _parse_json_array(text):
        if not isinstance(a, dict):
            continue
        title = str(a.get("title") or "").strip()
        if not title.lower().startswith("kb/") or title.lower() in seen:
            continue
        if wiki_guides.is_protected(title):       # never let the outline target a system page
            continue
        seen.add(title.lower())
        domain = a.get("domain") or wiki_guides.domain_for_title(title)
        sources = [int(x) for x in (a.get("sources") or []) if _isint(x)]
        articles.append({"title": title, "domain": domain,
                         "scope": str(a.get("scope") or "").strip(), "sources": sources})

    # Assignment safety net: if an article's name matches a canonical entity, make sure
    # EVERY note that mentions that entity is in its sources — catching any the LLM missed.
    for art in articles:
        leaf = art["title"].split("/")[-1]
        ids = entity_index.note_ids_for_name(conn, leaf)
        if ids:
            art["sources"] = sorted(set(art["sources"]) | set(ids))
    return {"articles": articles, "index_md": build_index_md(articles)}


def _load_sources(conn, ids: list[int]) -> list[dict]:
    ids = [int(i) for i in ids if _isint(i)]
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, title, content_md, created_at FROM notes "
        f"WHERE id IN ({q}) AND deleted_at IS NULL ORDER BY created_at",
        ids,
    ).fetchall()
    out = []
    for r in rows:
        # Pass RAW content (do NOT expand @t[...] tokens): the writer must see the live
        # tokens so it can carry them through into the evergreen article — expanding
        # here would freeze "Jeff is @t[age:1986-03-15]" into a literal that rots.
        out.append({"title": r["title"], "date": (r["created_at"] or "")[:10],
                    "content": (r["content_md"] or "")[:2000]})
    return out


def _sources_text(srcs: list[dict]) -> str:
    return "\n\n".join(f"[{s['title']}] ({s['date']})\n{s['content']}" for s in srcs)


def _strip_fence(text: str) -> str:
    m = _FENCE_RE.match(text or "")
    return (m.group(1) if m else (text or "")).strip()


def write_one(conn, art: dict, instructions: str | None = None) -> dict:
    """Write one article from its raw sources + the domain guide, then ONE
    self-critique/revise pass against the structure lint. Returns
    {title, domain, content_md, ok, errors, warnings, stub}."""
    title = str(art.get("title") or "").strip()
    domain = art.get("domain") or wiki_guides.domain_for_title(title)
    scope = str(art.get("scope") or "")
    srcs = _load_sources(conn, art.get("sources") or [])
    base = {"title": title, "domain": domain, "content_md": "", "ok": False,
            "errors": [], "warnings": [], "stub": False, "talk": []}
    if not srcs:
        base["errors"] = ["no source notes resolved"]
        return base
    if not llm.has_credentials():
        base["errors"] = ["no LLM credentials"]
        return base

    general = wiki_guides.guide_text(None)
    dguide = wiki_guides.guide_text(domain)
    prompt = (prompts.get("actions.wiki_write", "")
              .replace("{general_guide}", general).replace("{domain_guide}", dguide)
              .replace("{domain}", domain or "").replace("{title}", title)
              .replace("{scope}", scope).replace("{sources}", _sources_text(srcs)))
    try:
        draft, talk = _extract_talk(_strip_fence(llm.complete([{"role": "user", "content": prompt}], max_tokens=2200)))
    except Exception as exc:  # noqa: BLE001
        base["errors"] = [f"write failed: {exc}"]
        return base

    v = wiki_guides.validate_structure(title, draft)
    if (v["errors"] or v["warnings"]) and not v["stub"]:
        issues = "\n".join(f"- {x}" for x in (v["errors"] + v["warnings"]))
        rprompt = (prompts.get("actions.wiki_revise", "")
                   .replace("{issues}", issues).replace("{general_guide}", general)
                   .replace("{domain_guide}", dguide).replace("{domain}", domain or "")
                   .replace("{draft}", draft))
        try:
            revised, rtalk = _extract_talk(_strip_fence(
                llm.complete([{"role": "user", "content": rprompt}], max_tokens=2200)))
            v2 = wiki_guides.validate_structure(title, revised)
            if len(v2["errors"]) <= len(v["errors"]):   # keep the revision only if no worse
                draft, v, talk = revised, v2, (rtalk or talk)
        except Exception as exc:  # noqa: BLE001
            log.info("wiki_revise failed for %s: %s", title, exc)

    return {"title": title, "domain": domain, "content_md": draft, "talk": talk,
            "ok": v["ok"], "errors": v["errors"], "warnings": v["warnings"], "stub": v["stub"]}


def write_batch(conn, articles: list[dict], instructions: str | None = None) -> dict:
    """Write every article; split valid (saved by the recipe) vs quarantined (failed
    the structure lint — surfaced, not saved). Mirrors validate_citations' shape."""
    valid, quarantined = [], []
    for art in articles:
        d = write_one(conn, art, instructions)
        (valid if d["ok"] and d["content_md"] else quarantined).append(d)
    return {"valid": valid, "quarantined": quarantined,
            "count": len(articles), "bad": len(quarantined)}
