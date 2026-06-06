"""Client-fired app events → event-workflows.

A small, allow-listed way for the PWA to signal "this was viewed/opened" so
event-triggered workflows can react. It's the generic hook behind the wiki
link-label audit's on-view flag, and the place to tie future "when a wiki is
viewed" automations into. Debounced per event so rapid re-opens don't re-run
subscribed workflows.
"""
import time

from fastapi import APIRouter

from ..auth import CurrentUser
from ..db import get_conn, get_meta, set_meta
from ..services import workflows as wf_svc

router = APIRouter(prefix="/api/events", tags=["events"], dependencies=[CurrentUser])

# Events the PWA is allowed to fire (a UI signal, never a privileged action).
_CLIENT_EVENTS = {"wiki_viewed"}
_DEBOUNCE_SECONDS = 600  # at most once per 10 min per event


@router.post("/{event}")
def fire(event: str):
    """Fire an allow-listed client event, running its subscribed event-workflows.
    Debounced — returns {fired:false, debounced:true} if it ran too recently."""
    if event not in _CLIENT_EVENTS:
        return {"fired": False, "reason": "unknown event"}
    conn = get_conn()
    key = f"event_fired:{event}"
    now = time.time()
    last = get_meta(key, conn=conn)
    try:
        recent = last is not None and (now - float(last)) < _DEBOUNCE_SECONDS
    except ValueError:
        recent = False
    if recent:
        return {"fired": False, "debounced": True}
    set_meta(conn, key, str(now))
    wf_svc.fire_event(conn, event)   # runs subscribed event-workflows + commits
    return {"fired": True}
