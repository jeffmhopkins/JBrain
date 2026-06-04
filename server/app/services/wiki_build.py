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

from . import llm, prompts, wiki_guides, wikilinks

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
    versioning). Returns {deleted, kept}. Disambiguation pages (kb/_disambig/*) are DERIVED
    build artifacts, not static guides, so they're cleared here too (and regenerated)."""
    from . import notes as notes_svc
    rows = conn.execute(
        "SELECT id, title FROM notes WHERE kind = 'kb' AND deleted_at IS NULL"
    ).fetchall()
    deleted = kept = 0
    for r in rows:
        derived = r["title"].startswith("kb/_disambig/")
        if wiki_guides.is_protected(r["title"]) and not derived:
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
    from . import people
    prompt = (prompts.get("actions.wiki_outline", "")
              .replace("{owner}", people.owner_name(conn))
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


def _bad_links(conn, content: str, allowed: set[str]) -> list[str]:
    """[[targets]] in `content` that point nowhere — neither an allowed article title nor
    an existing live note. These are the dead links we refuse to save."""
    bad = []
    for t in wikilinks.extract_links(content):
        if t in allowed:
            continue
        if conn.execute("SELECT 1 FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL",
                        (t,)).fetchone():
            continue
        bad.append(t)
    return bad


def _neutralize_links(content: str, bad: set[str]) -> str:
    """Unwrap dead [[links]] into plain text (display text, else the title leaf) so a bad
    link can never reach a saved article. Non-bad links are left untouched."""
    def repl(m):
        target = m.group(1).strip()
        if target not in bad:
            return m.group(0)
        inner = m.group(0)[2:-2]
        return inner.split("|", 1)[1].strip() if "|" in inner else target.split("/")[-1]
    return wikilinks.WIKILINK_RE.sub(repl, content)


def write_one(conn, art: dict, instructions: str | None = None,
              known_titles: list[str] | None = None) -> dict:
    """Write one article from its raw sources + the domain guide, then ONE
    self-critique/revise pass against the structure lint. `known_titles` is the set of
    articles that will exist, so cross-links target real pages instead of inventing dead
    ones. Returns {title, domain, content_md, ok, errors, warnings, stub, talk}."""
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

    from . import people
    owner = people.owner_name(conn)
    general = wiki_guides.guide_text(None)
    dguide = wiki_guides.guide_text(domain)
    # Only let the writer cross-link articles that will exist (minus this one), so it
    # can't invent a dead [[kb/People/Someone]] link. Capped to bound prompt size.
    others = [t for t in (known_titles or []) if t and t != title][:600]
    known_block = "\n".join(others) if others else "(no other articles yet)"
    prompt = (prompts.get("actions.wiki_write", "")
              .replace("{owner}", owner)
              .replace("{general_guide}", general).replace("{domain_guide}", dguide)
              .replace("{domain}", domain or "").replace("{title}", title)
              .replace("{known_titles}", known_block)
              .replace("{scope}", scope).replace("{sources}", _sources_text(srcs)))
    try:
        draft, talk = _extract_talk(_strip_fence(llm.complete([{"role": "user", "content": prompt}], max_tokens=2200)))
    except Exception as exc:  # noqa: BLE001
        base["errors"] = [f"write failed: {exc}"]
        return base

    allowed = {t for t in (known_titles or [])} | {title}
    v = wiki_guides.validate_structure(title, draft)
    bad = _bad_links(conn, draft, allowed)
    # A dead link is always worth a revise pass (even if the structure lint is clean), so
    # the model rewrites it as a real link or plain text rather than us mechanically cutting it.
    if ((v["errors"] or v["warnings"]) and not v["stub"]) or bad:
        link_issues = [f"Dead link [[{t}]] — not a real article; link a listed article instead, or write it as plain text"
                       for t in bad]
        issues = "\n".join(f"- {x}" for x in (v["errors"] + v["warnings"] + link_issues))
        rprompt = (prompts.get("actions.wiki_revise", "")
                   .replace("{issues}", issues).replace("{general_guide}", general)
                   .replace("{domain_guide}", dguide).replace("{domain}", domain or "")
                   .replace("{known_titles}", known_block).replace("{draft}", draft))
        try:
            revised, rtalk = _extract_talk(_strip_fence(
                llm.complete([{"role": "user", "content": rprompt}], max_tokens=2200)))
            v2 = wiki_guides.validate_structure(title, revised)
            bad2 = _bad_links(conn, revised, allowed)
            # Keep the revision when it doesn't regress the lint AND clears at least as many links.
            if len(v2["errors"]) <= len(v["errors"]) and len(bad2) <= len(bad):
                draft, v, talk, bad = revised, v2, (rtalk or talk), bad2
        except Exception as exc:  # noqa: BLE001
            log.info("wiki_revise failed for %s: %s", title, exc)

    # Backstop guarantee: whatever the model left, no dead link survives into the saved
    # article — unwrap it to plain text and note it on the article's talk.
    if bad:
        draft = _neutralize_links(draft, set(bad))
        talk = list(talk) + [{"kind": "note",
                              "body": f"Unlinked dead reference [[{t}]] — no such article; kept as plain text."}
                             for t in bad]

    return {"title": title, "domain": domain, "content_md": draft, "talk": talk,
            "ok": v["ok"], "errors": v["errors"], "warnings": v["warnings"], "stub": v["stub"]}


def dead_links(conn) -> list[dict]:
    """Dangling [[links]] from a kb article to a target that doesn't exist — surfaced so
    they can be fixed instead of silently rotting. Covers real articles AND the DERIVED
    nav pages that link out (kb/_index, kb/_disambig/*); only the static guides are skipped
    (they carry no article cross-links)."""
    rows = conn.execute(
        "SELECT s.title AS source_title, s.slug AS source_slug, l.target_title "
        "FROM links l JOIN notes s ON s.id = l.source_note_id AND s.deleted_at IS NULL "
        "WHERE l.target_note_id IS NULL AND s.kind='kb' AND ("
        "  s.title NOT LIKE 'kb/\\_%' ESCAPE '\\' "
        "  OR s.title = 'kb/_index' OR s.title LIKE 'kb/\\_disambig/%' ESCAPE '\\') "
        "ORDER BY s.title, l.target_title",
    ).fetchall()
    return [dict(r) for r in rows]


def flag_dead_links(conn) -> dict:
    """Neutralize dead cross-links in saved articles and LOG the fix. Runs after every
    article is saved, so it has ground truth: any [[link]] whose target still doesn't
    exist (e.g. the target article was planned but quarantined) is unwrapped to plain text
    and recorded as a RESOLVED note on the article's talk — the AI handled it, so it's a
    log entry, never an open item awaiting a click. Also closes any dead-link items left
    open by an earlier build/model."""
    from . import article_talk
    from . import notes as notes_svc
    items = dead_links(conn)
    by_src: dict[str, set[str]] = {}
    for it in items:
        by_src.setdefault(it["source_title"], set()).add(it["target_title"])
    fixed = 0
    for src, targets in by_src.items():
        row = conn.execute(
            "SELECT content_md FROM notes WHERE title=? AND kind='kb' AND deleted_at IS NULL",
            (src,)).fetchone()
        if row:
            new = _neutralize_links(row["content_md"] or "", set(targets))
            if new != (row["content_md"] or ""):
                notes_svc.upsert_note(conn, src, new, kind="kb", version_note="unlinked dead references")
                fixed += 1
        article_talk.record(conn, src, [
            {"kind": "note", "body": f"Unlinked dead reference [[{t}]] — no such article; kept as plain text."}
            for t in sorted(targets)], author="ai")
    # Dead-link entries are completed actions, not open work — resolve them (and any left
    # open by older builds) so they live in the log, not the "needs attention" list.
    conn.execute(
        "UPDATE article_talk SET resolved_at=datetime('now') WHERE resolved_at IS NULL "
        "AND (body LIKE 'Dead link%' OR body LIKE 'Unlinked dead reference%')")
    conn.commit()
    return {"dead_links": len(items), "articles": len(by_src), "fixed": fixed}


def link_owner(conn) -> dict:
    """Connect the default person (the note-taker / 'me') to their People article, so the
    owner's page isn't an orphan. Matches the article leaf first against the person's real
    name/aliases, then against the generic placeholders the writer uses when the default
    person is unnamed ('Owner', 'Me'). Never guesses — returns linked:None rather than
    risk attaching 'me' to a family member's page."""
    from . import people
    o = people.owner(conn)
    if not o:
        return {"linked": None}
    name = (o["name"] or "").strip().lower()
    aliases = {a.strip().lower() for a in (o["aliases"] or "").split(",") if a.strip()}
    strong = ({name} if name and name != "me" else set()) | aliases
    weak = {"owner", "me", "the owner"} | ({name} if name else set())
    rows = [dict(r) for r in conn.execute(
        "SELECT slug, title FROM notes WHERE kind='kb' AND deleted_at IS NULL "
        "AND title LIKE 'kb/People/%'").fetchall()]

    def find(cands):
        for r in rows:
            if r["title"].split("/")[-1].strip().lower() in cands:
                return r
        return None

    target = (find(strong) if strong else None) or find(weak)
    if not target:
        return {"linked": None}
    conn.execute("UPDATE people SET note_slug=? WHERE id=?", (target["slug"], o["id"]))
    conn.commit()
    return {"linked": target["title"], "person": o["name"]}


_MAINTAIN_RE = re.compile(r"\n?```maintain\s*\n(.*?)```[ \t]*\n?", re.DOTALL)


def _extract_maintain(text: str):
    """Pull the trailing ```maintain JSON block out of the maintenance output.
    Returns (article_without_block, {resolved:[...], new:[...]})."""
    m = _MAINTAIN_RE.search(text or "")
    if not m:
        return text, {}
    body = text[:m.start()] + text[m.end():]
    try:
        data = json.loads(m.group(1))
    except Exception:  # noqa: BLE001
        data = {}
    return body, (data if isinstance(data, dict) else {})


def maintain_one(conn, article_title: str, known_titles: list[str] | None = None,
                 extra_source_ids: list[int] | None = None, removed_titles: list[str] | None = None) -> dict:
    """Component 3: update an article so it's faithful + current — address its OPEN talk
    items, integrate NEW/CHANGED sources (extra_source_ids), and purge claims that relied
    on REMOVED (deleted) sources (removed_titles). Returns the revised article + which
    items the model resolved (with how) + any new items. Pure — the caller applies/records."""
    from . import article_talk
    base = {"title": article_title, "ok": False, "changed": False,
            "content_md": "", "resolved": [], "new": [], "errors": [], "warnings": []}
    note = conn.execute(
        "SELECT content_md FROM notes WHERE title=? AND kind='kb' AND deleted_at IS NULL",
        (article_title,)).fetchone()
    if not note:
        base["errors"] = ["no such article"]
        return base
    open_items = [it for it in article_talk.open_for(conn, article_title)
                  if it["kind"] in ("conflict", "question", "todo", "directive")]
    extra_source_ids = [int(i) for i in (extra_source_ids or [])]
    removed_titles = [t for t in (removed_titles or []) if t]
    if not (open_items or extra_source_ids or removed_titles):
        return {**base, "ok": True}                         # nothing to do
    if not llm.has_credentials():
        base["errors"] = ["no LLM credentials"]
        return base

    from . import people
    content = note["content_md"] or ""
    domain = wiki_guides.domain_for_title(article_title) or ""
    # The article's own evidence base: the source notes it already cites (live ones only).
    cited = [t for t in wikilinks.extract_links(content) if not t.lower().startswith("kb/")]
    ids = []
    for t in cited:
        r = conn.execute("SELECT id FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL", (t,)).fetchone()
        if r:
            ids.append(r["id"])
    srcs = _load_sources(conn, ids)
    new_srcs = _load_sources(conn, extra_source_ids)
    items_text = "\n".join(f"[{it['id']}] ({it['kind']}, by {it['author']}) {it['body']}"
                           for it in open_items) or "(none)"
    new_block = _sources_text(new_srcs) or "(none)"
    removed_block = "\n".join(f"- [[{t}]]" for t in removed_titles) or "(none)"
    others = [t for t in (known_titles or []) if t and t != article_title][:600]
    known_block = "\n".join(others) if others else "(no other articles)"

    prompt = (prompts.get("actions.wiki_maintain", "")
              .replace("{owner}", people.owner_name(conn))
              .replace("{general_guide}", wiki_guides.guide_text(None))
              .replace("{domain_guide}", wiki_guides.guide_text(domain)).replace("{domain}", domain)
              .replace("{title}", article_title).replace("{article}", content)
              .replace("{items}", items_text).replace("{new_sources}", new_block)
              .replace("{removed_sources}", removed_block).replace("{known_titles}", known_block)
              .replace("{sources}", _sources_text(srcs)))
    try:
        revised, payload = _extract_maintain(_strip_fence(
            llm.complete([{"role": "user", "content": prompt}], max_tokens=2600)))
    except Exception as exc:  # noqa: BLE001
        base["errors"] = [f"maintain failed: {exc}"]
        return base

    revised = revised.strip()
    if not revised:
        base["errors"] = ["empty revision"]
        return base
    # Same dead-link guarantee as the writer: never save a dead link.
    allowed = {t for t in (known_titles or [])} | {article_title}
    bad = _bad_links(conn, revised, allowed)
    if bad:
        revised = _neutralize_links(revised, set(bad))
    v = wiki_guides.validate_structure(article_title, revised)
    open_ids = {it["id"] for it in open_items}
    resolved = [{"id": int(r["id"]), "how": str(r.get("how") or "")}
                for r in (payload.get("resolved") or [])
                if isinstance(r, dict) and _isint(r.get("id")) and int(r["id"]) in open_ids]
    new = [{"kind": str(n.get("kind") or "note"), "body": str(n.get("body") or "").strip()}
           for n in (payload.get("new") or []) if isinstance(n, dict) and str(n.get("body") or "").strip()]
    return {"title": article_title, "ok": v["ok"], "changed": revised != content.strip(),
            "content_md": revised, "resolved": resolved, "new": new,
            "errors": v["errors"], "warnings": v["warnings"]}


def _cited_source_len(conn, content: str) -> int:
    """Total length of the source notes a (kb) article cites — the grounding it can claim."""
    total = 0
    for t in wikilinks.extract_links(content):
        if t.lower().startswith("kb/"):
            continue
        r = conn.execute("SELECT content_md FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL",
                         (t,)).fetchone()
        if r:
            total += len(r["content_md"] or "")
    return total


def flag_ungrounded_reference(conn, ratio: float = 3.0, min_body: int = 500) -> dict:
    """Flag Reference articles whose body far outweighs their cited sources — the signature
    of the model padding with general 'common knowledge' from training instead of your
    notes. Records a todo (the worklist for an approved external fill) and returns counts.
    Forward-looking: the GROUNDING rule keeps new articles honest; this audits what's there."""
    from . import article_talk
    rows = conn.execute(
        "SELECT title, content_md FROM notes WHERE kind='kb' AND deleted_at IS NULL "
        "AND title LIKE 'kb/Reference/%'").fetchall()
    scanned = flagged = 0
    for r in rows:
        if wiki_guides.is_protected(r["title"]):
            continue
        scanned += 1
        body = r["content_md"] or ""
        core = re.split(r"\n##\s+References", body, maxsplit=1)[0]
        blen = len(re.sub(r"\s+", " ", core).strip())
        if blen < min_body:
            continue                                        # a stub is fine — that's the goal
        slen = _cited_source_len(conn, body)
        if slen == 0 or blen / slen > ratio:
            article_talk.record(conn, r["title"], [{"kind": "todo",
                "body": "External reference needed (Grokipedia) — the general content here isn't "
                        "grounded in your notes; awaiting an approved external fill."}], author="ai")
            flagged += 1
    conn.commit()
    return {"scanned": scanned, "flagged": flagged}


def _known_titles(conn) -> list[str]:
    return sorted({r["title"] for r in conn.execute(
        "SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL AND title NOT LIKE 'kb/_%'").fetchall()})


def _apply_maintain(conn, out: dict, version_note: str) -> bool:
    """Apply a maintain_one result: save the revision (versioned), resolve addressed items
    (recording HOW), record any new items. Returns True if the article content changed."""
    from . import article_talk
    from . import notes as notes_svc
    if not out["ok"]:
        return False
    changed = bool(out["changed"] and out["content_md"])
    if changed:
        notes_svc.upsert_note(conn, out["title"], out["content_md"], kind="kb", version_note=version_note)
    for r in out["resolved"]:
        article_talk.resolve_with(conn, r["id"], r["how"])
    if out["new"]:
        article_talk.record(conn, out["title"], out["new"], author="ai")
    return changed


def maintain_batch(conn, limit: int = 20) -> dict:
    """Run the maintenance pass over articles that have open talk items. Applies a valid
    revision (versioned), resolves the items the model addressed (recording HOW), and
    records any new items. Returns a summary."""
    rows = conn.execute(
        "SELECT DISTINCT t.article_title AS title FROM article_talk t "
        "WHERE t.resolved_at IS NULL AND t.kind IN ('conflict','question','todo','directive') "
        "AND EXISTS (SELECT 1 FROM notes n WHERE n.title=t.article_title AND n.kind='kb' AND n.deleted_at IS NULL) "
        "ORDER BY t.article_title LIMIT ?",
        (int(limit),)).fetchall()
    known = _known_titles(conn)
    changed = resolved = failed = 0
    for row in rows:
        out = maintain_one(conn, row["title"], known)
        if not out["ok"]:
            failed += 1
            continue
        changed += 1 if _apply_maintain(conn, out, "maintenance pass") else 0
        resolved += len(out["resolved"])
    conn.commit()
    return {"articles": len(rows), "changed": changed, "resolved": resolved, "failed": failed}


def _articles_citing(conn, note_id: int) -> set[str]:
    """kb articles that cite a given source note (via the links table)."""
    rows = conn.execute(
        "SELECT DISTINCT s.title FROM links l JOIN notes s ON s.id=l.source_note_id "
        "WHERE l.target_note_id=? AND s.kind='kb' AND s.deleted_at IS NULL", (note_id,)).fetchall()
    return {r["title"] for r in rows}


def _articles_for_note_entities(conn, note_id: int) -> set[str]:
    """kb articles whose entities this note mentions (so a new fact routes to its subject)."""
    rows = conn.execute(
        "SELECT DISTINCT e.article_title AS t FROM entity_mentions m JOIN entities e ON e.id=m.entity_id "
        "WHERE m.note_id=? AND e.article_title IS NOT NULL", (note_id,)).fetchall()
    return {r["t"] for r in rows}


_WATERMARK = "kb_incremental:since"


def update_batch(conn, limit: int = 40, new_subject_min: int = 2) -> dict:
    """Incremental update: flow notes changed since the last pass into the EXISTING KB.
    Routes each change to its articles (cite-based for edits/deletes ∪ entity-based for new
    facts), refreshes each affected article once, nudges a Review card for brand-new
    subjects that have no article yet, and advances the watermark. Additive + reconciling;
    the full rebuild remains the source of truth. Assumes changed notes are already
    analyzed + the entity index rebuilt (the recipe does that first)."""
    from ..db import get_meta, set_meta
    since = get_meta(_WATERMARK)
    if since is None:
        # First ever run: start the clock now (the full build already covered history).
        set_meta(conn, _WATERMARK, _now(conn)); conn.commit()
        return {"seeded": True, "changes": 0, "articles": 0, "changed": 0}
    if not llm.has_credentials():
        # Don't advance the watermark with no key, or changes would be skipped forever.
        return {"changes": 0, "articles": 0, "changed": 0, "skipped": "no LLM credentials"}

    changes = conn.execute(
        "SELECT id, title, COALESCE(deleted_at, updated_at) AS changed_at, "
        "(deleted_at IS NOT NULL) AS deleted FROM notes WHERE "
        "(kind='daily' OR (kind='entry' AND title NOT LIKE 'notes/daily/%')) "
        "AND COALESCE(deleted_at, updated_at) > ? ORDER BY changed_at LIMIT ?",
        (since, max(1, int(limit))),
    ).fetchall()
    if not changes:
        return {"changes": 0, "articles": 0, "changed": 0, "resolved": 0, "new_subjects": 0}

    # Route each change → affected articles, collecting new sources + removed (deleted) titles.
    affected: dict[str, dict] = {}
    orphans: list[dict] = []
    for ch in changes:
        targets = _articles_citing(conn, ch["id"])
        if not ch["deleted"]:
            targets |= _articles_for_note_entities(conn, ch["id"])
        if not targets:
            if not ch["deleted"]:
                orphans.append(dict(ch))                     # a new/edited subject with no article
            continue
        for t in targets:
            slot = affected.setdefault(t, {"new": set(), "removed": set()})
            if ch["deleted"]:
                slot["removed"].add(ch["title"])
            else:
                slot["new"].add(ch["id"])

    known = _known_titles(conn)
    changed = resolved = failed = 0
    for title, slot in affected.items():
        out = maintain_one(conn, title, known,
                           extra_source_ids=list(slot["new"]), removed_titles=list(slot["removed"]))
        if not out["ok"]:
            failed += 1
            continue
        changed += 1 if _apply_maintain(conn, out, "incremental update") else 0
        resolved += len(out["resolved"])

    # Nudge: brand-new subjects with no article yet (creation is deferred to the full rebuild).
    nudged = _nudge_new_subjects(conn, orphans, new_subject_min)

    set_meta(conn, _WATERMARK, changes[-1]["changed_at"])    # advance past everything processed
    conn.commit()
    return {"changes": len(changes), "articles": len(affected), "changed": changed,
            "resolved": resolved, "failed": failed, "new_subjects": nudged}


def _nudge_new_subjects(conn, orphans: list[dict], min_notes: int) -> int:
    """One Review card per recurring subject (≥ min_notes notes) that changed but has no
    article, so the owner can fold it in at the next rebuild. De-duped against pending cards."""
    from . import reviews as reviews_svc
    tally: dict[tuple, int] = {}
    for ch in orphans:
        for r in conn.execute(
            "SELECT e.type, e.canonical_name FROM entity_mentions m JOIN entities e ON e.id=m.entity_id "
            "WHERE m.note_id=? AND e.article_title IS NULL", (ch["id"],)).fetchall():
            key = (r["type"], r["canonical_name"])
            tally[key] = tally.get(key, 0) + 1
    posted = 0
    for (typ, name), c in tally.items():
        if c < int(min_notes):
            continue
        title = f"New subject: {name}"
        if conn.execute("SELECT 1 FROM review_items WHERE title=? AND status='pending'", (title,)).fetchone():
            continue
        reviews_svc.create_review_item(
            conn, None, title=title,
            message=f"{c} note(s) mention {name} ({typ}) but it has no article yet — add it on the next rebuild.")
        posted += 1
    return posted


def _now(conn) -> str:
    return conn.execute("SELECT datetime('now') AS n").fetchone()["n"]


def write_batch(conn, articles: list[dict], instructions: str | None = None, on_article=None) -> dict:
    """Write every article; split valid (saved by the recipe) vs quarantined (failed
    the structure lint — surfaced, not saved). Mirrors validate_citations' shape.
    `on_article(index, total, title)` is called before each write so the run modal can
    show which article is currently being written."""
    # The titles writers may cross-link: every planned article PLUS any live kb article
    # that survives (so links resolve when adding to an existing KB, not just full rebuild).
    planned = [str(a.get("title") or "").strip() for a in articles]
    existing = [r["title"] for r in conn.execute(
        "SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL AND title NOT LIKE 'kb/_%'").fetchall()]
    known = sorted({t for t in planned + existing if t})
    valid, quarantined = [], []
    total = len(articles)
    for i, art in enumerate(articles, 1):
        if on_article:
            on_article(i, total, str(art.get("title") or "").strip())
        d = write_one(conn, art, instructions, known_titles=known)
        (valid if d["ok"] and d["content_md"] else quarantined).append(d)
    # A human-readable reason per dropped article, so the review card explains WHY each
    # was quarantined (e.g. "no source notes resolved") — not just that it was.
    report = " | ".join(
        f"{d['title']} — {'; '.join(d['errors']) or ('stub' if d['stub'] else 'empty draft')}"
        for d in quarantined)
    return {"valid": valid, "quarantined": quarantined,
            "count": len(articles), "bad": len(quarantined), "report": report}
