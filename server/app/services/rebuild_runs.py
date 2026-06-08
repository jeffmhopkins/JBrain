"""In-memory registry of live "Rebuild page now" sessions.

A rebuild is deliberately NOT persisted (lean, session-only): each run lives here
keyed by an opaque run_id, holds the verbatim provider message blocks (so Guide can
continue the same conversation faithfully), the streamed draft, and a content hash of
the live page at start (the staleness guard for Accept). Closing the panel / refresh =
cancel; an idle run is reaped after a sliding TTL. Nothing here is ever written to the DB
or included in any share payload — the transcript is owner-only by construction.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

# Reap a run after this many seconds with no activity. Sliding: every get()/touch()
# resets it, so an actively-reviewed or actively-guided run never expires under you.
_TTL_SECONDS = 1800            # 30 minutes idle
_MAX_RUNS = 8                  # hard cap on concurrent in-memory runs (memory backstop)

# Statuses a run is allowed to be Accepted/Guided from.
_LIVE = ("streaming", "ready", "guiding")


@dataclass
class RebuildRun:
    """In-memory state for one active "Rebuild page now" session."""

    run_id: str
    slug: str
    title: str
    model: str | None                       # pinned at creation; Guide must reuse it
    base_hash: str                          # sha256 of the live page content at start
    kind: str = "rebuild"                   # "rebuild" (write from sources) | "suggest" (edit BASE)
    base_content: str = ""                  # verbatim live page body at start (suggest edits FROM it)
    instructions: str | None = None
    messages: list[dict] = field(default_factory=list)   # verbatim provider blocks (opaque)
    known: list[str] = field(default_factory=list)       # cross-link candidates (allowed set)
    candidates: list[dict] = field(default_factory=list)  # gathered sources: {note_id,title,date,reason,on,private,added}
    skipped: list[dict] = field(default_factory=list)     # considered-but-skipped: {note_id,title,date,reason}
    draft: str = ""                         # the streamed/guided article body (staged, never live)
    thoughts: str = ""                      # accumulated extended-thinking text (owner-only)
    sources: list[dict] = field(default_factory=list)    # [{title}] the rebuild loaded
    talk: list[dict] = field(default_factory=list)       # article-talk items to record on Accept
    status: str = "streaming"               # streaming|ready|guiding|accepting|accepted|rejected|error|cancelled
    error: str | None = None
    cancelled: bool = False                 # cooperative cancel flag (polled by the engine)
    rebound: bool = False                   # entity index freshened once for this session (draft-time linking)
    created_at: float = field(default_factory=time.monotonic)
    touched_at: float = field(default_factory=time.monotonic)


_RUNS: dict[str, RebuildRun] = {}
_BY_SLUG: dict[str, str] = {}               # slug -> run_id (one active run per page)


def content_hash(s: str | None) -> str:
    """Return the SHA-256 hex digest of a string (or "" for None).

    Args:
        s: String to hash, or None.

    Returns:
        Lowercase hex digest string.
    """
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def _sweep() -> None:
    """Reap idle runs that have exceeded _TTL_SECONDS, skipping those in 'accepting' state."""
    now = time.monotonic()
    for rid in [rid for rid, r in _RUNS.items()
                if r.status != "accepting" and now - r.touched_at > _TTL_SECONDS]:
        drop(rid)


def create(slug: str, title: str, model: str | None, base_content: str,
           kind: str = "rebuild") -> RebuildRun:
    """Start a fresh rebuild run for a page, cancelling any prior live run for the same slug.

    Enforces one active rebuild per page. Reaps idle and over-cap runs first.

    Args:
        slug: Page slug (used as the unique key for one-active-run-per-page).
        title: KB article title.
        model: LLM model identifier pinned for the duration of this run.
        base_content: Live page content at the time of creation (hashed for the staleness
            guard, and kept verbatim so a "suggest" session can edit FROM it).
        kind: "rebuild" (write the article from sources) or "suggest" (edit the current
            article at the owner's direction).

    Returns:
        Newly created RebuildRun.
    """
    _sweep()
    old = _BY_SLUG.get(slug)
    if old:
        drop(old)
    while len(_RUNS) >= _MAX_RUNS:
        oldest = min(_RUNS.values(), key=lambda r: r.touched_at, default=None)
        if oldest is None:
            break
        drop(oldest.run_id)
    run = RebuildRun(run_id=secrets.token_urlsafe(12), slug=slug, title=title,
                     model=model, base_hash=content_hash(base_content),
                     kind=kind, base_content=base_content or "")
    _RUNS[run.run_id] = run
    _BY_SLUG[slug] = run.run_id
    return run


def get(run_id: str) -> RebuildRun | None:
    """Look up a run by ID, updating its TTL on each access.

    Args:
        run_id: Opaque run identifier returned by create().

    Returns:
        The RebuildRun if it exists and has not been reaped, else None.
    """
    _sweep()
    run = _RUNS.get(run_id)
    if run is not None:
        run.touched_at = time.monotonic()
    return run


def touch(run: RebuildRun) -> None:
    """Reset a run's idle TTL without a full registry lookup.

    Args:
        run: The RebuildRun whose TTL should be extended.
    """
    run.touched_at = time.monotonic()


def drop(run_id: str) -> None:
    """Remove a run and signal its generator to stop via the cancelled flag. Idempotent.

    Args:
        run_id: Opaque run identifier to remove.
    """
    run = _RUNS.pop(run_id, None)
    if run is None:
        return
    run.cancelled = True
    if _BY_SLUG.get(run.slug) == run_id:
        _BY_SLUG.pop(run.slug, None)


def is_live(run: RebuildRun) -> bool:
    """Return True if the run is in a state that accepts Guide or Accept actions.

    Args:
        run: The RebuildRun to check.

    Returns:
        True if the run's status is streaming, ready, or guiding.
    """
    return run.status in _LIVE
