"""Declarative ("YAML-defined") workflow actions.

An action TYPE can be defined as data in `actions/*.yaml` instead of as a Python
function in workflows._ACTIONS. A definition is a list of `steps`, each invoking
one PRIMITIVE (a small, fixed, code-backed operation — see _PRIMITIVES) with
templated inputs. This keeps *effects* in a vetted primitive library while
letting *composition* live in YAML.

Templating uses Jinja2's SandboxedEnvironment (value/condition expressions only —
no side effects, no arbitrary attribute access). The engine supports linear
steps, `when:` guards, `for_each:` sub-pipelines, and `stop_when_empty:` early
returns. Dispatch (workflows.run_workflow) prefers a YAML definition over the
Python fallback, so both can coexist during migration.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path

import yaml
from jinja2 import ChainableUndefined
from jinja2 import meta as _jinja_meta
from jinja2.sandbox import SandboxedEnvironment

from . import clock
from . import embeddings
from . import geo
from . import llm
from . import notes as notes_svc
from . import reviews as reviews_svc
from ..db import get_conn, get_meta, set_meta


def _today() -> str:
    return clock.today_iso()


def _local_day_path() -> str:
    """Today as 'YYYY/MM/DD' in the app timezone (the dated-capture bucket)."""
    return clock.today_path()


# --- Templating -------------------------------------------------------------
# A single sandboxed environment. ChainableUndefined lets `a.b.c` chain over
# missing keys without raising; expressions never perform I/O.
_env = SandboxedEnvironment(undefined=ChainableUndefined, autoescape=False)
_env.filters["clip"] = lambda s, n: (s or "")[:n]


def _eval(expr: str, scope: dict):
    """Evaluate an expression to its native Python value (Undefined → None).
    Accepts a bare expression or one wrapped in `{{ }}` (for when/for_each)."""
    expr = expr.strip()
    if expr.startswith("{{") and expr.endswith("}}"):
        expr = expr[2:-2].strip()
    return _env.compile_expression(expr, undefined_to_none=True)(**scope)


def _render_value(val, scope):
    """Render a `with:` value: a lone `{{ expr }}` yields the native value; a
    string with embedded `{{ }}` is interpolated; dicts/lists recurse. The legacy
    `{date}` token is substituted in any resulting string for backward-compat."""
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("{{") and s.endswith("}}") and s.count("{{") == 1:
            out = _eval(s[2:-2], scope)
            return out.replace("{date}", _today()) if isinstance(out, str) else out
        if "{{" in val:
            return _env.from_string(val).render(**scope).replace("{date}", _today())
        return val.replace("{date}", _today())
    if isinstance(val, dict):
        return {k: _render_value(v, scope) for k, v in val.items()}
    if isinstance(val, list):
        return [_render_value(v, scope) for v in val]
    return val


def _truthy(v) -> bool:
    try:
        return bool(v)
    except Exception:
        return False


# --- Primitives -------------------------------------------------------------
# Each primitive is (ctx, **kwargs) -> value. They are the ONLY effecting code;
# YAML can compose them but never bypass them.

_MAX_CALL_DEPTH = 5     # call_action nesting depth
_MAX_STEPS = 1000       # total steps executed per top-level run (runaway backstop)


class _Ctx:
    def __init__(self, conn, workflow_id, trigger, *, call_stack=None, depth=0, budget=None, on_step=None):
        self.conn = conn
        self.workflow_id = workflow_id
        self.trigger = trigger or {}
        self.call_stack = call_stack or []          # action types currently on the stack
        self.depth = depth                          # call_action nesting depth
        self.on_step = on_step                      # optional progress callback(step_name)
        # Shared, mutable across nested call_action sub-runs so the step cap is
        # global (a [remaining] one-element list passed by reference).
        self.budget = budget if budget is not None else [_MAX_STEPS]


def _p_read_note(ctx, title=None, id=None):
    if id is not None:
        row = ctx.conn.execute(
            "SELECT id, title, slug, content_md FROM notes WHERE id = ? AND deleted_at IS NULL", (id,)
        ).fetchone()
    elif title:
        row = notes_svc.get_by_title(ctx.conn, title)
    else:
        return None
    return dict(row) if row else None


def _p_write_note(ctx, title, content_md=None, text=None, mode="replace", kind=None, version_note=None):
    body_in = content_md if content_md is not None else (text or "")
    if kind == "kb":
        title = notes_svc.root_title(title, "kb")   # the encyclopedia lives under kb/
    if mode == "append":
        note = notes_svc.get_by_title(ctx.conn, title)
        base = note["content_md"] if note else f"# {title}\n"
        content = base.rstrip() + "\n" + body_in + "\n"
        vn = version_note or "workflow append"
    else:
        content = body_in
        vn = version_note or "workflow synthesis"
    nid = notes_svc.upsert_note(ctx.conn, title, content, source="workflow", version_note=vn, kind=kind)
    # Resolve by id, not title: upsert_note may have disambiguated the title to
    # avoid clobbering an existing note of a different kind.
    note = ctx.conn.execute("SELECT title, slug FROM notes WHERE id = ?", (nid,)).fetchone()
    return {"id": nid, "title": note["title"] if note else title, "slug": note["slug"] if note else None}


def _p_create_review(ctx, title, message="", link_title=None):
    link_slug = None
    if link_title:
        n = notes_svc.get_by_title(ctx.conn, link_title)
        link_slug = n["slug"] if n else None
    rid = reviews_svc.create_review_item(ctx.conn, ctx.workflow_id, title, message or "", link_slug)
    return {"id": rid, "link_slug": link_slug}


def _p_semantic_search(ctx, query, limit=8, kind=None, tags=None, since=None, max_distance=None):
    return embeddings.semantic_search(
        ctx.conn, query, int(limit),
        kind=kind or None,
        tags=list(tags) if tags else None,
        since=since or None,
        max_distance=float(max_distance) if max_distance is not None else None,
    )


def _p_analyze_pending(ctx, limit=60, force=False):
    """Ids of entry/daily notes whose AI analysis is missing or stale (or ALL of them
    when force is set — to refresh every analysis after a behaviour/prompt change)."""
    from . import note_analysis
    return note_analysis.pending_ids(ctx.conn, int(limit), force=bool(force))


def _p_analyze_note(ctx, id, force=False):
    """(Re)compute one note's analysis sidecar (no-op if unchanged, unless force).
    Returns whether a fresh analysis was stored."""
    from . import note_analysis
    return {"id": int(id), "changed": bool(note_analysis.analyze(ctx.conn, int(id), force=bool(force)))}


def _p_rebuild_entity_index(ctx, limit=20000):
    """(Re)aggregate the canonical entity index from note_analysis. Returns the count."""
    from . import entity_index
    return {"entities": entity_index.rebuild(ctx.conn, int(limit))}


def _p_write_disambiguation(ctx):
    """Generate protected kb/_disambig/<Term> pages for terms mapping to multiple articles."""
    from . import entity_index
    return {"pages": entity_index.write_disambiguation_pages(ctx.conn)}


def _p_record_talk(ctx, article_title, entries):
    """Record the article writer's talk entries (decisions/conflicts/questions)."""
    from . import article_talk
    return {"added": article_talk.record(ctx.conn, str(article_title), list(entries or []), author="ai")}


def _p_flag_dead_links(ctx):
    """Flag dangling kb cross-links as todos on each article's talk."""
    from . import wiki_build
    return wiki_build.flag_dead_links(ctx.conn)


def _p_rebuild_article(ctx, title=None, instructions=None):
    """Regenerate ONE kb article in place from its source notes (manual/owner-triggered)."""
    from . import wiki_build
    return wiki_build.rebuild_article(ctx.conn, title, instructions)


def _p_check_needed_links(ctx, title=None, mode="propose"):
    """Deterministically find/add missing cross-links in an article (or KB-wide)."""
    from . import wiki_build
    return wiki_build.check_needed_links(ctx.conn, title, mode)


def _p_create_article(ctx, subject=None, etype=None, min_notes=2):
    """Create ONE new-subject kb article from its notes (dedup before spawn)."""
    from . import wiki_build
    return wiki_build.create_article(ctx.conn, subject, etype, int(min_notes))


def _p_recategorize_article(ctx, title=None, new_title=None):
    """Move/rename a kb article (folds it into a subcategory; rewrites inbound links)."""
    from . import wiki_build
    return wiki_build.recategorize_article(ctx.conn, title, new_title)


def _p_merge_articles(ctx, sources=None, into=None):
    """Fold kb articles INTO another (union sources, rewrite inbound links, hysteresis)."""
    from . import wiki_build
    return wiki_build.merge_articles(ctx.conn, sources or [], into)


def _p_refresh_index(ctx):
    """Rebuild kb/_index from live articles (no LLM)."""
    from . import wiki_build
    return {"articles": wiki_build.refresh_index(ctx.conn)}


def _p_split_article(ctx, parent=None, child=None, child_sources=None):
    """Spin a child article off a parent from given source notes (explicit partition)."""
    from . import wiki_build
    return wiki_build.split_article(ctx.conn, parent, child, child_sources or [])


def _p_research_article(ctx, title=None, mode="propose"):
    """Surface related Reference links for an article (propose-only, embedding-nominated)."""
    from . import wiki_build
    return wiki_build.research_article(ctx.conn, title, mode)


def _p_taxonomy_health(ctx):
    """Read-only KB taxonomy-drift report (orphans, un-foldered Reference); cards if overdue."""
    from . import wiki_build
    return wiki_build.taxonomy_health(ctx.conn)


def _p_link_owner(ctx):
    """Link the default person to their freshly-written People article."""
    from . import wiki_build
    return wiki_build.link_owner(ctx.conn)


def _p_wiki_maintain(ctx, limit=20):
    """Component 3: address open talk items on existing articles against their sources."""
    from . import wiki_build
    return wiki_build.maintain_batch(ctx.conn, int(limit))


def _p_wiki_update(ctx, limit=40):
    """Component 3 (incremental): flow notes changed since the watermark into existing articles."""
    from . import wiki_build
    return wiki_build.update_batch(ctx.conn, int(limit))


def _p_flag_ungrounded_reference(ctx):
    """Flag Reference articles padded with LLM 'common knowledge' instead of the notes."""
    from . import wiki_build
    return wiki_build.flag_ungrounded_reference(ctx.conn)


def _p_redate_notes(ctx, limit=2000, dry_run=False):
    """File loose entry notes under the flat dated tree notes/YYYY/MM/DD/N."""
    from . import note_normalize
    return note_normalize.redate_batch(ctx.conn, int(limit), bool(dry_run))


def _p_title_notes(ctx, limit=40, dry_run=False):
    """Give bare dated notes a generated leaf title (notes/<date>/N - <title>)."""
    from . import note_normalize
    return note_normalize.title_batch(ctx.conn, int(limit), bool(dry_run))


def _p_seed_kb_watermark(ctx):
    """Reset the incremental-update watermark to now — the full build just covered all
    history, so incremental should only pick up changes from here on."""
    from . import wiki_build
    set_meta(ctx.conn, "kb_incremental:since", wiki_build._now(ctx.conn))
    return {"since": "now"}


def _p_review_open_talk(ctx, limit=20):
    """Open a review 'session' from the build: post a Review card for each article that
    has unresolved talk items needing a human (conflict / question / todo / directive), so
    they're worked through the Review inbox rather than ticked off in the talk panel."""
    from . import reviews as reviews_svc
    conn = ctx.conn
    rows = conn.execute(
        "SELECT t.article_title AS title, COUNT(*) AS c FROM article_talk t "
        "WHERE t.resolved_at IS NULL AND t.kind IN ('conflict','question','todo','directive') "
        "GROUP BY t.article_title ORDER BY c DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    posted = 0
    for r in rows:
        note = conn.execute(
            "SELECT slug FROM notes WHERE title=? AND kind='kb' AND deleted_at IS NULL",
            (r["title"],)).fetchone()
        if not note:
            continue
        if conn.execute("SELECT 1 FROM review_items WHERE link_slug=? AND status='pending'",
                        (note["slug"],)).fetchone():
            continue                                        # already an open card for this article — don't re-spam
        leaf = r["title"].split("/")[-1]
        reviews_svc.create_review_item(
            conn, ctx.workflow_id, title=f"Review: {leaf}",
            message=f"{r['c']} open item(s) in this article's AI talk need a look.",
            link_slug=note["slug"])
        posted += 1
    conn.commit()
    return {"cards": posted}


def _p_write_kb_index(ctx, articles, valid):
    """(Re)write the protected kb/_index map from the articles that were actually SAVED, so
    it never links to a quarantined (unsaved) article — the one dead-link source the
    prevention passes skip, because kb/_* pages are protected."""
    from . import wiki_build
    from . import notes as ns
    saved = {str(d.get("title", "")).lower() for d in (valid or [])}
    kept = [a for a in (articles or []) if str(a.get("title", "")).lower() in saved]
    if not kept:
        return {"articles": 0, "skipped": "no saved articles — kept the existing index"}
    ns.upsert_note(ctx.conn, "kb/_index", wiki_build.build_index_md(kept), kind="kb")
    ctx.conn.commit()
    return {"articles": len(kept)}


def _p_kb_reset(ctx):
    """Soft-delete all kb/ articles except protected kb/_* pages, and clear the
    synthesis watermark/markers. Undoable. Only reachable via the wiki_build recipe."""
    from . import wiki_build
    return wiki_build.reset(ctx.conn)


def _p_corpus_digest(ctx, limit=3000):
    """Compact survey (gist/domain/entities per note) the outline reads."""
    from . import wiki_build
    return wiki_build.corpus_digest(ctx.conn, int(limit))


def _p_wiki_outline(ctx, digest, instructions=None):
    """Survey -> entity-first taxonomy: {articles:[{title,domain,scope,sources}], index_md}."""
    from . import wiki_build
    return wiki_build.outline(ctx.conn, list(digest or []), instructions)


def _p_wiki_write_batch(ctx, articles, instructions=None):
    """Write every planned article (draft + one revise pass + structure lint); returns
    {valid, quarantined, count, bad}. Reports each article to the run modal as it's written."""
    from . import wiki_build

    def on_article(i, n, title):
        if ctx.on_step:
            try:
                ctx.on_step(f"wiki_write:{i}/{n}:{title}")
            except Exception:  # noqa: BLE001 — progress must never break the build
                pass
    return wiki_build.write_batch(ctx.conn, list(articles or []), instructions, on_article=on_article)


def _p_validate_structure(ctx, title, content_md):
    """Lint one article against its domain guide's spec (lead, citations, PII firewall,
    required sections). Returns {ok, errors, warnings, stub, domain}."""
    from . import wiki_guides
    return wiki_guides.validate_structure(title, content_md or "")


def _p_query_notes(ctx, kind=None, since_id=0, limit=1000):
    sql = "SELECT id, title, slug, content_md, created_at FROM notes WHERE deleted_at IS NULL"
    params: list = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if since_id:
        sql += " AND id > ?"
        params.append(int(since_id))
    sql += " ORDER BY id LIMIT ?"
    # Clamp: a non-positive limit is SQLite's "no limit" (would pull every row
    # into one LLM prompt); cap the upper bound too.
    params.append(max(1, min(int(limit), 1000)))
    return [dict(r) for r in ctx.conn.execute(sql, params).fetchall()]


def _p_query_entry_changes(ctx, since="", limit=20):
    """Entries CHANGED since a timestamp watermark — new, edited, OR soft-deleted
    (so wiki synthesis can fold in edits and clean up after removals, not just add
    new notes). Each row carries `deleted` and `changed_at`; the caller advances the
    watermark to the max changed_at processed."""
    # Synthesis consumes the daily ROLLUPS plus any legacy free-titled entries —
    # but NOT the raw dated captures (notes/daily/.../<n>), which reach the KB only
    # via their daily summary. This keeps facts in the KB exactly once (no double
    # count) while legacy notes/<topic> entries stay first-class sources.
    rows = ctx.conn.execute(
        "SELECT id, title, slug, content_md, created_at, updated_at, deleted_at, "
        "(deleted_at IS NOT NULL) AS deleted, COALESCE(deleted_at, updated_at) AS changed_at "
        "FROM notes WHERE "
        "  (kind = 'daily' OR (kind = 'entry' AND title NOT LIKE 'notes/daily/%')) "
        "  AND COALESCE(deleted_at, updated_at) > ? "
        "ORDER BY changed_at LIMIT ?",
        (since or "", max(1, min(int(limit), 1000))),
    ).fetchall()
    return [dict(r) for r in rows]


def _p_get_meta(ctx, key, default=None):
    return get_meta(key, default)


def _p_set_meta(ctx, key, value):
    set_meta(ctx.conn, key, str(value))
    return None


def _p_set_tags(ctx, note_id, tags):
    return notes_svc.set_tags(ctx.conn, int(note_id), list(tags or []))


# Code-backed helpers exposed as named primitives. These wrap the irregular
# logic (LLM prompt building, parsing, watermark arithmetic) that doesn't belong
# in YAML — and delegate to the same workflows.* functions tests already stub.

def _p_suggest_tags(ctx, title, content, prompt=None):
    from . import workflows as wf
    return wf._suggest_tags(title or "", content or "", prompt)


def _p_summarise_entries(ctx, entries, prompt=None):
    from . import workflows as wf
    # The summary is STORED (a daily/log rollup), so render @t[...] live values as
    # dated snapshots — the rollup is a point-in-time record, not a live note.
    entries = [clock.expand_tokens(str(e), snapshot=True) for e in (entries or [])]
    return wf._summarise_entries(entries, prompt)


def _p_wiki_plan(ctx, entries, existing_kb, instructions=None):
    from . import workflows as wf
    # conn enables semantic retrieval of only the relevant existing KB articles.
    return wf._synthesize_actions(list(entries or []), list(existing_kb or []), instructions, conn=ctx.conn)


_FN_DEF_RE = re.compile(r"(?m)^[ \t]*\[\^([^\]\s]+)\]:(.*)$")   # [^id]: definition line
_FN_MARK_RE = re.compile(r"\[\^([^\]\s]+)\](?!:)")              # [^id] inline marker
_WIKILINK_RE = re.compile(r"\[\[[^\]]+\]\]")


def citation_issues(content_md: str) -> list[str]:
    """Graph-safety check on a synthesized article's citations (M1): every footnote
    marker resolves to one definition, every definition carries a [[…]] (so a links
    row is recorded), and no id is reused for two sources. Malformed citations would
    silently break delete/edit reconciliation, so such articles are NOT written."""
    text = content_md or ""
    defs: dict[str, int] = {}
    def_has_link: dict[str, bool] = {}
    for m in _FN_DEF_RE.finditer(text):
        fid = m.group(1)
        defs[fid] = defs.get(fid, 0) + 1
        def_has_link[fid] = bool(_WIKILINK_RE.search(m.group(2)))
    markers = {m.group(1) for m in _FN_MARK_RE.finditer(text)}
    issues = []
    for fid in sorted(markers - set(defs)):
        issues.append(f"footnote [^{fid}] has no definition")
    for fid in sorted(f for f, c in defs.items() if c > 1):
        issues.append(f"footnote id [^{fid}] is defined more than once")
    for fid in sorted(f for f, ok in def_has_link.items() if not ok):
        issues.append(f"footnote [^{fid}] definition has no [[source]] link")
    return issues


def _p_kb_uncited_pending(ctx, limit=25, since_floor="", reconsider=False):
    """Live daily-rollups + legacy free-titled entries that NO kb article cites AND
    that synthesis has not already EVALUATED (per-entry marker). Mirrors the
    query_entry_changes candidate set exactly (raw dated captures reach the KB only
    via their rollup, so they're excluded). `since_floor`: entries created on/before
    it are treated as already-evaluated (bootstrap — skip the historical corpus).
    `reconsider`: ignore both the marker and the floor (expensive)."""
    rows = ctx.conn.execute(
        "SELECT n.id, n.title, n.slug, n.content_md, n.created_at, n.updated_at "
        "FROM notes n WHERE n.deleted_at IS NULL "
        "  AND (n.kind='daily' OR (n.kind='entry' AND n.title NOT LIKE 'notes/daily/%')) "
        "  AND NOT EXISTS (SELECT 1 FROM links l JOIN notes kb ON kb.id=l.source_note_id "
        "       AND kb.kind='kb' AND kb.deleted_at IS NULL "
        "       WHERE l.target_note_id=n.id OR lower(l.target_title)=lower(n.title)) "
        "ORDER BY n.id"
    ).fetchall()
    out = []
    for r in rows:
        if not reconsider:
            if since_floor and (r["created_at"] or "") <= since_floor:
                continue                                  # pre-floor → implicitly evaluated
            if get_meta(f"wiki_synth:evaluated:{r['id']}") is not None:
                continue                                  # already considered, intentionally skipped
        out.append(dict(r))
    capped = out[: max(1, min(int(limit), 200))]
    return {"entries": capped, "count": len(out), "ids": [e["id"] for e in capped]}


def _p_chatter_pending(ctx, limit=500):
    """The 'dropped chatter' pool for recurrence detection: live daily-rollups +
    free-titled entries that NO kb article cites and that haven't been promoted as a
    pattern yet. Same candidate predicate as the coverage check (raw dated captures
    excluded). No store — this is just a query over existing notes."""
    rows = ctx.conn.execute(
        "SELECT n.id, n.title, n.slug, n.content_md, n.created_at "
        "FROM notes n WHERE n.deleted_at IS NULL "
        "  AND (n.kind='daily' OR (n.kind='entry' AND n.title NOT LIKE 'notes/daily/%')) "
        "  AND NOT EXISTS (SELECT 1 FROM links l JOIN notes kb ON kb.id=l.source_note_id "
        "       AND kb.kind='kb' AND kb.deleted_at IS NULL "
        "       WHERE l.target_note_id=n.id OR lower(l.target_title)=lower(n.title)) "
        "ORDER BY n.id"
    ).fetchall()
    out = [dict(r) for r in rows if get_meta(f"chatter_promoted:{r['id']}") is None]
    return {"entries": out[: max(1, min(int(limit), 2000))], "count": len(out)}


def _p_cluster_chatter(ctx, entries, tau=0.35, min_days=3, neighbours=16, promote_limit=5):
    """Cluster the chatter pool by embedding similarity (greedy union-find over
    semantic_search — reuses the vectors every note already has, no LLM, nothing
    written). A cluster spanning >= min_days DISTINCT calendar days is a recurring
    PATTERN worth promoting. Returns the promotable clusters, largest first."""
    items = [e for e in (entries or []) if e.get("id") is not None]
    by_id = {e["id"]: e for e in items}
    parent = {i: i for i in by_id}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    for e in items:
        q = ((e.get("title") or "") + "\n" + (e.get("content_md") or ""))[:400].strip()
        if not q:
            continue
        try:
            hits = embeddings.semantic_search(ctx.conn, q, limit=int(neighbours))
        except Exception:
            hits = []
        for h in hits:
            hid = h.get("id")
            if hid == e["id"] or hid not in by_id:
                continue
            if float(h.get("distance", 1.0)) <= float(tau):
                ra, rb = find(e["id"]), find(hid)
                if ra != rb:
                    parent[ra] = rb

    groups: dict = {}
    for i in by_id:
        groups.setdefault(find(i), []).append(i)
    promotable = []
    for ids in groups.values():
        days = {(by_id[i].get("created_at") or "")[:10] for i in ids}
        days.discard("")
        if len(days) >= int(min_days):
            promotable.append({"member_ids": ids, "entries": [by_id[i] for i in ids],
                               "distinct_days": len(days), "size": len(ids)})
    promotable.sort(key=lambda c: (-c["distinct_days"], -c["size"]))
    return {"promotable": promotable[: max(1, int(promote_limit))], "count": len(promotable)}


def _p_mark_promoted(ctx, ids, key=""):
    """Mark chatter entries consumed by a pattern promotion so they're never
    re-clustered (mirrors mark_evaluated)."""
    for nid in (ids or []):
        set_meta(ctx.conn, f"chatter_promoted:{int(nid)}", str(key or "1"))
    return {"marked": len(ids or [])}


def _p_mark_evaluated(ctx, ids, at=None):
    """Record that synthesis CONSIDERED these entries (whether or not a fact resulted),
    so the coverage check never re-feeds an intentionally-skipped entry. Idempotent."""
    from datetime import datetime, timezone
    ts = at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for nid in (ids or []):
        set_meta(ctx.conn, f"wiki_synth:evaluated:{int(nid)}", str(ts))
    return {"marked": len(ids or [])}


def _p_stage_kb_proposals(ctx, articles, conversation_id=None):
    """Stage synthesized KB articles as pending staging_actions (CREATE/UPDATE,
    kind=kb) for the owner to approve — the 'review first' path for coverage. An
    UPDATE carries a content-hash basis so a stale apply is refused, mirroring the
    architect's propose_actions."""
    staged = 0
    for a in (articles or []):
        title = (a.get("title") or "").strip()
        if not title:
            continue
        note = notes_svc.get_by_title(ctx.conn, title)
        if note:
            h = hashlib.sha256((note["content_md"] or "").encode("utf-8")).hexdigest()
            payload = {"type": "UPDATE", "title": title, "content": a.get("content_md") or "",
                       "kind": "kb", "_basis": {"note_id": note["id"], "content_hash": h}}
        else:
            payload = {"type": "CREATE", "title": title, "content": a.get("content_md") or "", "kind": "kb"}
        ctx.conn.execute(
            "INSERT INTO staging_actions (conversation_id, type, payload_json) VALUES (?, ?, ?)",
            (conversation_id, payload["type"], json.dumps(payload)))
        staged += 1
    return {"staged": staged}


def _p_validate_citations(ctx, articles):
    """Split a wiki_plan into citation-VALID articles (safe to write) and
    QUARANTINED ones (malformed footnotes → would break the link graph)."""
    valid, quarantined = [], []
    for a in (articles or []):
        issues = citation_issues(a.get("content_md") or "")
        if issues:
            quarantined.append({**a, "issues": issues})
        else:
            valid.append(a)
    return {"valid": valid, "quarantined": quarantined,
            "ok": len(valid), "bad": len(quarantined)}


def _cited_titles(md: str) -> set:
    """Lower-cased set of every [[wiki-link]] target in the text (link-graph members)."""
    return {m.group(1).strip().lower()
            for m in re.finditer(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]", md or "") if m.group(1).strip()}


def _p_kb_audit(ctx, limit=1000):
    """Read-only LINT of the knowledge base: scan every kb article for citation and
    formatting problems WITHOUT writing anything. Per article it reports:
      - footnote integrity (a marker with no definition, an id defined twice, a
        definition carrying no [[source]] link) — via citation_issues()
      - a citation / cross-link whose [[target]] resolves to no existing note (broken)
      - leftover old-style "## Sources" list (should be a "## References" footnote block)
      - footnote markers used but no "## References" section holding their definitions
      - no citations at all (a KB article with durable facts but nothing traced)
    Returns {flagged: [{id,title,slug,issues:[...]}], bad, ok, scanned}."""
    rows = ctx.conn.execute(
        "SELECT id, title, slug, content_md FROM notes "
        "WHERE kind='kb' AND deleted_at IS NULL ORDER BY title"
    ).fetchall()
    capped = rows[: max(1, min(int(limit), 5000))]
    flagged = []
    for r in capped:
        md = r["content_md"] or ""
        issues = list(citation_issues(md))
        # Broken targets: every [[title]] (cite or cross-link) must resolve to a note.
        # Keep the original casing for the message; dedupe case-insensitively.
        seen = set()
        for m in re.finditer(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]", md):
            tgt = m.group(1).strip()
            if not tgt or tgt.lower() in seen:
                continue
            seen.add(tgt.lower())
            if notes_svc.get_by_title(ctx.conn, tgt) is None:
                issues.append(f"link [[{tgt}]] resolves to no note")
        # Formatting drift against the house style.
        if "## Sources" in md:
            issues.append('old-style "## Sources" list — convert to "## References" footnotes')
        if _FN_MARK_RE.search(md) and "## References" not in md:
            issues.append('footnote markers present but no "## References" section')
        if not _WIKILINK_RE.search(md):
            issues.append("no citations — no [[source]] link anywhere")
        if issues:
            flagged.append({"id": r["id"], "title": r["title"], "slug": r["slug"], "issues": issues})
    return {"flagged": flagged, "bad": len(flagged), "ok": len(capped) - len(flagged), "scanned": len(capped)}


def _strip_code_fence(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    return s.strip()


_RECITE_DEFAULT = (
    "You are reformatting ONE knowledge-base article's CITATIONS to the house style. "
    "Do NOT change, add, or drop any FACT — only restyle citations and lightly tidy prose. "
    "Output ONLY the rewritten Markdown article, nothing else.\n"
    "Rules:\n"
    "- Keep every fact and EVERY [[wiki-link]] the article already has — never drop a cited "
    "source or a cross-link.\n"
    "- NATURAL INLINE: where a [[title]] reads naturally in the sentence, keep it inline "
    "(use [[title|display]] if the bare title is clunky), exact title preserved.\n"
    "- ATTRIBUTION FOOTNOTE: where a [[source]] is just tacked on as attribution, replace it "
    "with a footnote marker [^sN] and define it in a \"## References\" block: "
    "[^s1]: [[Source Title]] — DATE (reuse the date from the old \"## Sources\" list if present, "
    "else omit \"— DATE\").\n"
    "- Replace any old \"## Sources\" bullet list with the \"## References\" footnote block.\n"
    "- Every [^sN] used has exactly one definition; every definition contains the source's [[…]]; "
    "never reuse an id for two different sources.\n"
    "- If the article has no citations to restyle, return it unchanged.\n\n"
    "ARTICLE:\n{article}"
)


def _p_kb_old_citation_pending(ctx, limit=10):
    """KB articles still in the OLD citation style (a '## Sources' list and/or inline
    [[notes/…]] cites) and NOT yet converted to footnotes ([^…]). Idempotent: once an
    article gains a [^ footnote it drops out of the candidate set."""
    rows = ctx.conn.execute(
        "SELECT id, title, content_md FROM notes WHERE kind='kb' AND deleted_at IS NULL "
        "  AND content_md NOT LIKE '%[^%' "
        "  AND (content_md LIKE '%## Sources%' OR content_md LIKE '%[[notes/%') "
        "ORDER BY id"
    ).fetchall()
    capped = [dict(r) for r in rows][: max(1, min(int(limit), 100))]
    return {"articles": capped, "count": len(rows), "ids": [a["id"] for a in capped]}


def _p_recite_articles(ctx, articles):
    """Rewrite each article's citations into the footnote style via the LLM, then GUARD:
    reject a rewrite whose footnotes are malformed (citation_issues) OR that DROPS any
    [[…]] the original had (would break the link graph). Returns valid vs quarantined."""
    from . import prompts
    valid, quarantined = [], []
    tmpl = prompts.get("actions.recite", _RECITE_DEFAULT)
    for a in (articles or []):
        title, original = a.get("title"), (a.get("content_md") or "")
        if not title or not original.strip():
            continue
        try:
            out = llm.complete([{"role": "user", "content": tmpl.replace("{article}", original)}],
                               model=llm.model_for("synthesis"), max_tokens=4096)
        except Exception as e:
            quarantined.append({"title": title, "issues": [f"LLM error: {e}"]}); continue
        out = _strip_code_fence(out)
        issues = citation_issues(out)
        dropped = _cited_titles(original) - _cited_titles(out)
        if not out.strip():
            issues.append("empty rewrite")
        if dropped:
            issues.append("would drop citations: " + ", ".join(sorted(dropped))[:160])
        if issues:
            quarantined.append({"title": title, "issues": issues})
        else:
            valid.append({"op": "update", "title": title, "content_md": out})
    return {"valid": valid, "quarantined": quarantined, "ok": len(valid), "bad": len(quarantined)}


def _p_gather_context(ctx, source_title=None, context_query=None):
    """Build context text from a named note or a semantic search (synthesize).
    @t[...] live values are expanded so the model reads the value, not the token."""
    if source_title:
        row = notes_svc.get_by_title(ctx.conn, source_title)
        return clock.expand_tokens(row["content_md"]) if row else ""
    if context_query:
        out = ""
        for r in embeddings.semantic_search(ctx.conn, context_query, 8):
            n = notes_svc.get_by_title(ctx.conn, r["title"])
            if n:
                out += f"\n\n## {n['title']}\n{clock.expand_tokens(n['content_md'])}"
        return out
    return ""


def _p_llm(ctx, prompt, content="", max_tokens=1024, on_no_key="raise", model=None):
    """Run an LLM prompt over optional context. `on_no_key` controls behaviour
    when no provider key is configured: raise | fallback (return content) | skip ('')."""
    if not llm.has_credentials():
        if on_no_key == "raise":
            raise RuntimeError("no LLM API key configured")
        if on_no_key == "fallback":
            return content or ""
        return ""
    user = f"{prompt}\n\n<content>\n{content}\n</content>" if content else prompt
    chosen = (model or "").strip() or llm.model_for("default")
    return llm.complete([{"role": "user", "content": user}], model=chosen, max_tokens=int(max_tokens))


def _p_daylog_pending(ctx, log_title):
    """Parse a log note's dated lines and the per-log watermark; return the days
    still to summarise (encapsulates summarize_day_log's irregular front-end)."""
    from . import workflows as wf
    empty = {"days": [], "watermark_key": None, "last_date": None}
    note = notes_svc.get_by_title(ctx.conn, log_title)
    if not note:
        return empty
    by_date: dict[str, list[str]] = {}
    for line in note["content_md"].splitlines():
        m = wf._DATED_LINE.match(line.strip())
        if m:
            by_date.setdefault(m.group(1), []).append(m.group(2))
    if not by_date:
        return empty
    dates = sorted(by_date)
    current = dates[-1]  # the day still being logged into — leave it alone
    wm_key = f"daylog_summarized:{log_title.lower()}"
    last = get_meta(wm_key)
    todo = [d for d in dates if d < current and (last is None or d > last)]
    return {
        "days": [{"date": d, "entries": by_date[d]} for d in todo],
        "watermark_key": wm_key,
        "last_date": (todo[-1] if todo else None),
    }


def _p_daily_pending(ctx, limit_days=60):
    """Completed days (before local today) that have dated capture entries
    (notes/daily/YYYY/MM/DD/<n>) but no daily summary note yet.

    Idempotency is the EXISTENCE of the day's summary note — no watermark — so a
    restart or multi-day outage simply resumes with whatever days are still
    missing (and backfills them all in one run). Each returned day carries its
    entries' bodies (for summarise_entries) and child titles (for ## Entries
    backlinks). Today is excluded (it's still being written into)."""
    today = _local_day_path()
    rows = ctx.conn.execute(
        "SELECT title, content_md, lat, lon FROM notes "
        "WHERE title LIKE 'notes/daily/%' AND kind = 'entry' AND deleted_at IS NULL "
        "ORDER BY title",
    ).fetchall()
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        parts = r["title"].split("/")           # notes/daily/Y/MM/DD/<n>
        if len(parts) == 6 and parts[5].isdigit():
            by_day.setdefault("/".join(parts[2:5]), []).append(dict(r))

    existing = {
        r["title"][len("notes/daily/"):]
        for r in ctx.conn.execute(
            "SELECT title FROM notes WHERE kind = 'daily' "
            "AND title LIKE 'notes/daily/%' AND deleted_at IS NULL",
        ).fetchall()
    }

    days = []
    for date in sorted(by_day):
        if date >= today or date in existing:   # leave today open; skip already-rolled-up days
            continue
        children = by_day[date]
        days.append({
            "date": date,
            "daily_title": f"notes/daily/{date}",
            "entries": [c["content_md"] for c in children],
            "children": [c["title"] for c in children],
            # Pre-rendered "## Entries" backlink list so the YAML stays a simple
            # substitution (no Jinja loop needed in the recipe).
            "entries_md": "\n".join(f"- [[{c['title']}]]" for c in children),
            # Entries that carry a capture coordinate — candidates for promoting to
            # a named place (suggest_places reads this).
            "located": [
                {"title": c["title"], "content": c["content_md"], "lat": c["lat"], "lon": c["lon"]}
                for c in children if c["lat"] is not None and c["lon"] is not None
            ],
        })
    return {"days": days[: max(1, int(limit_days))], "count": len(days)}


def _p_call_action(ctx, action, config=None, trigger=None):
    """Run another action recipe as a nested sub-pipeline (composition / chaining).
    The sub-recipe gets a FRESH variable scope (its own config/trigger), but the
    recursion depth and step budget are SHARED with the parent so cycles and
    runaways are bounded. A recipe may declare top-level `returns: "{{ expr }}"`
    to hand a value back; otherwise a small summary object is returned."""
    if ctx.depth >= _MAX_CALL_DEPTH:
        raise RuntimeError("call_action: max call depth exceeded")
    recipe = get_action_def(action)
    if recipe is None:
        raise RuntimeError(f"call_action: unknown action '{action}'")
    target = recipe.get("type", action)
    if target in ctx.call_stack:
        raise RuntimeError(f"call_action: cycle through '{target}'")

    sub = _Ctx(ctx.conn, ctx.workflow_id, trigger or {},
               call_stack=ctx.call_stack + [target], depth=ctx.depth + 1, budget=ctx.budget,
               on_step=ctx.on_step)
    scope = {"config": config or {}, "trigger": trigger or {}, "today": _today(), "prompts": _PromptsProxy()}
    trace: list = []
    stopped = None
    try:
        _run_steps(sub, recipe.get("steps", []), scope, trace)
    except _Stop as s:
        stopped = s.message
    ret = _render_value(recipe["returns"], scope) if recipe.get("returns") is not None else None
    return {"type": target, "steps": len(trace), "stopped": stopped, "return": ret}


# --- Re-filing loose notes ("sort unfiled") --------------------------------
# A title IS its path ('/' separates folders; the root is notes/). "Loose" = a
# bare title with no folder at all, or a single segment directly under notes/
# (e.g. notes/SomeThought). Dated daily captures/rollups (notes/daily/…) are
# never touched — they stay as the chronological log.
def _is_loose_title(title: str) -> bool:
    t = (title or "").strip()
    if not t or t == "notes/daily" or t.startswith("notes/daily/"):
        return False
    segs = t.split("/")
    return len(segs) == 1 or (len(segs) == 2 and segs[0] == "notes")


def _p_find_unfiled(ctx, limit=200):
    """Loose, unfiled notes to re-file. Returns a list (so stop_when_empty
    short-circuits when there's nothing to sort)."""
    rows = ctx.conn.execute(
        "SELECT id, title, content_md FROM notes WHERE deleted_at IS NULL ORDER BY id"
    ).fetchall()
    out = [
        {"id": r["id"], "title": r["title"], "snippet": (r["content_md"] or "").strip()[:280]}
        for r in rows if _is_loose_title(r["title"])
    ]
    return out[: max(1, int(limit))]


def _existing_folders(conn) -> list[str]:
    """Folder paths already in use (every ancestor prefix of a note title), minus
    the dated daily tree — the taxonomy to re-file INTO."""
    folders: set[str] = set()
    for r in conn.execute("SELECT title FROM notes WHERE deleted_at IS NULL").fetchall():
        parts = (r["title"] or "").split("/")
        for i in range(1, len(parts)):
            f = "/".join(parts[:i])
            if not f.startswith("notes/daily"):
                folders.add(f)
    return sorted(folders)


def _parse_moves(raw: str, valid_from: set[str]) -> list[dict]:
    """Pull a JSON array of {from,to} out of an LLM reply, keeping only moves whose
    source is a real candidate and whose target is a different, foldered path under
    the notes/ root."""
    m = re.search(r"\[.*\]", raw or "", re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    moves, seen = [], set()
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict):
            continue
        frm = (d.get("from") or d.get("from_title") or "").strip()
        to = (d.get("to") or d.get("to_title") or "").strip().strip("/")
        if frm not in valid_from or not to or to == frm or to in seen:
            continue
        if not to.startswith("notes/"):     # keep every move under the notes/ root
            to = "notes/" + to
        if "/" not in to[len("notes/"):]:   # must land in an actual sub-folder
            continue
        seen.add(to)
        moves.append({"from": frm, "to": to})
    return moves


def _p_plan_moves(ctx, candidates, instructions=None):
    """Ask the LLM to file each loose note into the EXISTING folder structure,
    inventing a new folder only when nothing fits. Returns {moves, count}."""
    items = candidates if isinstance(candidates, list) else []
    if not items:
        return {"moves": [], "count": 0}
    if not llm.has_credentials():
        return {"moves": [], "count": 0, "error": "no LLM API key configured"}
    folder_block = "\n".join(f"- {f}" for f in _existing_folders(ctx.conn)) or "(no folders yet)"
    notes_block = "\n".join(
        f'{i + 1}. "{n["title"]}" — {n.get("snippet", "")[:200]}' for i, n in enumerate(items)
    )
    extra = f"\n\nAdditional guidance: {instructions}" if instructions else ""
    prompt = (
        "You organise a personal knowledge wiki. Each note's title IS its path, with "
        "'/' as the folder separator and 'notes/' as the root.\n\n"
        "EXISTING FOLDERS:\n" + folder_block + "\n\n"
        "LOOSE NOTES TO FILE (current title — content snippet):\n" + notes_block + "\n\n"
        "For each loose note, choose the best destination path. PREFER an existing "
        "folder; invent a new folder ONLY when none fits. Keep the note's own name as "
        "the final path segment and just prepend the right folder(s), all under 'notes/'. "
        "Skip a note that's already well placed.\n\n"
        'Reply with ONLY a JSON array, e.g. [{"from":"notes/Sleep tips","to":"notes/Health/Sleep tips"}]. '
        'Use the EXACT current title as "from".' + extra
    )
    raw = llm.complete([{"role": "user", "content": prompt}], model=llm.model_for("cheap"), max_tokens=2000)
    moves = _parse_moves(raw, {n["title"] for n in items})
    return {"moves": moves, "count": len(moves)}


def _p_stage_moves(ctx, moves):
    """Stage each proposed move as a pending (conversation-less) RENAME action for
    the owner to approve in the staging area — nothing moves until then. Skips
    no-ops, missing sources, and targets that already exist / collide in the batch."""
    staged, seen = 0, set()
    for m in (moves or []):
        if not isinstance(m, dict):
            continue
        cur = (m.get("from") or m.get("from_title") or "").strip()
        new = (m.get("to") or m.get("to_title") or "").strip()
        if not cur or not new or cur == new or new in seen:
            continue
        if not notes_svc.get_by_title(ctx.conn, cur) or notes_svc.get_by_title(ctx.conn, new):
            continue
        seen.add(new)
        ctx.conn.execute(
            "INSERT INTO staging_actions (conversation_id, type, payload_json) VALUES (NULL, ?, ?)",
            ("RENAME", json.dumps({"type": "RENAME", "title": cur, "new_title": new})),
        )
        staged += 1
    return {"staged": staged}


def _p_notify(ctx, title, body="", url="/"):
    """Fire a Web Push to every subscribed device (custom title/body/deep-link).
    Fire-and-forget on its own connection, so it never blocks or fails the pipeline
    transaction. Lets a trigger's action actually reach the owner (e.g. location)."""
    from . import push
    push.notify(str(title)[:120], str(body or "")[:400], str(url or "/"))
    return {"queued": True}


def _existing_place(conn, name, lat, lon, radius_m):
    """True if a place by this name already exists, OR any place sits within this
    one's radius of the coord (so we don't re-suggest a spot already saved)."""
    if conn.execute("SELECT 1 FROM places WHERE name = ? COLLATE NOCASE LIMIT 1", (name,)).fetchone():
        return True
    for p in conn.execute("SELECT lat, lon FROM places").fetchall():
        if geo.haversine_km(lat, lon, p["lat"], p["lon"]) * 1000.0 <= float(radius_m):
            return True
    return False


def _p_suggest_places(ctx, entries):
    """From entries that carry a capture coordinate, ask the LLM which clearly
    happened AT a nameable, reusable place. The LLM only picks an index + a name;
    the COORDINATE comes from the entry (never invented). Returns deduped
    candidates {name, lat, lon, radius_m, source_title}. No-op without an LLM key."""
    items = [e for e in (entries or []) if isinstance(e, dict) and e.get("lat") is not None and e.get("lon") is not None]
    if not items or not llm.has_credentials():
        return {"candidates": []}
    block = "\n".join(
        f'{i + 1}. "{(e.get("title") or "").split("/")[-1]}" — {(e.get("content") or "")[:200]}'
        for i, e in enumerate(items)
    )
    prompt = (
        "These journal entries each have GPS coordinates. Identify ONLY the ones that "
        "clearly happened AT a specific, nameable, REUSABLE place (a gym, a café, an "
        "office, a park) — not a vague area, a one-off, or a place merely mentioned but "
        "not visited. For each such entry, give a short place name (≤ 4 words).\n\n"
        "ENTRIES:\n" + block + "\n\n"
        'Reply with ONLY a JSON array, e.g. [{"index":1,"name":"The Gym"}]. Omit entries '
        "that don't clearly identify a reusable place. If none qualify, reply []."
    )
    raw = llm.complete([{"role": "user", "content": prompt}], model=llm.model_for("cheap"), max_tokens=600)
    m = re.search(r"\[.*\]", raw or "", re.DOTALL)
    if not m:
        return {"candidates": []}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return {"candidates": []}
    out, seen = [], set()
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict):
            continue
        try:
            e = items[int(d.get("index", 0)) - 1]
        except (ValueError, IndexError, TypeError):
            continue
        name = (d.get("name") or "").strip()[:80]
        key = name.lower()
        if not name or key in seen:
            continue
        if _existing_place(ctx.conn, name, e["lat"], e["lon"], 150):
            continue
        seen.add(key)
        out.append({"name": name, "lat": e["lat"], "lon": e["lon"],
                    "radius_m": 150, "source_title": e["title"]})
    return {"candidates": out}


def _p_stage_places(ctx, candidates):
    """Stage each place candidate as a pending ADD_PLACE action for the owner to
    approve (nothing is written to `places` until they tap Apply). Skips ones
    already pending or already saved since suggestion."""
    staged = 0
    for c in (candidates or []):
        if not isinstance(c, dict) or not (c.get("name") and c.get("lat") is not None and c.get("lon") is not None):
            continue
        name = str(c["name"])[:80]
        if _existing_place(ctx.conn, name, c["lat"], c["lon"], c.get("radius_m") or 150):
            continue
        dup = ctx.conn.execute(
            "SELECT 1 FROM staging_actions WHERE type='ADD_PLACE' AND status='pending' "
            "AND json_extract(payload_json, '$.name') = ? COLLATE NOCASE LIMIT 1", (name,)
        ).fetchone()
        if dup:
            continue
        payload = {"type": "ADD_PLACE", "name": name, "lat": c["lat"], "lon": c["lon"],
                   "radius_m": int(c.get("radius_m") or 150), "source_title": c.get("source_title"),
                   "summary": f"Save “{name}” as a place (from [[{c.get('source_title', '')}]])"}
        ctx.conn.execute(
            "INSERT INTO staging_actions (conversation_id, type, payload_json) VALUES (NULL, 'ADD_PLACE', ?)",
            (json.dumps(payload),),
        )
        staged += 1
    return {"staged": staged}


def _p_discover_stays(ctx, min_days=3, days_back=21, min_minutes=30):
    """Find UNLABELED spots the trail shows you returning to across several distinct
    days — a place worth naming. Pure geo-math (no LLM). Groups recurring stays by
    proximity; emits a candidate (coordinate-named placeholder for the owner to
    rename) when one recurs on >= min_days days and isn't already a saved place."""
    from datetime import datetime, timedelta, timezone
    from . import geotrail
    since = (datetime.now(timezone.utc) - timedelta(days=int(days_back or 21))).strftime("%Y-%m-%d %H:%M:%S")
    stays = [s for s in geotrail.stay_points(ctx.conn, since=since, min_min=float(min_minutes or 30))
             if not s.get("label")]
    groups: list[dict] = []
    for s in stays:
        g = next((g for g in groups
                  if geo.haversine_km(g["lat"], g["lon"], s["lat"], s["lon"]) * 1000.0 <= 120), None)
        if g is None:
            groups.append({"lat": s["lat"], "lon": s["lon"], "n": 1, "days": {s["arrived"][:10]}})
        else:
            g["lat"] = (g["lat"] * g["n"] + s["lat"]) / (g["n"] + 1)   # rolling centroid
            g["lon"] = (g["lon"] * g["n"] + s["lon"]) / (g["n"] + 1)
            g["n"] += 1
            g["days"].add(s["arrived"][:10])
    out = []
    for g in groups:
        if len(g["days"]) < int(min_days or 3):
            continue
        if _existing_place(ctx.conn, "", g["lat"], g["lon"], 150):   # already covered by a saved place
            continue
        out.append({"name": f"Frequent spot ({g['lat']:.3f}, {g['lon']:.3f})",
                    "lat": round(g["lat"], 6), "lon": round(g["lon"], 6),
                    "radius_m": 150, "source_title": None})
    return {"candidates": out}


def _p_research_nudges(ctx):
    """Post review-inbox nudges for active research links that have new candidate
    notes the owner hasn't reviewed yet (the approve-to-add tray)."""
    from . import research
    return {"nudged": research.post_candidate_nudges(ctx.conn)}


_PRIMITIVES = {
    "read_note": _p_read_note,
    "call_action": _p_call_action,
    "write_note": _p_write_note,
    "create_review": _p_create_review,
    "semantic_search": _p_semantic_search,
    "analyze_pending": _p_analyze_pending,
    "analyze_note": _p_analyze_note,
    "rebuild_entity_index": _p_rebuild_entity_index,
    "write_disambiguation": _p_write_disambiguation,
    "record_talk": _p_record_talk,
    "flag_dead_links": _p_flag_dead_links,
    "rebuild_article": _p_rebuild_article,
    "check_needed_links": _p_check_needed_links,
    "create_article": _p_create_article,
    "taxonomy_health": _p_taxonomy_health,
    "recategorize_article": _p_recategorize_article,
    "merge_articles": _p_merge_articles,
    "refresh_index": _p_refresh_index,
    "research_article": _p_research_article,
    "split_article": _p_split_article,
    "link_owner": _p_link_owner,
    "review_open_talk": _p_review_open_talk,
    "wiki_maintain": _p_wiki_maintain,
    "wiki_update": _p_wiki_update,
    "flag_ungrounded_reference": _p_flag_ungrounded_reference,
    "redate_notes": _p_redate_notes,
    "title_notes": _p_title_notes,
    "seed_kb_watermark": _p_seed_kb_watermark,
    "write_kb_index": _p_write_kb_index,
    "kb_reset": _p_kb_reset,
    "corpus_digest": _p_corpus_digest,
    "wiki_outline": _p_wiki_outline,
    "wiki_write_batch": _p_wiki_write_batch,
    "validate_structure": _p_validate_structure,
    "query_notes": _p_query_notes,
    "query_entry_changes": _p_query_entry_changes,
    "get_meta": _p_get_meta,
    "set_meta": _p_set_meta,
    "set_tags": _p_set_tags,
    "suggest_tags": _p_suggest_tags,
    "summarise_entries": _p_summarise_entries,
    "wiki_plan": _p_wiki_plan,
    "validate_citations": _p_validate_citations,
    "kb_old_citation_pending": _p_kb_old_citation_pending,
    "kb_audit": _p_kb_audit,
    "recite_articles": _p_recite_articles,
    "kb_uncited_pending": _p_kb_uncited_pending,
    "mark_evaluated": _p_mark_evaluated,
    "chatter_pending": _p_chatter_pending,
    "cluster_chatter": _p_cluster_chatter,
    "mark_promoted": _p_mark_promoted,
    "stage_kb_proposals": _p_stage_kb_proposals,
    "daylog_pending": _p_daylog_pending,
    "daily_pending": _p_daily_pending,
    "gather_context": _p_gather_context,
    "llm": _p_llm,
    "find_unfiled": _p_find_unfiled,
    "plan_moves": _p_plan_moves,
    "stage_moves": _p_stage_moves,
    "research_nudges": _p_research_nudges,
    "notify": _p_notify,
    "suggest_places": _p_suggest_places,
    "stage_places": _p_stage_places,
    "discover_stays": _p_discover_stays,
}


# Machine-readable contract for each primitive — drives the editor's step palette
# and the recipe validator. Hand-maintained; a test pins its keys + input names to
# _PRIMITIVES (and the function signatures) so it can't drift.
_PRIMITIVE_META: dict[str, dict] = {
    "read_note": {"summary": "Read a note by title or id.",
                  "inputs": [{"name": "title", "type": "str"}, {"name": "id", "type": "int"}],
                  "output": "object"},
    "call_action": {"summary": "Run another action recipe as a sub-pipeline (chaining).",
                    "inputs": [{"name": "action", "type": "str", "required": True},
                               {"name": "config", "type": "object"}, {"name": "trigger", "type": "object"}],
                    "output": "object"},
    "write_note": {"summary": "Create or update a note (versioned).",
                   "inputs": [{"name": "title", "type": "str", "required": True},
                              {"name": "content_md", "type": "str"}, {"name": "text", "type": "str"},
                              {"name": "mode", "type": "enum", "choices": ["replace", "append"]},
                              {"name": "kind", "type": "str"}, {"name": "version_note", "type": "str"}],
                   "output": "object"},
    "create_review": {"summary": "Post a card to the Review inbox.",
                      "inputs": [{"name": "title", "type": "str", "required": True},
                                 {"name": "message", "type": "str"}, {"name": "link_title", "type": "str"}],
                      "output": "object"},
    "semantic_search": {"summary": "Vector search over notes, with optional kind/tag/recency filters and a relevance floor.",
                        "inputs": [{"name": "query", "type": "str", "required": True},
                                   {"name": "limit", "type": "int"},
                                   {"name": "kind", "type": "str"},
                                   {"name": "tags", "type": "list"},
                                   {"name": "since", "type": "str"},
                                   {"name": "max_distance", "type": "float"}], "output": "list"},
    "find_unfiled": {"summary": "List loose notes (no folder, or one level under notes/) to re-file.",
                     "inputs": [{"name": "limit", "type": "int"}], "output": "list"},
    "plan_moves": {"summary": "LLM-propose a destination folder for each loose note (reusing existing folders).",
                   "inputs": [{"name": "candidates", "type": "list", "required": True},
                              {"name": "instructions", "type": "str"}], "output": "object"},
    "stage_moves": {"summary": "Stage proposed note moves as pending RENAME actions for approval.",
                    "inputs": [{"name": "moves", "type": "list", "required": True}], "output": "object"},
    "research_nudges": {"summary": "Nudge the owner about new candidate notes for active research links.",
                        "inputs": [], "output": "object"},
    "notify": {"summary": "Send a Web Push to all devices (custom title/body/deep-link).",
               "inputs": [{"name": "title", "type": "str", "required": True}, {"name": "body", "type": "str"},
                          {"name": "url", "type": "str"}], "output": "object"},
    "suggest_places": {"summary": "From located entries, LLM-pick which clearly name a reusable place (coords from the entry).",
                       "inputs": [{"name": "entries", "type": "list", "required": True}], "output": "object"},
    "stage_places": {"summary": "Stage place candidates as pending ADD_PLACE actions for owner approval.",
                     "inputs": [{"name": "candidates", "type": "list", "required": True}], "output": "object"},
    "discover_stays": {"summary": "Find unlabeled spots the trail shows you revisiting across several days (place candidates).",
                       "inputs": [{"name": "min_days", "type": "int"}, {"name": "days_back", "type": "int"},
                                  {"name": "min_minutes", "type": "int"}], "output": "object"},
    "analyze_pending": {"summary": "Ids of entry/daily notes whose AI analysis is missing or stale (or all, if force).",
                        "inputs": [{"name": "limit", "type": "int"}, {"name": "force", "type": "bool"}], "output": "list"},
    "analyze_note": {"summary": "(Re)compute one note's AI analysis sidecar (no-op if unchanged, unless force).",
                     "inputs": [{"name": "id", "type": "int", "required": True}, {"name": "force", "type": "bool"}], "output": "dict"},
    "rebuild_entity_index": {"summary": "Aggregate the canonical entity index from note_analysis.",
                             "inputs": [{"name": "limit", "type": "int"}], "output": "dict"},
    "write_disambiguation": {"summary": "Generate kb/_disambig pages for terms mapping to multiple articles.",
                             "inputs": [], "output": "dict"},
    "record_talk": {"summary": "Record an article's writer talk entries (decisions/conflicts/questions).",
                    "inputs": [{"name": "article_title", "type": "str", "required": True},
                               {"name": "entries", "type": "list"}], "output": "dict"},
    "flag_dead_links": {"summary": "Flag dangling kb cross-links as todos on each article's talk.",
                        "inputs": [], "output": "dict"},
    "rebuild_article": {"summary": "Regenerate ONE kb article in place from its source notes (manual).",
                        "inputs": [{"name": "title", "type": "str"},
                                   {"name": "instructions", "type": "str"}], "output": "dict"},
    "check_needed_links": {"summary": "Deterministically find/add missing kb cross-links (propose|auto).",
                        "inputs": [{"name": "title", "type": "str"},
                                   {"name": "mode", "type": "str"}], "output": "dict"},
    "create_article": {"summary": "Create ONE new-subject kb article from its notes (dedup before spawn).",
                        "inputs": [{"name": "subject", "type": "str"}, {"name": "etype", "type": "str"},
                                   {"name": "min_notes", "type": "int"}], "output": "dict"},
    "taxonomy_health": {"summary": "Read-only KB taxonomy-drift report (orphans, un-foldered Reference).",
                        "inputs": [], "output": "dict"},
    "recategorize_article": {"summary": "Move/rename a kb article (rewrites inbound links + index).",
                        "inputs": [{"name": "title", "type": "str"}, {"name": "new_title", "type": "str"}], "output": "dict"},
    "merge_articles": {"summary": "Fold kb articles into another (union sources, rewrite inbound links).",
                        "inputs": [{"name": "sources", "type": "list"}, {"name": "into", "type": "str"}], "output": "dict"},
    "refresh_index": {"summary": "Rebuild kb/_index from live articles (no LLM).",
                        "inputs": [], "output": "dict"},
    "research_article": {"summary": "Propose related Reference links for an article (semantic, corroborated).",
                        "inputs": [{"name": "title", "type": "str"}, {"name": "mode", "type": "str"}], "output": "dict"},
    "split_article": {"summary": "Spin a child article off a parent from given source notes.",
                        "inputs": [{"name": "parent", "type": "str"}, {"name": "child", "type": "str"},
                                   {"name": "child_sources", "type": "list"}], "output": "dict"},
    "link_owner": {"summary": "Link the default person to their People article.",
                   "inputs": [], "output": "dict"},
    "review_open_talk": {"summary": "Post a Review card per article with unresolved talk items.",
                         "inputs": [{"name": "limit", "type": "int"}], "output": "dict"},
    "wiki_maintain": {"summary": "Address open talk items on existing articles against their sources.",
                      "inputs": [{"name": "limit", "type": "int"}], "output": "dict"},
    "wiki_update": {"summary": "Flow notes changed since the watermark into existing articles (incremental).",
                    "inputs": [{"name": "limit", "type": "int"}], "output": "dict"},
    "flag_ungrounded_reference": {"summary": "Flag Reference articles padded with LLM common knowledge vs the notes.",
                                  "inputs": [], "output": "dict"},
    "redate_notes": {"summary": "File loose entry notes under the flat dated tree notes/YYYY/MM/DD/N.",
                     "inputs": [{"name": "limit", "type": "int"}, {"name": "dry_run", "type": "bool"}], "output": "dict"},
    "title_notes": {"summary": "Give bare dated notes a generated leaf title (notes/<date>/N - title).",
                    "inputs": [{"name": "limit", "type": "int"}, {"name": "dry_run", "type": "bool"}], "output": "dict"},
    "seed_kb_watermark": {"summary": "Reset the incremental-update watermark to now (after a full build).",
                          "inputs": [], "output": "dict"},
    "write_kb_index": {"summary": "Write kb/_index from the saved articles (excludes quarantined).",
                       "inputs": [{"name": "articles", "type": "list", "required": True},
                                  {"name": "valid", "type": "list", "required": True}], "output": "dict"},
    "kb_reset": {"summary": "Soft-delete all kb/ articles except protected kb/_* pages; clear synthesis markers.",
                 "inputs": [], "output": "dict"},
    "corpus_digest": {"summary": "Compact survey (gist/domain/entities per note) for the outline.",
                      "inputs": [{"name": "limit", "type": "int"}], "output": "list"},
    "wiki_outline": {"summary": "Survey -> entity-first taxonomy (articles + assigned sources + index).",
                     "inputs": [{"name": "digest", "type": "list", "required": True},
                                {"name": "instructions", "type": "str"}], "output": "dict"},
    "wiki_write_batch": {"summary": "Write every planned article (draft + revise + lint); valid vs quarantined.",
                         "inputs": [{"name": "articles", "type": "list", "required": True},
                                    {"name": "instructions", "type": "str"}], "output": "dict"},
    "validate_structure": {"summary": "Lint one article against its domain guide's spec.",
                           "inputs": [{"name": "title", "type": "str", "required": True},
                                      {"name": "content_md", "type": "str"}], "output": "dict"},
    "query_notes": {"summary": "List notes by kind / since id.",
                    "inputs": [{"name": "kind", "type": "str"}, {"name": "since_id", "type": "int"},
                               {"name": "limit", "type": "int"}], "output": "list"},
    "query_entry_changes": {"summary": "Daily rollups + legacy entries changed (new/edited/deleted) since a timestamp.",
                            "inputs": [{"name": "since", "type": "str"}, {"name": "limit", "type": "int"}],
                            "output": "list"},
    "get_meta": {"summary": "Read a stored key (e.g. a watermark).",
                 "inputs": [{"name": "key", "type": "str", "required": True}, {"name": "default", "type": "str"}],
                 "output": "scalar"},
    "set_meta": {"summary": "Write a stored key.",
                 "inputs": [{"name": "key", "type": "str", "required": True}, {"name": "value", "type": "str", "required": True}],
                 "output": "none"},
    "set_tags": {"summary": "Set a note's tags.",
                 "inputs": [{"name": "note_id", "type": "int", "required": True}, {"name": "tags", "type": "list"}],
                 "output": "list"},
    "suggest_tags": {"summary": "Ask the LLM for tags for a note.",
                     "inputs": [{"name": "title", "type": "str", "required": True},
                                {"name": "content", "type": "str", "required": True}, {"name": "prompt", "type": "str"}],
                     "output": "list"},
    "summarise_entries": {"summary": "Summarise a list of log lines (LLM, with no-key fallback).",
                          "inputs": [{"name": "entries", "type": "list", "required": True}, {"name": "prompt", "type": "str"}],
                          "output": "scalar"},
    "wiki_plan": {"summary": "Ask the LLM to fold entries into KB notes; returns [{op,title,content_md}].",
                  "inputs": [{"name": "entries", "type": "list", "required": True},
                             {"name": "existing_kb", "type": "list", "required": True}, {"name": "instructions", "type": "str"}],
                  "output": "list"},
    "validate_citations": {"summary": "Split a wiki_plan into citation-valid vs quarantined (malformed footnotes).",
                           "inputs": [{"name": "articles", "type": "list", "required": True}],
                           "output": "object"},
    "kb_old_citation_pending": {"summary": "KB articles still in the old citation style (not yet footnoted).",
                                "inputs": [{"name": "limit", "type": "int"}], "output": "object"},
    "kb_audit": {"summary": "Read-only lint of every KB article: citation integrity, broken [[links]], formatting drift.",
                 "inputs": [{"name": "limit", "type": "int"}], "output": "object"},
    "recite_articles": {"summary": "LLM-reformat articles to footnote citations; reject ones that drop a link.",
                        "inputs": [{"name": "articles", "type": "list", "required": True}], "output": "object"},
    "kb_uncited_pending": {"summary": "Entries no kb article cites and synthesis hasn't evaluated.",
                           "inputs": [{"name": "limit", "type": "int"}, {"name": "since_floor", "type": "str"},
                                      {"name": "reconsider", "type": "bool"}], "output": "object"},
    "mark_evaluated": {"summary": "Mark entries as considered by synthesis (per-entry watermark).",
                       "inputs": [{"name": "ids", "type": "list", "required": True}, {"name": "at", "type": "str"}],
                       "output": "object"},
    "chatter_pending": {"summary": "Uncited 'dropped chatter' entries for recurrence detection.",
                        "inputs": [{"name": "limit", "type": "int"}], "output": "object"},
    "cluster_chatter": {"summary": "Cluster chatter by similarity; clusters spanning >= min_days are patterns.",
                        "inputs": [{"name": "entries", "type": "list", "required": True},
                                   {"name": "tau", "type": "float"}, {"name": "min_days", "type": "int"},
                                   {"name": "neighbours", "type": "int"}, {"name": "promote_limit", "type": "int"}],
                        "output": "object"},
    "mark_promoted": {"summary": "Mark chatter entries consumed by a pattern promotion.",
                      "inputs": [{"name": "ids", "type": "list", "required": True}, {"name": "key", "type": "str"}],
                      "output": "object"},
    "stage_kb_proposals": {"summary": "Stage synthesized KB articles as pending actions for owner review.",
                           "inputs": [{"name": "articles", "type": "list", "required": True},
                                      {"name": "conversation_id", "type": "int"}], "output": "object"},
    "daylog_pending": {"summary": "Days of a log still to summarise (+ watermark).",
                       "inputs": [{"name": "log_title", "type": "str", "required": True}], "output": "object"},
    "daily_pending": {"summary": "Completed days with dated entries but no daily summary yet.",
                      "inputs": [{"name": "limit_days", "type": "int"}], "output": "object"},
    "gather_context": {"summary": "Build context text from a note or a semantic search.",
                       "inputs": [{"name": "source_title", "type": "str"}, {"name": "context_query", "type": "str"}],
                       "output": "scalar"},
    "llm": {"summary": "Run an LLM prompt over optional context.",
            "inputs": [{"name": "prompt", "type": "str", "required": True}, {"name": "content", "type": "str"},
                       {"name": "max_tokens", "type": "int"},
                       {"name": "on_no_key", "type": "enum", "choices": ["raise", "fallback", "skip"]},
                       {"name": "model", "type": "str"}],
            "output": "scalar"},
}


def primitive_catalog() -> list[dict]:
    """The step palette: each primitive + its declared inputs + output shape."""
    return [{"name": n, **_PRIMITIVE_META.get(n, {"inputs": [], "output": "scalar"})}
            for n in sorted(_PRIMITIVES)]


# --- Interpreter ------------------------------------------------------------

class _Stop(Exception):
    """Carries an early-return message out of nested steps."""
    def __init__(self, message: str):
        self.message = message


def _run_steps(ctx, steps, scope, trace):
    for step in steps:
        when = step.get("when")
        if when is not None and not _truthy(_eval(when, scope)):
            continue

        stop_when = step.get("stop_when")
        if stop_when is not None and _truthy(_eval(stop_when, scope)):
            raise _Stop(str(step.get("stop_message", "stopped")))

        if step.get("do") is None and "for_each" not in step:
            continue  # control-only step (when/stop_when)

        ctx.budget[0] -= 1
        if ctx.budget[0] < 0:
            raise RuntimeError("step budget exceeded (runaway recipe?)")

        if "for_each" in step:
            coll = _eval(step["for_each"], scope) or []
            for item in coll:
                child = dict(scope)
                child["item"] = item
                _run_steps(ctx, step.get("steps", []), child, trace)
            if step.get("id"):
                scope[step["id"]] = {"count": len(coll)}
            continue

        prim = _PRIMITIVES.get(step.get("do"))
        if prim is None:
            raise RuntimeError(f"unknown primitive '{step.get('do')}'")
        args = {k: _render_value(v, scope) for k, v in (step.get("with") or {}).items()}
        if ctx.on_step:                       # live "watch" progress (best-effort)
            try:
                ctx.on_step(step.get("do"))
            except Exception:  # noqa: BLE001
                pass
        out = prim(ctx, **args)
        if step.get("id"):
            scope[step["id"]] = out
        trace.append(step["do"])

        swe = step.get("stop_when_empty")
        if swe is not None and not _truthy(out):
            raise _Stop(str(swe))


def run_pipeline(conn, recipe: dict, cfg: dict, workflow_id, context: dict | None, *, on_step=None) -> str:
    ctx = _Ctx(conn, workflow_id, context, on_step=on_step)
    scope = {
        "config": cfg or {},
        "trigger": context or {},
        "today": _today(),
        "prompts": _PromptsProxy(),
    }
    trace: list[str] = []
    try:
        _run_steps(ctx, recipe.get("steps", []), scope, trace)
    except _Stop as s:
        return s.message
    return f"ran {len(trace)} step(s): " + ", ".join(trace) if trace else "ok"


class _PromptsProxy:
    """Read-only `prompts.<section>.<key>` access inside templates (DB-override
    aware), so recipes can reference the shared prompt store."""
    def __init__(self, prefix: str = ""):
        self._prefix = prefix

    def __getattr__(self, name):
        from . import prompts as _prompts
        key = f"{self._prefix}{name}"
        val = _prompts.get(key)
        return val if val else _PromptsProxy(key + ".")

    def __str__(self):
        from . import prompts as _prompts
        return _prompts.get(self._prefix.rstrip(".")) or ""


# --- Definition loading & validation ---------------------------------------

# Repo files are the seed; the action_defs DB table is the editable source of
# truth (DB-first). _REPO_DEFS / _ALIASES are the file layer (read-only, used for
# seeding, fallback, and alias resolution). _cache is a per-thread parsed-recipe
# cache keyed by (type -> (updated_at, recipe)) so we don't re-parse YAML each run
# but still see committed edits (we read updated_at from the DB every time).
_REPO_DEFS: dict | None = None
_ALIASES: dict[str, str] = {}  # legacy action_type -> canonical type
_cache = threading.local()


def _actions_dir() -> Path | None:
    for c in (
        os.environ.get("JBRAIN_ACTIONS_DIR"),
        Path(__file__).resolve().parents[3] / "actions",  # repo root
        Path("/app/actions"),                              # container
    ):
        if c and Path(c).is_dir():
            return Path(c)
    return None


def _load_repo() -> dict:
    """Parse actions/*.yaml into {type: recipe} and rebuild the alias map."""
    global _REPO_DEFS, _ALIASES
    defs: dict = {}
    aliases: dict[str, str] = {}
    d = _actions_dir()
    if d:
        for path in sorted(d.glob("*.yaml")):
            try:
                doc = yaml.safe_load(path.read_text())
            except Exception:
                continue
            if doc and doc.get("type") and isinstance(doc.get("steps"), list):
                defs[doc["type"]] = doc
                for a in (doc.get("aliases") or []):
                    aliases[a] = doc["type"]
    _REPO_DEFS = defs
    _ALIASES = aliases
    return defs


def _repo_defs() -> dict:
    if _REPO_DEFS is None:
        _load_repo()
    return _REPO_DEFS


# Public names kept for tests/validation that operate on the repo files.
def load_action_defs() -> dict:
    return _load_repo()


def reload_action_defs() -> dict:
    return _load_repo()


def ingest_repo_action_defs(conn) -> int:
    """Seed/update the action_defs table from actions/*.yaml. Mirrors the
    workflows ingest: insert new types, refresh unlocked ones whose repo file
    changed, leave user-locked rows untouched. Stores the raw YAML verbatim."""
    d = _actions_dir()
    if not d:
        return 0
    count = 0
    for path in sorted(d.glob("*.yaml")):
        try:
            text = path.read_text()
            doc = yaml.safe_load(text)
        except Exception:
            continue
        if not (doc and doc.get("type") and isinstance(doc.get("steps"), list)):
            continue
        t = doc["type"]
        h = hashlib.sha256(text.encode()).hexdigest()
        existing = conn.execute(
            "SELECT locked, origin_hash FROM action_defs WHERE type = ?", (t,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO action_defs (type, recipe_yaml, source, locked, origin_hash) "
                "VALUES (?, ?, 'repo', 0, ?)", (t, text, h),
            )
            count += 1
        elif not existing["locked"] and existing["origin_hash"] != h:
            conn.execute(
                "UPDATE action_defs SET recipe_yaml = ?, origin_hash = ?, "
                "updated_at = datetime('now') WHERE type = ?", (text, h, t),
            )
            count += 1
    conn.commit()
    return count


def get_action_def(action_type: str) -> dict | None:
    """Resolve a recipe (DB-first, alias-aware). Falls back to the repo file only
    before the table is seeded. Uses a per-thread (type, updated_at) cache to
    avoid re-parsing YAML while still reflecting committed edits."""
    _repo_defs()  # ensure the alias map is loaded
    canonical = _ALIASES.get(action_type, action_type)
    try:
        row = get_conn().execute(
            "SELECT recipe_yaml, updated_at FROM action_defs WHERE type = ?", (canonical,)
        ).fetchone()
    except Exception:
        row = None
    if row is None:
        return _repo_defs().get(canonical)
    cache = getattr(_cache, "d", None)
    if cache is None:
        cache = {}
        _cache.d = cache
    hit = cache.get(canonical)
    if hit and hit[0] == row["updated_at"]:
        return hit[1]
    try:
        recipe = yaml.safe_load(row["recipe_yaml"]) or {}
    except Exception:
        recipe = _repo_defs().get(canonical)
    cache[canonical] = (row["updated_at"], recipe)
    return recipe


def action_types() -> list[str]:
    """Every action type: DB rows ∪ repo files (DB is authoritative once seeded)."""
    types = set(_repo_defs().keys())
    try:
        for r in get_conn().execute("SELECT type FROM action_defs"):
            types.add(r["type"])
    except Exception:
        pass
    return sorted(types)


_SCOPE_BASE = {"config", "trigger", "today", "prompts"}


def _expr_names(text) -> set:
    """Top-level variable names referenced in a value/expression (bare or {{ }})."""
    s = str(text).strip()
    if "{{" not in s and "{%" not in s:
        s = "{{ " + s + " }}"  # bare expr (when / for_each / stop_when)
    try:
        return set(_jinja_meta.find_undeclared_variables(_env.parse(s)))
    except Exception:
        return set()


def _value_names(val) -> set:
    if isinstance(val, str):
        return _expr_names(val) if "{{" in val else set()
    if isinstance(val, dict):
        return set().union(*(_value_names(v) for v in val.values())) if val else set()
    if isinstance(val, list):
        return set().union(*(_value_names(v) for v in val)) if val else set()
    return set()


def validate_recipe(recipe) -> list[str]:
    """Soft lint of a recipe: unknown primitive, unknown input, malformed
    for_each, and references to variables not yet in scope. Warnings only — the
    Jinja runtime tolerates missing names (ChainableUndefined → None)."""
    if not isinstance(recipe, dict):
        return ["recipe must be a mapping"]
    warnings: list[str] = []
    if not recipe.get("type"):
        warnings.append("missing 'type'")
    steps = recipe.get("steps")
    if not isinstance(steps, list):
        return warnings + ["'steps' must be a list"]

    def walk(steps, scope: set):
        scope = set(scope)
        for step in steps:
            if not isinstance(step, dict):
                warnings.append("each step must be a mapping")
                continue
            refs: set = set()
            for k in ("when", "for_each", "stop_when"):
                if step.get(k) is not None:
                    refs |= _expr_names(step[k])
            refs |= _value_names(step.get("with") or {})
            for name in sorted(refs - scope):
                warnings.append(f"references unknown variable '{name}'")
            if "for_each" in step:
                if not isinstance(step.get("steps"), list):
                    warnings.append("for_each step missing nested 'steps'")
                else:
                    walk(step["steps"], scope | {"item"})
            else:
                do = step.get("do")
                if do is not None:
                    if do not in _PRIMITIVES:
                        warnings.append(f"unknown primitive '{do}'")
                    else:
                        valid = {i["name"] for i in _PRIMITIVE_META.get(do, {}).get("inputs", [])}
                        for k in (step.get("with") or {}):
                            if valid and k not in valid:
                                warnings.append(f"{do}: unknown input '{k}'")
            if step.get("id"):
                scope.add(step["id"])

    walk(steps, set(_SCOPE_BASE))
    return warnings


def validate_action_defs() -> list[str]:
    """Boot lint over the shipped repo recipes (warnings printed at startup)."""
    out: list[str] = []
    for name, recipe in _load_repo().items():
        out += [f"{name}: {w}" for w in validate_recipe(recipe)]
    return out
