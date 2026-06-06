"""Reference-candidate capture: a TOPIC-ONLY usage signal.

When the Analyze-mode `medical_reference` tool surfaces an external health topic the owner doesn't
yet have under kb/Reference, we record the TOPIC (and its public source URL) so the nightly promote
pass can build it into the owner's own curated library over time.

PRIVACY (load-bearing): this records ONLY the public topic name + the server-returned source URL +
its public summary — NEVER the owner's query text, lab values, dates, names, or any conversation id.
A "general" reference page must be able to inherit nothing owner-specific. Recording a candidate
NEVER writes a note (Analyze is read-only over notes); it only enqueues a usage signal.
"""
from __future__ import annotations


def _norm(topic: str) -> str:
    from . import entity_index
    try:
        return entity_index.normalize(topic) or ""
    except Exception:  # noqa: BLE001
        return " ".join((topic or "").lower().split())


def has_reference_article(conn, topic: str, norm: str | None = None) -> bool:
    """True if the owner already has a kb/Reference/… article whose leaf normalizes to this topic
    (the loop has already closed — don't re-capture)."""
    norm = norm or _norm(topic)
    if not norm:
        return False
    for r in conn.execute("SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL "
                          "AND title LIKE 'kb/Reference/%'"):
        if _norm((r["title"] or "").split("/")[-1]) == norm:
            return True
    return False


def record(conn, *, topic: str, source: str = "medlineplus", url: str, snippet: str = "",
           category: str | None = None) -> None:
    """Enqueue/bump a reference candidate (atomic upsert by normalized topic — a repeat lookup bumps
    `hits`/`last_seen` so repeated interest is visible). Dedups against existing kb/Reference up front.
    Takes ONLY the public topic + source — never owner data."""
    norm = _norm(topic)
    if not norm or not (url or "").strip():
        return
    if has_reference_article(conn, topic, norm):
        return
    conn.execute(
        "INSERT INTO reference_candidates (topic, norm_key, source, url, snippet, category) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(norm_key) DO UPDATE SET hits=hits+1, last_seen=datetime('now'), "
        "  url=excluded.url, snippet=excluded.snippet, "
        "  category=COALESCE(reference_candidates.category, excluded.category), "
        "  status=CASE WHEN status='dismissed' THEN 'dismissed' ELSE status END",
        (topic, norm, source, url, snippet or "", category))
    conn.commit()
