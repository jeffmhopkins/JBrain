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
    """Normalize a topic string for deduplication.

    Args:
        topic: Raw topic string.

    Returns:
        Normalized string; falls back to lowercased whitespace-collapsed form on error.
    """
    from . import entity_index
    try:
        return entity_index.normalize(topic) or ""
    except Exception:  # noqa: BLE001
        return " ".join((topic or "").lower().split())


def has_reference_article(conn, topic: str, norm: str | None = None) -> bool:
    """Return True if the owner already has a kb/Reference article for this topic.

    Checks all kb/Reference/ notes by normalizing each leaf title. When True, the
    capture loop has already closed and the candidate should not be re-recorded.

    Args:
        conn: SQLite connection.
        topic: Topic string to look for.
        norm: Pre-computed normalized key; computed from topic if not supplied.

    Returns:
        True if a matching kb/Reference article exists.
    """
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
    """Enqueue or bump a reference candidate by normalized topic (atomic upsert).

    A repeat lookup increments ``hits`` and updates ``last_seen`` so repeated interest
    is visible to the promote pass. Deduplicates against existing kb/Reference articles
    up front. Records ONLY the public topic and source URL — never owner data.

    Args:
        conn: SQLite connection (commits internally).
        topic: Public topic name (e.g. 'Aspirin').
        source: Provenance source key ('medlineplus' or 'rxnorm').
        url: Public source URL for the topic.
        snippet: Short public-domain summary text (may be empty).
        category: Optional category hint (e.g. 'Medications').
    """
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
