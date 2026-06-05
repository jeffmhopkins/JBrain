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
    # Scope floor: drop any article with no source note — it isn't grounded in your notes
    # (a >1-hop / general-knowledge article). Keeps the wiki to "1 hop out from the notes".
    grounded = [a for a in articles if a["sources"]]
    dropped = len(articles) - len(grounded)
    return {"articles": grounded, "index_md": build_index_md(grounded), "dropped": dropped}


def augment_sources(conn, articles: list[dict], *, per_article: int = 5,
                    max_distance: float = 0.85) -> dict:
    """PRR "retrieve per section": after the outline names each article and its scope,
    run ONE focused semantic search per article to pull in source notes the survey /
    entity assignment missed, then UNION them onto the article's existing sources.

    Deliberately conservative so it adds signal, not noise:
      - searches only 'entry'/'daily' notes — never other kb articles (an article must
        not end up citing the wiki itself);
      - a relevance floor (max_distance) so the KNN can't inject off-topic notes just
        because it always returns SOMETHING;
      - at most `per_article` additions, and existing (outline-assigned) sources are
        never dropped — only added to.
    Local embeddings only — no LLM call, so the whole pass is near-free. Returns the
    same articles list (mutated in place) plus a count of sources added, for the card."""
    from . import embeddings
    added_total = 0
    for art in articles:
        existing = {int(i) for i in (art.get("sources") or [])}
        leaf = str(art.get("title") or "").split("/")[-1]
        seed = f"{leaf} — {art.get('scope', '')}".strip(" —")
        if not seed:
            continue
        # 'entry' and 'daily' are both first-class sources (raw captures reach the KB via
        # their daily rollup); pool both and take the globally NEAREST, so neither kind is
        # arbitrarily preferred over the other.
        cand: list[dict] = []
        for kind in ("entry", "daily"):
            try:
                cand += embeddings.semantic_search(conn, seed, limit=per_article * 3,
                                                   kind=kind, max_distance=max_distance)
            except Exception:  # noqa: BLE001 — a retrieval miss must never break the build
                pass
        cand.sort(key=lambda h: h["distance"])
        additions: list[int] = []
        for h in cand:
            nid = int(h["id"])
            if nid not in existing and nid not in additions:
                additions.append(nid)
                if len(additions) >= per_article:
                    break
        if additions:
            art["sources"] = sorted(existing | set(additions))
            added_total += len(additions)
    return {"articles": articles, "added": added_total}


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
    """Remove dead links so they can't reach a saved article. A dead link in a FOOTNOTE
    DEFINITION (a citation to a now-missing source) drops the whole definition line — and
    any inline [^marker] left without a definition is then stripped — rather than leaving a
    mangled '[^s1]: Ghost — DATE'. Dead links in PROSE unwrap to plain text. Live links and
    valid footnotes are untouched."""
    def repl(m):
        target = m.group(1).strip()
        if target not in bad:
            return m.group(0)
        inner = m.group(0)[2:-2]
        return inner.split("|", 1)[1].strip() if "|" in inner else target.split("/")[-1]

    kept = []
    for line in content.split("\n"):
        if re.match(r"^\s*\[\^[^\]]+\]:", line) and any(b in line for b in bad):
            continue                                        # a citation to a dead source → drop the footnote
        kept.append(line)
    out = wikilinks.WIKILINK_RE.sub(repl, "\n".join(kept))
    defined = set(re.findall(r"(?m)^\s*\[\^([^\]]+)\]:", out))
    # Strip inline footnote markers whose definition we just removed.
    return re.sub(r"\[\^([^\]]+)\]", lambda m: m.group(0) if m.group(1) in defined else "", out)


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
    others = scoped_known_titles(conn, title, known_titles)
    known_block = "\n".join(others) if others else "(no other articles yet)"
    # Per-article guidance (e.g. open directives carried in by rebuild_article). Empty for
    # an ordinary build. Without the placeholder in the prompt this was silently ignored.
    guidance = f"\nADDITIONAL GUIDANCE — follow this too:\n{instructions.strip()}\n" if (instructions or "").strip() else ""
    prompt = (prompts.get("actions.wiki_write", "")
              .replace("{owner}", owner)
              .replace("{general_guide}", general).replace("{domain_guide}", dguide)
              .replace("{domain}", domain or "").replace("{title}", title)
              .replace("{known_titles}", known_block).replace("{instructions}", guidance)
              .replace("{scope}", scope).replace("{sources}", _sources_text(srcs)))
    try:
        draft, talk = _extract_talk(_strip_fence(llm.complete([{"role": "user", "content": prompt}], max_tokens=2200)))
    except Exception as exc:  # noqa: BLE001
        base["errors"] = [f"write failed: {exc}"]
        return base

    allowed = {t for t in (known_titles or [])} | {title}
    v = wiki_guides.validate_structure(title, draft)
    bad = _bad_links(conn, draft, allowed)
    # Bounded revise loop (§10 gate model: fail → a bounded, NON-REGRESSING revise). Up to
    # two passes; a pass is kept only if it doesn't regress the lint/links, and we only take
    # a second pass when the first STRICTLY improved — so it converges and can't oscillate.
    for _ in range(2):
        if not (((v["errors"] or v["warnings"]) and not v["stub"]) or bad):
            break
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
        except Exception as exc:  # noqa: BLE001
            log.info("wiki_revise failed for %s: %s", title, exc)
            break
        v2 = wiki_guides.validate_structure(title, revised)
        bad2 = _bad_links(conn, revised, allowed)
        prev, cur = len(v["errors"]) + len(bad), len(v2["errors"]) + len(bad2)
        if cur <= prev:                                 # non-regressing → adopt the revision
            draft, v, talk, bad = revised, v2, (rtalk or talk), bad2
        if cur >= prev:                                 # no strict improvement → stop looping
            break

    # Backstop guarantee: whatever the model left, no dead link survives into the saved
    # article — unwrap it to plain text and note it on the article's talk.
    if bad:
        draft = _neutralize_links(draft, set(bad))
        talk = list(talk) + [{"kind": "note",
                              "body": f"Unlinked dead reference [[{t}]] — no such article; kept as plain text."}
                             for t in bad]

    # Heading guarantee: the H1 is the display name, never the leaked kb/ path.
    draft = wiki_guides.fix_article_h1(title, draft)
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
    others = scoped_known_titles(conn, article_title, known_titles)
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
    # Bounded self-critique/revise loop against the lint (mirrors write_one's §10 gate):
    # up to two non-regressing passes, second only on strict improvement — so a structural
    # slip (e.g. a deletion-purge that dropped a required section) gets a couple of chances
    # to recover instead of silently no-op'ing, without oscillating.
    for _ in range(2):
        if not ((v["errors"] or v["warnings"]) and not v["stub"]):
            break
        issues = "\n".join(f"- {x}" for x in (v["errors"] + v["warnings"]))
        rprompt = (prompts.get("actions.wiki_revise", "")
                   .replace("{issues}", issues).replace("{general_guide}", wiki_guides.guide_text(None))
                   .replace("{domain_guide}", wiki_guides.guide_text(domain)).replace("{domain}", domain)
                   .replace("{known_titles}", known_block).replace("{draft}", revised))
        try:
            r2, _ = _extract_maintain(_strip_fence(
                llm.complete([{"role": "user", "content": rprompt}], max_tokens=2600)))
        except Exception as exc:  # noqa: BLE001
            log.info("wiki_maintain revise failed for %s: %s", article_title, exc)
            break
        r2 = r2.strip()
        b2 = _bad_links(conn, r2, allowed)
        if b2:
            r2 = _neutralize_links(r2, set(b2))
        v2 = wiki_guides.validate_structure(article_title, r2)
        prev, cur = len(v["errors"]), len(v2["errors"])
        if r2 and cur <= prev:
            revised, v = r2, v2
        if not r2 or cur >= prev:
            break
    revised = wiki_guides.fix_article_h1(article_title, revised)   # H1 = display name, not kb/ path
    by_id = {it["id"]: it for it in open_items}
    resolved = [{"id": int(r["id"]), "kind": by_id[int(r["id"])]["kind"],
                 "outcome": str(r.get("outcome") or "").strip().lower(),
                 "how": str(r.get("how") or "")}
                for r in (payload.get("resolved") or [])
                if isinstance(r, dict) and _isint(r.get("id")) and int(r["id"]) in by_id]
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
    # Reclassify any flags left as 'todo' by an earlier build so they stop driving maintenance.
    conn.execute("UPDATE article_talk SET kind='note' WHERE kind='todo' AND resolved_at IS NULL "
                 "AND body LIKE 'External reference needed%'")
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
            article_talk.record(conn, r["title"], [{"kind": "note",
                "body": "External reference needed (Grokipedia) — the general content here isn't "
                        "grounded in your notes; awaiting an approved external fill."}], author="ai")
            flagged += 1
    conn.commit()
    return {"scanned": scanned, "flagged": flagged}


def _known_titles(conn) -> list[str]:
    return sorted({r["title"] for r in conn.execute(
        r"SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL AND title NOT LIKE 'kb/\_%' ESCAPE '\'").fetchall()})


def scoped_known_titles(conn, title: str, all_titles, budget: int = 600) -> list[str]:
    """Relevant cross-link candidates for `title`, replacing the old blind alphabetical
    `[:600]` cap. When the whole KB fits in `budget` we return everything (no need to
    scope). Past it, we PRIORITISE a relevant neighbourhood — articles that link to this
    one (backlinks), this article's own current link targets, and same-folder siblings —
    then top up to `budget`, so we never offer FEWER candidates than the old cap, but the
    ones we keep when truncating are the relevant ones (the alphabetical slice dropped
    everything after ~'kb/R…' regardless of relevance). Deterministic, no LLM."""
    others = [t for t in (all_titles or []) if t and t != title]
    if len(others) <= budget:
        return others
    others_set = set(others)
    keep: set[str] = set()
    # Backlinks: kb articles that link TO this title (target_title survives soft-delete).
    for r in conn.execute(
        "SELECT DISTINCT s.title FROM links l JOIN notes s ON s.id=l.source_note_id "
        "WHERE lower(l.target_title)=lower(?) AND s.kind='kb' AND s.deleted_at IS NULL", (title,)):
        keep.add(r["title"])
    # This article's own outward kb link targets (co-relevant neighbours it already cites).
    row = conn.execute("SELECT id FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL",
                       (title,)).fetchone()
    if row:
        for r in conn.execute(
            "SELECT DISTINCT t.title FROM links l JOIN notes t ON t.id=l.target_note_id "
            "WHERE l.source_note_id=? AND t.kind='kb' AND t.deleted_at IS NULL", (row["id"],)):
            keep.add(r["title"])
    # Same-folder siblings (shared kb/<Domain>/<Sub>/ parent prefix).
    if "/" in title:
        parent = title.rsplit("/", 1)[0]
        for r in conn.execute(
            "SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL AND title LIKE ?",
            (parent + "/%",)):
            keep.add(r["title"])
    keep &= others_set
    keep.discard(title)
    result = sorted(keep)[:budget]
    if len(result) < budget:                       # top up with the rest so count >= old cap
        seen = set(result)
        for t in others:
            if t not in seen:
                result.append(t)
                if len(result) >= budget:
                    break
    return result


# ---- KB write lock (advisory, atomic claim) ------------------------------------------
def kb_lock_acquire(conn, key: str = "kb_write", ttl_s: int = 1800) -> bool:
    """Claim the KB write lock so manual ops, the scrub, and nightly jobs can't interleave
    mutations across separate connections. Atomic: INSERT OR IGNORE on a unique key — exactly
    one caller wins. A holder older than ttl_s is reclaimed so a crash can't wedge the KB."""
    conn.execute("CREATE TABLE IF NOT EXISTS kb_locks (key TEXT PRIMARY KEY, held_at INTEGER NOT NULL)")
    conn.execute("DELETE FROM kb_locks WHERE key=? AND held_at < CAST(strftime('%s','now') AS INTEGER) - ?",
                 (key, int(ttl_s)))
    cur = conn.execute(
        "INSERT OR IGNORE INTO kb_locks(key, held_at) VALUES (?, CAST(strftime('%s','now') AS INTEGER))", (key,))
    conn.commit()
    return cur.rowcount == 1


def kb_lock_release(conn, key: str = "kb_write") -> None:
    conn.execute("DELETE FROM kb_locks WHERE key=?", (key,))
    conn.commit()


def rebuild_article(conn, title: str, instructions: str | None = None) -> dict:
    """Regenerate ONE existing kb article from scratch from its PRIMARY SOURCES (the owner's
    notes), then re-link it into the KB. REGENERATE-IN-PLACE, never a wipe: it revives the
    SAME row, so slug + version history + inbound links + the AI-talk ledger all survive.
    Sources = the article's prior citations ∪ the entity index for its subject (search is
    never a seed). Open directives/conflicts are carried into the writer. On a quarantine
    (lint fail) the prior version is restored and an open todo is recorded — a failed rebuild
    never leaves a hole. Runs under the KB write lock. Returns {ok, title, reason?, quarantined?}."""
    from . import notes as notes_svc, entity_index, article_talk
    title = (title or "").strip()
    note = notes_svc.get_by_title(conn, title)
    if not note or note["kind"] != "kb":
        return {"ok": False, "title": title, "reason": "no such kb article"}
    if not kb_lock_acquire(conn):
        return {"ok": False, "title": title,
                "reason": "KB is busy (another build/maintain is running) — try again shortly"}
    try:
        nid = note["id"]
        prior = note["content_md"] or ""
        # 1. Primary sources: prior citations ∪ entity index for the subject. No search seed.
        ids: set[int] = set()
        for t in wikilinks.extract_links(prior):
            if t.lower().startswith("kb/"):
                continue
            r = conn.execute("SELECT id FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL",
                             (t,)).fetchone()
            if r:
                ids.add(int(r["id"]))
        ids |= {int(i) for i in entity_index.note_ids_for_name(conn, title.rsplit("/", 1)[-1])}
        # 2. Carry OPEN directives/conflicts into the writer so the rebuild doesn't drop them.
        opens = article_talk.open_for(conn, title)
        directives = "; ".join(o["body"] for o in opens
                               if o.get("kind") in ("directive", "conflict") and o.get("body"))
        instr = (instructions or "").strip()
        if directives:
            instr = (instr + "\nHonor these standing directives / unresolved conflicts: " + directives).strip()
        scope = prior.splitlines()[0].lstrip("# ").strip() if prior.strip() else ""
        art = {"title": title, "domain": wiki_guides.domain_for_title(title),
               "scope": scope, "sources": sorted(ids)}
        # 3. Soft-delete this one article, regenerate, revive the SAME row on success.
        notes_svc.soft_delete(conn, nid)
        out = write_one(conn, art, instr, known_titles=_known_titles(conn))
        if out.get("ok") and out.get("content_md"):
            notes_svc.upsert_note(conn, title, out["content_md"], kind="kb",
                                  source="rebuild", version_note="rebuilt from sources")
            # soft_delete nulled inbound links' target_note_id; re-point them at the revived
            # row so other articles' [[links]] to this one reconnect (as restore() does).
            wikilinks.resolve_dangling_links(conn, nid, title)
            if out.get("talk"):
                article_talk.record(conn, title, out["talk"])
            entity_index.rebuild(conn)                 # relink entity → fresh article
            entity_index.write_disambiguation_pages(conn)
            flag_dead_links(conn)                      # sweep any dangling cross-links
            conn.commit()
            return {"ok": True, "title": title}
        # Quarantine: restore the prior version (never leave a hole) + surface an open todo.
        notes_svc.restore(conn, nid)
        reason = "; ".join(out.get("errors") or []) or "structure lint failed"
        article_talk.add(conn, title, "todo", f"Rebuild quarantined ({reason}) — manual review.")
        conn.commit()
        return {"ok": False, "title": title, "quarantined": True, "reason": reason}
    finally:
        kb_lock_release(conn)


# Common short/general leaves we never auto-link (too many false positives as bare words).
_STOP_LEAVES = {
    "home", "work", "car", "list", "notes", "note", "care", "team", "group", "family",
    "house", "office", "school", "money", "health", "food", "travel", "today", "people",
}


def _mask_spans(text: str) -> list[tuple[int, int]]:
    """Char ranges where a link must NOT be inserted: fenced code, inline code, existing
    [[wikilinks]], and footnote-definition lines (`[^sN]: …` — citations)."""
    spans: list[tuple[int, int]] = []
    for pat, flags in ((r"```.*?```", re.DOTALL), (r"`[^`]+`", 0),
                       (r"\[\[.*?\]\]", 0), (r"(?m)^\[\^[^\]]+\]:.*$", 0)):
        spans += [m.span() for m in re.finditer(pat, text, flags)]
    return spans


def check_needed_links(conn, title: str | None = None, mode: str = "propose") -> dict:
    """Deterministic ADD-link backstop (the complement to flag_dead_links's remove): find
    mentions in an article that EXACTLY match an existing kb article's leaf name but aren't
    linked, and link them. Refuses ambiguous leaves (map to ≥2 articles, or in the entity
    index's ambiguous_terms) and short/common-word leaves; masks code, existing links, and
    footnote-citation lines. mode='propose' (default) returns proposals for a Review card;
    mode='auto' writes them (versioned). No See-also, no reciprocal edits. No 600 cap."""
    from . import notes as notes_svc, entity_index
    titles = _known_titles(conn)
    leafmap: dict[str, list[str]] = {}
    for t in titles:
        leafmap.setdefault(t.split("/")[-1].strip().lower(), []).append(t)
    ambiguous = {k for k, v in leafmap.items() if len(v) > 1}
    try:
        for amb in entity_index.ambiguous_terms(conn):
            ambiguous.add(str(amb.get("term", "")).lower())
    except Exception:  # noqa: BLE001
        pass

    def linkable(leaf_lower: str, leaf: str) -> bool:
        if leaf_lower in ambiguous or len(leaf) < 4:
            return False
        if " " not in leaf and leaf_lower in _STOP_LEAVES:
            return False
        return True

    targets = [title] if title else titles
    out_articles = []
    for tt in targets:
        note = notes_svc.get_by_title(conn, tt)
        if not note or note["kind"] != "kb":
            continue
        body = note["content_md"] or ""
        spans = _mask_spans(body)
        linked = {x.lower() for x in wikilinks.extract_links(body)}
        props = []
        for leaf_lower, cands in leafmap.items():
            tgt = cands[0]
            if tgt == tt or tgt.lower() in linked:
                continue
            leaf = tgt.split("/")[-1]
            if not linkable(leaf_lower, leaf):
                continue
            for m in re.finditer(r"(?<!\w)" + re.escape(leaf) + r"(?!\w)", body, re.IGNORECASE):
                if not any(s <= m.start() < e for s, e in spans):
                    props.append({"target": tgt, "surface": m.group(0), "at": m.start()})
                    break
        if mode == "auto" and props:
            nb = body
            for p in sorted(props, key=lambda x: -x["at"]):      # right-to-left preserves offsets
                s, e = p["at"], p["at"] + len(p["surface"])
                nb = nb[:s] + f"[[{p['target']}|{p['surface']}]]" + nb[e:]
            notes_svc.upsert_note(conn, tt, nb, kind="kb", source="user",
                                  version_note="added missing cross-links")
        out_articles.append({"title": tt,
                             "proposals": [{"target": p["target"], "surface": p["surface"]} for p in props]})
    if mode == "auto":
        conn.commit()
    return {"ok": True, "articles": out_articles,
            "count": sum(len(a["proposals"]) for a in out_articles)}


_TYPE_DOMAIN = {"person": "People", "animal": "People", "org": "Groups", "place": "Places",
                "thing": "Things", "work": "Things",
                "condition": "Reference", "medication": "Reference", "procedure": "Reference",
                "concept": "Reference", "event": "Reference"}
_REF_SUB = {"condition": "Medicine/Conditions", "medication": "Medicine/Medications",
            "procedure": "Medicine/Procedures", "event": "Events"}


def create_article(conn, subject: str, etype: str | None = None, min_notes: int = 2) -> dict:
    """Create ONE new-subject kb article from its notes — what the incremental loop used to
    defer to the full rebuild. DEDUP BEFORE SPAWN: if the subject's canonical entity already
    has an article, or an existing kb leaf normalises-equal, route there (fold) instead of
    minting a near-duplicate. Spawns only a subject with ≥ min_notes notes (else fold, don't
    stub). Picks kb/<Domain>/<Name> (Reference is foldered), writes from sources, relinks.
    Runs under the KB write lock. Returns {ok, created?|folded?, title?, reason?}."""
    from . import notes as notes_svc, entity_index, article_talk
    subject = (subject or "").strip()
    norm = entity_index.normalize(subject)
    if not norm:
        return {"ok": False, "reason": "empty subject"}
    ent = conn.execute("SELECT id, type, canonical_name, article_title FROM entities "
                       "WHERE normalized_key=? ORDER BY note_count DESC LIMIT 1", (norm,)).fetchone()
    if ent and ent["article_title"]:
        return {"ok": True, "folded": True, "title": ent["article_title"], "reason": "article already exists"}
    for r in conn.execute("SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL"):
        if entity_index.normalize(r["title"].split("/")[-1]) == norm:
            return {"ok": True, "folded": True, "title": r["title"], "reason": "near-duplicate title exists"}
    ids = entity_index.note_ids_for_name(conn, subject)
    if len(ids) < int(min_notes):
        return {"ok": False, "reason": f"only {len(ids)} note(s) — fold into a related article, don't spawn a stub"}
    name = (ent["canonical_name"] if ent else subject).strip().replace("/", " ").strip()
    typ = (etype or (ent["type"] if ent else "") or "").lower()
    domain = _TYPE_DOMAIN.get(typ, "Reference")
    title = (f"kb/Reference/{_REF_SUB.get(typ, 'Concepts')}/{name}" if domain == "Reference"
             else f"kb/{domain}/{name}")
    if conn.execute("SELECT 1 FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL", (title,)).fetchone():
        return {"ok": True, "folded": True, "title": title, "reason": "title already exists"}
    if not kb_lock_acquire(conn):
        return {"ok": False, "reason": "KB busy — try again shortly"}
    try:
        art = {"title": title, "domain": domain, "scope": "", "sources": sorted(ids)}
        out = write_one(conn, art, None, known_titles=_known_titles(conn))
        if out.get("ok") and out.get("content_md"):
            notes_svc.upsert_note(conn, title, out["content_md"], kind="kb",
                                  source="create", version_note="created from sources")
            if out.get("talk"):
                article_talk.record(conn, title, out["talk"])
            entity_index.rebuild(conn)            # point the entity at its new article
            flag_dead_links(conn)
            conn.commit()
            return {"ok": True, "created": True, "title": title}
        conn.commit()
        return {"ok": False, "title": title, "reason": "; ".join(out.get("errors") or []) or "structure lint failed"}
    finally:
        kb_lock_release(conn)


def refresh_index(conn) -> int:
    """Rebuild kb/_index from the live (non-protected) articles. No LLM. Run after any
    structural op (create/merge/rename) so the org map can't go stale or dangle."""
    from . import notes as notes_svc
    arts = [{"title": r["title"], "domain": wiki_guides.domain_for_title(r["title"]) or "", "scope": ""}
            for r in conn.execute(
                r"SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL "
                r"AND title NOT LIKE 'kb/\_%' ESCAPE '\' ORDER BY title")]
    notes_svc.upsert_note(conn, "kb/_index", build_index_md(arts), kind="kb",
                          source="index", version_note="index refreshed")
    return len(arts)


def _structure_log(conn, op: str, pair_key: str) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS kb_structure_log (op TEXT, pair_key TEXT, at INTEGER)")
    conn.execute("INSERT INTO kb_structure_log(op, pair_key, at) VALUES (?, ?, CAST(strftime('%s','now') AS INTEGER))",
                 (op, pair_key))


def _structure_recent(conn, op: str, pair_key: str, k_days: int = 3) -> bool:
    conn.execute("CREATE TABLE IF NOT EXISTS kb_structure_log (op TEXT, pair_key TEXT, at INTEGER)")
    return conn.execute(
        "SELECT 1 FROM kb_structure_log WHERE op=? AND pair_key=? "
        "AND at > CAST(strftime('%s','now') AS INTEGER) - ? LIMIT 1",
        (op, pair_key, int(k_days) * 86400)).fetchone() is not None


def recategorize_article(conn, title: str, new_title: str) -> dict:
    """Move/rename a kb article (e.g. fold it into a subcategory). Reuses upsert_note's
    rename path, which rewrites inbound [[old]]→[[new]] links itself (never relies on the
    dead-link sweep, which would unwrap them). Refreshes the index. Under the KB lock."""
    from . import notes as notes_svc, entity_index
    title, new_title = (title or "").strip(), (new_title or "").strip()
    note = notes_svc.get_by_title(conn, title)
    if not note or note["kind"] != "kb":
        return {"ok": False, "reason": "no such kb article"}
    if not new_title.startswith("kb/") or new_title.lower().startswith("kb/_"):
        return {"ok": False, "reason": "new title must be a normal kb/… path"}
    if notes_svc.get_by_title(conn, new_title):
        return {"ok": False, "reason": "target title already exists"}
    if not kb_lock_acquire(conn):
        return {"ok": False, "reason": "KB busy — try again shortly"}
    try:
        notes_svc.upsert_note(conn, new_title, note["content_md"], note_id=note["id"], kind="kb",
                              source="recategorize", version_note=f"recategorized from {title}")
        entity_index.rebuild(conn)
        flag_dead_links(conn)
        refresh_index(conn)
        conn.commit()
        return {"ok": True, "from": title, "to": new_title}
    finally:
        kb_lock_release(conn)


def merge_articles(conn, sources: list[str], into: str) -> dict:
    """Fold one or more kb articles INTO another: rewrite the merged article from the union
    of all their source notes, soft-delete the sources, and rewrite every inbound
    [[source]]→[[into]] link (via _rename_inbound_links — NOT the dead-link sweep, which
    would unwrap them and lose the connection). Inverse-pair hysteresis blocks merging
    titles a recent split produced. Under the KB lock."""
    from . import notes as notes_svc, entity_index, article_talk
    into = (into or "").strip()
    sources = [s.strip() for s in (sources or []) if s and s.strip() and s.strip() != into]
    if not into or not sources:
        return {"ok": False, "reason": "need source(s) + a distinct `into`"}
    if _structure_recent(conn, "split", "|".join(sorted(sources))):
        article_talk.add(conn, into, "todo",
                         f"Merge of {sources} blocked — they were split apart recently; review by hand.")
        return {"ok": False, "reason": "blocked by hysteresis (recently split)"}
    by = {t: notes_svc.get_by_title(conn, t) for t in [into] + sources}
    if not by[into] or by[into]["kind"] != "kb":
        return {"ok": False, "reason": "`into` is not a kb article"}
    if not kb_lock_acquire(conn):
        return {"ok": False, "reason": "KB busy — try again shortly"}
    try:
        ids: set[int] = set()
        for t, n in by.items():
            if not n:
                continue
            for x in wikilinks.extract_links(n["content_md"] or ""):
                if not x.lower().startswith("kb/"):
                    r = conn.execute("SELECT id FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL",
                                     (x,)).fetchone()
                    if r:
                        ids.add(int(r["id"]))
            ids |= {int(i) for i in entity_index.note_ids_for_name(conn, t.rsplit("/", 1)[-1])}
        out = write_one(conn, {"title": into, "domain": wiki_guides.domain_for_title(into),
                               "scope": "", "sources": sorted(ids)}, None, known_titles=_known_titles(conn))
        if not (out.get("ok") and out.get("content_md")):
            conn.commit()
            return {"ok": False, "reason": "; ".join(out.get("errors") or []) or "merged draft failed the lint"}
        into_id = by[into]["id"]
        notes_svc.upsert_note(conn, into, out["content_md"], note_id=into_id, kind="kb",
                              source="merge", version_note=f"merged {', '.join(sources)}")
        wikilinks.resolve_dangling_links(conn, into_id, into)
        for s in sources:
            sn = by[s]
            if not sn:
                continue
            notes_svc.soft_delete(conn, sn["id"])
            notes_svc._rename_inbound_links(conn, s, into, into_id)   # [[old]]→[[into]], never unwrap
        if out.get("talk"):
            article_talk.record(conn, into, out["talk"])
        _structure_log(conn, "merge", "|".join(sorted(sources)))
        entity_index.rebuild(conn)
        flag_dead_links(conn)
        refresh_index(conn)
        conn.commit()
        return {"ok": True, "into": into, "merged": sources}
    finally:
        kb_lock_release(conn)


_RESEARCH_TAU = 0.55


def research_article(conn, title: str, mode: str = "propose") -> dict:
    """Surface EXISTING kb/Reference/* articles related to this article's salient concepts by
    MEANING (embeddings) — catching relationships check_needed_links misses because the body
    never names the leaf. Gauntlet-constrained: PROPOSE-ONLY (never auto-writes, never adds
    prose — the owner's accept/reject is the non-colluding adversary). A candidate must be
    embedding-near (distance < tau) AND corroborated by the deterministic neighbourhood
    (shared entity/citation/sibling) — embedding-alone nomination is the sprawl we refuse.
    Returns {ok, proposals:[{target, why}]}."""
    from . import notes as notes_svc, embeddings
    note = notes_svc.get_by_title(conn, title)
    if not note or note["kind"] != "kb":
        return {"ok": False, "reason": "no such kb article"}
    body = note["content_md"] or ""
    lines = body.splitlines()
    query = " ".join(([lines[0]] if lines else []) + [ln for ln in lines if ln.startswith("##")]).strip()[:600] \
        or title.rsplit("/", 1)[-1]
    nominated: dict[str, float] = {}
    try:
        for hit in embeddings.semantic_search(conn, query, 25):
            t = hit.get("title") or ""
            if t.startswith("kb/Reference/") and t != title and float(hit.get("distance", 1.0)) < _RESEARCH_TAU:
                nominated[t] = float(hit.get("distance", 1.0))
    except Exception:  # noqa: BLE001
        pass
    neigh = set(scoped_known_titles(conn, title, _known_titles(conn), budget=100000))
    linked = {x.lower() for x in wikilinks.extract_links(body)}
    candidates = [t for t in nominated if t in neigh and t.lower() not in linked]   # corroborated
    if not candidates:
        return {"ok": True, "proposals": []}
    proposals = list(candidates)
    if llm.has_credentials():                       # adjudicate, fed candidate LEADS (new info)
        leads = []
        for t in candidates[:12]:
            r = conn.execute("SELECT content_md FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL",
                             (t,)).fetchone()
            head = (r["content_md"].splitlines()[0] if r and r["content_md"] else "")
            leads.append(f"- {t}: {head[:160]}")
        prompt = (prompts.get("actions.research_article", "")
                  .replace("{title}", title).replace("{salient}", query)
                  .replace("{candidates}", "\n".join(leads)))
        try:
            picked = json.loads(_strip_fence(llm.complete([{"role": "user", "content": prompt}], max_tokens=400)))
            chosen = {p.get("reference_title") for p in picked if isinstance(p, dict)}
            proposals = [t for t in candidates if t in chosen]      # validate membership (anti-hallucination)
        except Exception:  # noqa: BLE001
            proposals = list(candidates)
    return {"ok": True, "proposals": [{"target": t} for t in proposals]}


def split_article(conn, parent: str, child: str, child_sources: list[str]) -> dict:
    """Spin a child article OFF a parent: write `child` from the given source notes, then
    re-write the parent from its REMAINING sources. Explicit partition (the owner or a
    restructure hint says which notes go to the child) — deterministic, no auto-partition.
    Inverse-pair hysteresis blocks splitting a pair a recent merge produced. Under the lock."""
    from . import notes as notes_svc, entity_index, article_talk
    parent, child = (parent or "").strip(), (child or "").strip()
    pnote = notes_svc.get_by_title(conn, parent)
    if not pnote or pnote["kind"] != "kb":
        return {"ok": False, "reason": "no such parent kb article"}
    if not child.startswith("kb/") or child.lower().startswith("kb/_") or notes_svc.get_by_title(conn, child):
        return {"ok": False, "reason": "child must be a new kb/… title"}
    pair = "|".join(sorted([parent, child]))
    if _structure_recent(conn, "merge", pair):
        article_talk.add(conn, parent, "todo", f"Split of {child} blocked — recently merged; review by hand.")
        return {"ok": False, "reason": "blocked by hysteresis (recently merged)"}
    child_ids: set[int] = set()
    for t in (child_sources or []):
        r = conn.execute("SELECT id FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL", (t,)).fetchone()
        if r:
            child_ids.add(int(r["id"]))
    if not child_ids:
        return {"ok": False, "reason": "no child source notes resolved"}
    parent_ids: set[int] = set()
    for x in wikilinks.extract_links(pnote["content_md"] or ""):
        if not x.lower().startswith("kb/"):
            r = conn.execute("SELECT id FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL", (x,)).fetchone()
            if r:
                parent_ids.add(int(r["id"]))
    parent_ids |= {int(i) for i in entity_index.note_ids_for_name(conn, parent.rsplit("/", 1)[-1])}
    remaining = sorted(parent_ids - child_ids)
    if not kb_lock_acquire(conn):
        return {"ok": False, "reason": "KB busy — try again shortly"}
    try:
        cout = write_one(conn, {"title": child, "domain": wiki_guides.domain_for_title(child),
                                "scope": "", "sources": sorted(child_ids)}, None, known_titles=_known_titles(conn))
        if not (cout.get("ok") and cout.get("content_md")):
            conn.commit()
            return {"ok": False, "reason": "; ".join(cout.get("errors") or []) or "child draft failed the lint"}
        notes_svc.upsert_note(conn, child, cout["content_md"], kind="kb", source="split",
                              version_note=f"split from {parent}")
        if cout.get("talk"):
            article_talk.record(conn, child, cout["talk"])
        if remaining:                                  # trim the parent to its remaining sources
            pout = write_one(conn, {"title": parent, "domain": wiki_guides.domain_for_title(parent),
                                    "scope": "", "sources": remaining}, None, known_titles=_known_titles(conn))
            if pout.get("ok") and pout.get("content_md"):
                notes_svc.upsert_note(conn, parent, pout["content_md"], note_id=pnote["id"], kind="kb",
                                      source="split", version_note=f"trimmed — split out {child}")
        _structure_log(conn, "split", pair)
        entity_index.rebuild(conn)
        flag_dead_links(conn)
        refresh_index(conn)
        conn.commit()
        return {"ok": True, "parent": parent, "child": child}
    finally:
        kb_lock_release(conn)


# An item CLOSES only on a genuine settlement outcome; anything else (incl. a missing or
# unrecognized outcome) leaves it OPEN. answered/applied/reconciled also require the article
# to have actually changed this pass — a claimed fix with no edit is downgraded and kept open
# so "no source / no edit needed" can never silently close work. "moot" is exempt (an item can
# legitimately already be answered, or be voided by a removed claim, with nothing left to edit).
_CLOSING = {"answered", "applied", "reconciled", "moot"}
_NEEDS_EDIT = {"answered", "applied", "reconciled"}


def _apply_maintain(conn, out: dict, version_note: str) -> tuple[bool, int]:
    """Apply a maintain_one result: save the revision (versioned), close the items the model
    GENUINELY settled (recording HOW), record any new items. An item closes only when its
    `outcome` is a real settlement (see _CLOSING) — and answered/applied/reconciled also
    require the article to have changed, else the item stays open. Returns (content_changed,
    number_of_items_actually_closed)."""
    from . import article_talk
    from . import notes as notes_svc
    if not out["ok"]:
        return False, 0
    changed = bool(out["changed"] and out["content_md"])
    if changed:
        notes_svc.upsert_note(conn, out["title"], out["content_md"], kind="kb", version_note=version_note)
    closed = 0
    for r in out["resolved"]:
        outcome = r.get("outcome") or ""
        how = r["how"] or ""
        if outcome not in _CLOSING:
            continue                                    # unresolvable / unknown → stays open
        if outcome in _NEEDS_EDIT and not changed:
            continue                                    # claimed a fix but made no edit → stays open
        if outcome == "moot" and not changed:
            how = ("(moot — no edit needed) " + how).strip()
        article_talk.resolve_with(conn, r["id"], how)
        closed += 1
    if out["new"]:
        article_talk.record(conn, out["title"], out["new"], author="ai")
    return changed, closed


_MAINT_WATERMARK = "kb_maintain:since"


def _now_sec(conn) -> str:
    # article_talk.created_at is SECOND precision (datetime('now')); take the watermark at
    # the same precision so the `>=` gate below is exact, never off by a sub-second.
    return conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%S','now') AS n").fetchone()["n"]


def maintain_batch(conn, limit: int = 20) -> dict:
    """Run the maintenance pass — but only over articles with a talk item raised SINCE the
    last pass (a watermark), not every article with any open item. This is the compute gate:
    an article whose only open items are old unsettled ones with no new directive gets
    skipped (its sources changing is update_batch's job — that pass re-examines all its open
    items). So maintenance specifically picks up NEW owner directives / writer-raised items.

    Applies each valid revision (versioned), closes the items the model genuinely settled
    (recording HOW), records any new items, and advances the watermark.

    Watermark discipline (mirrors update_batch): advance only over the LEADING run of new
    items whose article succeeded — a failed (or deferred-by-cap) article holds the watermark
    at that item, so nothing is silently skipped; the next run retries from there. First run
    drains the whole existing backlog of open items once, then gates on newness thereafter."""
    from ..db import get_meta, set_meta
    since = get_meta(_MAINT_WATERMARK)
    if since is None:
        since = ""                       # first run: every currently-open item qualifies
    if not llm.has_credentials():
        # Don't advance with no key, or these items would be skipped forever.
        return {"articles": 0, "changed": 0, "resolved": 0, "examined": 0,
                "kept_open": 0, "failed": 0, "skipped": "no LLM credentials"}

    # Open items raised at/after the watermark second (oldest first), each tied to a live kb
    # article. `>=` (with a second-precision watermark) is at-least-once: an item created in
    # the watermark's own second is re-examined rather than risk being skipped.
    items = conn.execute(
        "SELECT t.id, t.created_at, t.article_title AS title FROM article_talk t "
        "WHERE t.resolved_at IS NULL AND t.kind IN ('conflict','question','todo','directive') "
        "AND t.created_at >= ? "
        "AND EXISTS (SELECT 1 FROM notes n WHERE n.title=t.article_title AND n.kind='kb' AND n.deleted_at IS NULL) "
        "ORDER BY t.created_at, t.id",
        (since,)).fetchall()
    if not items:
        # Quiet night (no new items): no LLM spent — just advance the clock so it keeps moving.
        set_meta(conn, _MAINT_WATERMARK, _now_sec(conn)); conn.commit()
        return {"articles": 0, "changed": 0, "resolved": 0, "examined": 0, "kept_open": 0, "failed": 0}

    # Distinct articles in item-creation order, capped at `limit`; the rest are deferred and
    # (like failures) hold the watermark so they're picked up next run.
    seen, ordered = set(), []
    for it in items:
        if it["title"] not in seen:
            seen.add(it["title"]); ordered.append(it["title"])
    deferred = set(ordered[int(limit):])
    work = ordered[:int(limit)]

    known = _known_titles(conn)
    changed = resolved = examined = failed = 0
    bad = set(deferred)
    for title in work:
        out = maintain_one(conn, title, known)
        if not out["ok"]:
            failed += 1
            bad.add(title)
            continue
        did_change, n_closed = _apply_maintain(conn, out, "maintenance pass")
        changed += 1 if did_change else 0
        resolved += n_closed
        examined += len(out["resolved"])

    # Advance over the leading run of items whose article succeeded; stop at the first item
    # belonging to a failed/deferred article so it (and everything after) is retried. On a
    # fully-clean run, jump to now so old processed items don't re-qualify next pass.
    new_wm = _now_sec(conn)
    for it in items:
        if it["title"] in bad:
            new_wm = it["created_at"]
            break
    set_meta(conn, _MAINT_WATERMARK, new_wm)
    conn.commit()
    return {"articles": len(work), "changed": changed, "resolved": resolved,
            "examined": examined, "kept_open": examined - resolved,
            "failed": failed, "deferred": len(deferred)}


def _articles_citing(conn, note_id: int) -> set[str]:
    """kb articles that cite a given source note (via the links table)."""
    rows = conn.execute(
        "SELECT DISTINCT s.title FROM links l JOIN notes s ON s.id=l.source_note_id "
        "WHERE l.target_note_id=? AND s.kind='kb' AND s.deleted_at IS NULL", (note_id,)).fetchall()
    return {r["title"] for r in rows}


def _articles_citing_title(conn, title: str) -> set[str]:
    """kb articles that cite a note by TITLE. Used for DELETED sources: soft_delete nulls
    links.target_note_id (so the id-based lookup finds nothing), but it leaves target_title
    intact — so we match on that to still route a deletion to the articles that cited it
    and let them purge claims whose only source just disappeared."""
    rows = conn.execute(
        "SELECT DISTINCT s.title FROM links l JOIN notes s ON s.id=l.source_note_id "
        "WHERE lower(l.target_title)=lower(?) AND s.kind='kb' AND s.deleted_at IS NULL", (title,)).fetchall()
    return {r["title"] for r in rows}


def _articles_for_note_entities(conn, note_id: int) -> set[str]:
    """kb articles whose entities this note mentions (so a new fact routes to its subject)."""
    rows = conn.execute(
        "SELECT DISTINCT e.article_title AS t FROM entity_mentions m JOIN entities e ON e.id=m.entity_id "
        "WHERE m.note_id=? AND e.article_title IS NOT NULL", (note_id,)).fetchall()
    return {r["t"] for r in rows}


_WATERMARK = "kb_incremental:since"


def update_batch(conn, limit: int = 40, new_subject_min: int = 2, max_articles: int = 25) -> dict:
    """Incremental update: flow notes changed since the last pass into the EXISTING KB.
    Routes each change to its articles (cite-based for edits/deletes ∪ entity-based for new
    facts), refreshes each affected article once, nudges a Review card for brand-new
    subjects that have no article yet, and advances the watermark. Additive + reconciling;
    the full rebuild remains the source of truth. Assumes changed notes are already
    analyzed + the entity index rebuilt (the recipe does that first).

    Watermark discipline: it advances only over the LEADING run of changes whose every
    target article succeeded — a failed (or deferred-by-cap) article holds the watermark at
    the prior change, so nothing is silently skipped; the next run retries from there."""
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
    # Keep per-change targets so we can hold the watermark precisely on any failure.
    affected: dict[str, dict] = {}
    change_targets: list[tuple] = []                         # (changed_at, {titles})  in order
    orphans: list[dict] = []
    for ch in changes:
        if ch["deleted"]:
            # soft_delete nulled this note's incoming links' target_note_id, so route the
            # deletion by surviving target_title instead (id-based lookup would find nothing).
            targets = _articles_citing_title(conn, ch["title"])
        else:
            targets = _articles_citing(conn, ch["id"]) | _articles_for_note_entities(conn, ch["id"])
        change_targets.append((ch["changed_at"], targets))
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

    # Cap the LLM fan-out: refresh at most max_articles this run; the rest are "deferred"
    # and (like failures) hold the watermark so they're picked up next run.
    items = list(affected.items())
    deferred = {t for t, _ in items[int(max_articles):]}
    items = items[:int(max_articles)]

    known = _known_titles(conn)
    changed = resolved = examined = failed = 0
    bad_articles = set(deferred)
    for title, slot in items:
        out = maintain_one(conn, title, known,
                           extra_source_ids=list(slot["new"]), removed_titles=list(slot["removed"]))
        if not out["ok"]:
            failed += 1
            bad_articles.add(title)
            continue
        did_change, n_closed = _apply_maintain(conn, out, "incremental update")
        changed += 1 if did_change else 0
        resolved += n_closed
        examined += len(out["resolved"])

    # Brand-new recurring subjects: CREATE their articles now — maintenance owns this, no
    # waiting for a full rebuild. create_article dedups/folds and refuses thin subjects; a
    # creation that fails the lint falls back to a Review card for the owner.
    subj = _create_new_subjects(conn, orphans, new_subject_min)

    # Advance over the leading run of changes whose targets all succeeded; stop at the first
    # change that touched a failed/deferred article so it (and everything after) is retried.
    new_wm = since
    for changed_at, targets in change_targets:
        if targets & bad_articles:
            break
        new_wm = changed_at
    set_meta(conn, _WATERMARK, new_wm)
    conn.commit()
    return {"changes": len(changes), "articles": len(items), "changed": changed,
            "resolved": resolved, "examined": examined, "kept_open": examined - resolved,
            "failed": failed, "deferred": len(deferred),
            "created": subj["created"], "new_subjects": subj["created"] + subj["nudged"]}


def taxonomy_health(conn, post_card: bool = True) -> dict:
    """Read-only KB taxonomy-drift report (no LLM) — turns "rare manual Reorganize" from a
    guess into a triggered decision. Surfaces ORPHAN articles (nothing but the index links
    them) and un-foldered Reference articles (kb/Reference/<Name> — the guide says Reference
    is always foldered). Posts a single "Reorganize recommended" Review card past thresholds
    (skip with post_card=False — e.g. when a chat tool just wants the report). Returns the
    counts + a sample of titles."""
    from . import reviews as reviews_svc
    arts = [r["title"] for r in conn.execute(
        r"SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL AND title NOT LIKE 'kb/\_%' ESCAPE '\'")]
    orphans = []
    for t in arts:
        inbound = conn.execute(
            r"SELECT 1 FROM links l JOIN notes s ON s.id=l.source_note_id "
            r"WHERE lower(l.target_title)=lower(?) AND s.kind='kb' AND s.deleted_at IS NULL "
            r"AND s.title NOT LIKE 'kb/\_%' ESCAPE '\' LIMIT 1", (t,)).fetchone()
        if not inbound:
            orphans.append(t)
    flat_ref = [t for t in arts if t.startswith("kb/Reference/") and t.count("/") == 2]
    report = {"articles": len(arts), "orphans": len(orphans), "flat_reference": len(flat_ref),
              "orphan_titles": orphans[:20], "flat_reference_titles": flat_ref[:20]}
    reasons = []
    if len(orphans) >= 5:
        reasons.append(f"{len(orphans)} orphan article(s) (nothing links to them)")
    if flat_ref:
        reasons.append(f"{len(flat_ref)} un-foldered Reference article(s)")
    if reasons and post_card:
        card = "Reorganize recommended"
        if not conn.execute("SELECT 1 FROM review_items WHERE title=? AND status='pending'", (card,)).fetchone():
            reviews_svc.create_review_item(conn, None, title=card,
                                           message="; ".join(reasons) + ". Run a Reorganize when convenient.")
            report["carded"] = True
        conn.commit()
    report["reasons"] = reasons
    return report


def _create_new_subjects(conn, orphans: list[dict], min_notes: int) -> dict:
    """Recurring subjects (≥ min_notes notes) that changed but have no article yet: CREATE
    them (create_article dedups/folds + refuses thin stubs). A creation that fails the lint
    falls back to a Review card so the owner sees it — maintenance no longer defers new
    subjects to a full rebuild. Returns {created, nudged}."""
    from . import reviews as reviews_svc
    tally: dict[tuple, int] = {}
    for ch in orphans:
        for r in conn.execute(
            "SELECT e.type, e.canonical_name FROM entity_mentions m JOIN entities e ON e.id=m.entity_id "
            "WHERE m.note_id=? AND e.article_title IS NULL", (ch["id"],)).fetchall():
            key = (r["type"], r["canonical_name"])
            tally[key] = tally.get(key, 0) + 1
    created = nudged = 0
    for (typ, name), c in tally.items():
        if c < int(min_notes):
            continue
        res = create_article(conn, name, etype=typ, min_notes=int(min_notes))
        if res.get("created"):
            created += 1
        elif res.get("folded"):
            continue
        else:
            card = f"New subject: {name}"
            if not conn.execute("SELECT 1 FROM review_items WHERE title=? AND status='pending'",
                                (card,)).fetchone():
                reviews_svc.create_review_item(
                    conn, None, title=card,
                    message=f"{c} note(s) mention {name} ({typ}) — couldn't auto-create its article "
                            f"({res.get('reason')}); add it by hand.")
                nudged += 1
    return {"created": created, "nudged": nudged}


def _now(conn) -> str:
    # Match the notes table's timestamp format (millisecond precision) so watermark
    # comparisons against updated_at/deleted_at are exact, not off-by-a-fraction.
    return conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%f','now') AS n").fetchone()["n"]


def write_batch(conn, articles: list[dict], instructions: str | None = None, on_article=None) -> dict:
    """Write every article; split valid (saved by the recipe) vs quarantined (failed
    the structure lint — surfaced, not saved). Mirrors validate_citations' shape.
    `on_article(index, total, title)` is called before each write so the run modal can
    show which article is currently being written."""
    # The titles writers may cross-link: every planned article PLUS any live kb article
    # that survives (so links resolve when adding to an existing KB, not just full rebuild).
    planned = [str(a.get("title") or "").strip() for a in articles]
    existing = [r["title"] for r in conn.execute(
        r"SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL AND title NOT LIKE 'kb/\_%' ESCAPE '\'").fetchall()]
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
