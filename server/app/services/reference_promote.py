"""Promote repeated reference candidates into STAGED kb/Reference articles (nightly).

For each candidate looked up enough times (hits >= threshold) that the owner doesn't already have,
build a SHORT reference stub and STAGE it (a pending staging_actions CREATE) for the owner to approve
— it never goes live unreviewed, and reference_lookup (internal-first) then answers locally next time.

SAFETY (load-bearing):
- The stub is built DETERMINISTICALLY from the public source only (topic + the host-pinned NLM URL +
  the public-domain summary) — NO LLM synthesis, so it can't hallucinate, and NOTHING from the
  conversation / the owner's notes / labs is an input. A "general" reference page inherits no PHI.
- It is STAGED, never written live (unlike the owner's own KB synthesis): it routes through
  staging_actions like every other proposed write, so the owner approves it in the normal tray.
- It records the source URL + fetch date so it can be refreshed when it goes stale.
"""
from __future__ import annotations

import json

from . import reference_candidates as rc

_CATEGORIES = {"Conditions", "Medications", "Procedures", "Concepts", "Events"}


def _category(cand) -> str:
    c = (cand["category"] or "").strip().title() if cand["category"] else ""
    return c if c in _CATEGORIES else "Conditions"


def _title_for(cand) -> str:
    leaf = (cand["topic"] or "").replace("/", " ").strip()
    return f"kb/Reference/Medicine/{_category(cand)}/{leaf}"


def _stub(cand, url: str, snippet: str, fetched: str) -> str:
    leaf = (cand["topic"] or "").replace("/", " ").strip()
    body = f"# {leaf}\n\n"
    body += ("_Reference seed — general background drafted from a health topic you looked up. "
             "Not medical advice; not endorsed by NLM. Enrich it from your own notes over time._\n\n")
    if snippet:
        body += snippet.strip() + "\n\n"
    body += (f"**Source:** [MedlinePlus — {leaf}]({url}) — fetched {fetched}. "
             "U.S. National Library of Medicine, public domain.\n")
    return body


def run(conn, min_hits: int = 2, limit: int = 5) -> dict:
    """Stage up to `limit` reference stubs for candidates with hits >= `min_hits`. Posts one review
    card if anything was staged. Returns {promoted, titles}."""
    from . import medref, reviews
    from . import clock
    rows = conn.execute(
        "SELECT * FROM reference_candidates WHERE status='new' AND hits >= ? "
        "ORDER BY hits DESC, last_seen DESC LIMIT ?", (max(1, int(min_hits)), max(1, int(limit)))).fetchall()
    promoted: list[str] = []
    today = clock.today_iso() if hasattr(clock, "today_iso") else None
    # Titles already pending in staging — don't double-stage the same reference.
    pending_titles = set()
    for r in conn.execute("SELECT payload_json FROM staging_actions WHERE status='pending' AND type='CREATE'"):
        try:
            pending_titles.add(json.loads(r["payload_json"]).get("title"))
        except Exception:  # noqa: BLE001
            pass
    for cand in rows:
        # Re-dedup: the owner may have created the article since capture.
        if rc.has_reference_article(conn, cand["topic"], cand["norm_key"]):
            conn.execute("UPDATE reference_candidates SET status='published' WHERE id=?", (cand["id"],))
            continue
        title = _title_for(cand)
        if title in pending_titles:                # already pending in staging — don't double-stage
            conn.execute("UPDATE reference_candidates SET status='staged' WHERE id=?", (cand["id"],))
            continue
        # Re-fetch fresh (cached → fast) so the cited summary is current; fall back to what we captured.
        fresh = medref.health_topic(conn, cand["topic"]) or {}
        url = fresh.get("url") or cand["url"]
        snippet = fresh.get("snippet") or (cand["snippet"] or "")
        fetched = (today or (cand["last_seen"] or "")[:10])
        payload = {"type": "CREATE", "title": title, "content": _stub(cand, url, snippet, fetched), "kind": "kb"}
        conn.execute("INSERT INTO staging_actions (conversation_id, type, payload_json) VALUES (NULL, 'CREATE', ?)",
                     (json.dumps(payload),))
        conn.execute("UPDATE reference_candidates SET status='staged' WHERE id=?", (cand["id"],))
        promoted.append(title)
    if promoted:
        reviews.create_review_item(
            conn, None,
            title=f"{len(promoted)} reference draft(s) from your lookups",
            message=(f"Drafted {len(promoted)} kb/Reference article(s) from health topics you looked up "
                     "repeatedly — review & approve them in the staging tray. Each is a short, cited seed "
                     "(general background, not medical advice) you can enrich from your own notes."),
            link_slug="kb/_index")
    conn.commit()
    return {"promoted": len(promoted), "titles": promoted}
