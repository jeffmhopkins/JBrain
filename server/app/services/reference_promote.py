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

# The cited source is written as a marked block so the nightly staleness pass (reference_refresh) can
# re-seat ONLY the citation (URL + fetch date) without touching anything the owner enriched:
#   <!-- refsrc --> **Source:** [...](url) — fetched DATE. …
#   <!-- refseed src="medlineplus|rxnorm" url="…" fetched="DATE" -->   (machine-readable provenance)
_SRC_MARK = "<!-- refsrc -->"


def _source_of(cand) -> str:
    """Return the provenance source key for re-fetching a candidate's citation.

    Args:
        cand: Reference candidate row (dict-like with 'source' and 'category' fields).

    Returns:
        'rxnorm' for drug candidates (re-fetched via MedlinePlus Connect), else
        'medlineplus'.
    """
    try:
        src = cand["source"]
    except (KeyError, IndexError):
        src = None
    return (src or "").strip() or ("rxnorm" if _is_medication(cand) else "medlineplus")


def _source_block(source: str, url: str, fetched: str, leaf: str) -> str:
    """Build the cited-source block: a visible attribution line plus a machine-readable marker.

    Shared with reference_refresh so a re-seated citation keeps the exact same shape
    and is parseable by _provenance().

    Args:
        source: Provenance source key ('medlineplus' or 'rxnorm').
        url: Public source URL.
        fetched: ISO date string when the source was fetched.
        leaf: Human-readable topic name used in the visible link text.

    Returns:
        Multi-line string with the <!-- refsrc --> visible line and <!-- refseed --> marker.
    """
    return (f"{_SRC_MARK} **Source:** [MedlinePlus — {leaf}]({url}) — fetched {fetched}. "
            "U.S. National Library of Medicine, public domain.\n"
            f'<!-- refseed src="{source}" url="{url}" fetched="{fetched}" -->\n')


def _category(cand) -> str:
    """Return the title-cased category for a candidate, defaulting to 'Conditions'.

    Args:
        cand: Reference candidate row with a 'category' field.

    Returns:
        Category string from _CATEGORIES, or 'Conditions' if absent/unrecognised.
    """
    c = (cand["category"] or "").strip().title() if cand["category"] else ""
    return c if c in _CATEGORIES else "Conditions"


def _title_for(cand) -> str:
    """Derive the kb/Reference article title for a candidate.

    Args:
        cand: Reference candidate row.

    Returns:
        Wiki page title string (e.g. 'kb/Reference/Medicine/Conditions/Aspirin').
    """
    leaf = (cand["topic"] or "").replace("/", " ").strip()
    return f"kb/Reference/Medicine/{_category(cand)}/{leaf}"


def _is_medication(cand) -> bool:
    """Return True if a candidate is a drug (captured via drug_reference).

    Drug candidates are re-fetched differently from health topics: via RxNorm ->
    MedlinePlus Connect rather than the health-topics search.

    Args:
        cand: Reference candidate row.

    Returns:
        True if the candidate's source is 'rxnorm' or its category is 'Medications'.
    """
    try:
        src = cand["source"]
    except (KeyError, IndexError):
        src = None
    return (src or "") == "rxnorm" or _category(cand) == "Medications"


def _stub(cand, url: str, snippet: str, fetched: str) -> str:
    """Build the initial markdown body for a reference seed article.

    Args:
        cand: Reference candidate row supplying topic and category.
        url: Public source URL to cite.
        snippet: Short public-domain summary text to include (may be empty).
        fetched: ISO date string for the citation fetch date.

    Returns:
        Markdown string ready to be staged as a new kb/Reference article.
    """
    leaf = (cand["topic"] or "").replace("/", " ").strip()
    kind = "medication" if _is_medication(cand) else "health topic"
    body = f"# {leaf}\n\n"
    body += (f"_Reference seed — general background drafted from a {kind} you looked up. "
             "Not medical advice; not endorsed by NLM. Enrich it from your own notes over time._\n\n")
    if snippet:
        body += snippet.strip() + "\n\n"
    body += _source_block(_source_of(cand), url, fetched, leaf)
    return body


def run(conn, min_hits: int = 2, limit: int = 5) -> dict:
    """Stage reference stub articles for candidates that have been looked up enough times.

    Processes up to ``limit`` candidates with hits >= ``min_hits`` that are not already
    owned or pending. Re-fetches each source (cached), builds a deterministic stub, and
    inserts a staging_actions CREATE row for owner review. Posts one review card if
    anything was staged.

    Args:
        conn: SQLite connection (commits internally).
        min_hits: Minimum lookup count before a candidate is promoted.
        limit: Maximum candidates to process in one run.

    Returns:
        Dict with keys: promoted (int), titles (list[str]).
    """
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
        # Re-fetch fresh (cached → fast) so the cited link/summary is current; fall back to what we
        # captured. A drug candidate resolves via RxNorm→MedlinePlus Connect, a topic via health-topics.
        fresh = (medref.drug_topic(conn, cand["topic"]) if _is_medication(cand)
                 else medref.health_topic(conn, cand["topic"])) or {}
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
