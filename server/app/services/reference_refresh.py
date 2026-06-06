"""Re-validate STAGED kb/Reference seeds when their cited source goes stale (nightly maintenance).

Every reference seed (built by reference_promote) carries a machine-readable provenance marker
(``<!-- refseed src=… url=… fetched=… -->``). When a seed's fetched date is older than a TTL, this
re-fetches the public source and STAGES an UPDATE that re-seats ONLY the cited Source line + that
marker (today's date, and the current URL if the page moved) — the rest of the article, including
anything the owner enriched, is preserved untouched.

SAFETY (load-bearing, same posture as promote):
- NO LLM and NOTHING owner-specific is an input — the only change is the public citation
  (URL + fetch date). The owner's prose is never rewritten.
- STAGED, never written live: it routes through staging_actions like every other proposed write, so
  the owner approves it in the normal tray (with optimistic-concurrency `_basis`, so a seed the owner
  edited in the meantime can't be silently clobbered).
- A source that can't be re-fetched is left as-is and retried next run (a dead link surfaces as an
  unchanged-but-stale article, never a mangled one). Pending refreshes aren't double-staged.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date

from . import reference_promote as rp

# The whole cited-source block: the visible "<!-- refsrc -->" line through its "<!-- refseed … -->"
# provenance marker (adjacent lines in a seed) — replaced as a unit so the citation stays well-formed.
_BLOCK_RE = re.compile(r"(?ms)^<!-- refsrc -->.*?<!--\s*refseed\s+.*?-->[ \t]*$")
_SEED_RE = re.compile(r"<!--\s*refseed\s+(.*?)-->")
_FIELD_RE = re.compile(r'(\w+)="([^"]*)"')


def _today() -> str:
    return date.today().isoformat()


def _provenance(body: str) -> dict | None:
    """Parse the `<!-- refseed … -->` marker → {src, url, fetched} (or None if absent/incomplete)."""
    m = _SEED_RE.search(body or "")
    if not m:
        return None
    fields = dict(_FIELD_RE.findall(m.group(1)))
    if not fields.get("url") or not fields.get("fetched"):
        return None
    return fields


def _days_since(iso: str, today: str) -> int:
    try:
        return (date.fromisoformat(today[:10]) - date.fromisoformat(iso[:10])).days
    except Exception:  # noqa: BLE001 — an unparseable date is treated as not-stale (skip, don't crash)
        return 0


def _reseat(body: str, source: str, url: str, fetched: str, leaf: str) -> str:
    """Replace the marked source block with a freshly-cited one; everything else is preserved."""
    block = rp._source_block(source, url, fetched, leaf).rstrip("\n")
    return _BLOCK_RE.sub(lambda _m: block, body, count=1)


def run(conn, ttl_days: int = 180, limit: int = 5) -> dict:
    """Stage citation refreshes for up to `limit` kb/Reference seeds whose source is older than
    `ttl_days`. Posts one review card if anything was staged. Returns {refreshed, titles}."""
    from . import medref, reviews, clock
    today = (clock.today_iso() if hasattr(clock, "today_iso") else None) or _today()
    ttl = max(1, int(ttl_days))
    # Titles already pending in staging — don't double-stage the same refresh until it's decided.
    pending: set[str] = set()
    for r in conn.execute("SELECT payload_json FROM staging_actions WHERE status='pending' AND type='UPDATE'"):
        try:
            pending.add(json.loads(r["payload_json"]).get("title"))
        except Exception:  # noqa: BLE001
            pass
    rows = conn.execute(
        "SELECT id, title, content_md FROM notes WHERE kind='kb' AND deleted_at IS NULL "
        "AND title LIKE 'kb/Reference/%' AND content_md LIKE '%refseed%'").fetchall()
    refreshed: list[str] = []
    for note in rows:
        if len(refreshed) >= max(1, int(limit)):
            break
        prov = _provenance(note["content_md"])
        if not prov or _days_since(prov["fetched"], today) < ttl:
            continue                                       # no marker, or still fresh
        if note["title"] in pending:
            continue                                       # already awaiting the owner
        leaf = (note["title"].split("/")[-1] or "").strip()
        src = prov.get("src") or "medlineplus"
        # Re-fetch the public source (cached) — a drug via RxNorm→MedlinePlus Connect, else health-topics.
        fresh = (medref.drug_topic(conn, leaf) if src == "rxnorm" else medref.health_topic(conn, leaf)) or {}
        new_url = fresh.get("url")
        if not new_url:
            continue                                       # source unreachable — leave as-is, retry next run
        new_body = _reseat(note["content_md"], src, new_url, today, leaf)
        if new_body == note["content_md"]:
            continue                                       # block didn't change (shouldn't happen: date moves)
        chash = hashlib.sha256((note["content_md"] or "").encode("utf-8")).hexdigest()
        payload = {"type": "UPDATE", "title": note["title"], "content": new_body, "kind": "kb",
                   "_basis": {"note_id": note["id"], "content_hash": chash}}
        conn.execute("INSERT INTO staging_actions (conversation_id, type, payload_json) VALUES (NULL, 'UPDATE', ?)",
                     (json.dumps(payload),))
        refreshed.append(note["title"])
    if refreshed:
        reviews.create_review_item(
            conn, None,
            title=f"{len(refreshed)} reference source(s) re-verified",
            message=(f"Re-checked {len(refreshed)} kb/Reference seed(s) whose cited source was over {ttl_days} "
                     "days old and staged a citation refresh (updated link + fetch date only — your own "
                     "additions are untouched). Approve them in the staging tray."),
            link_slug="kb/_index")
    conn.commit()
    return {"refreshed": len(refreshed), "titles": refreshed}
