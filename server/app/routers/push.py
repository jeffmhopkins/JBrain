"""Web Push subscription API (owner-only)."""
from fastapi import APIRouter
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import push

router = APIRouter(prefix="/api/push", tags=["push"], dependencies=[CurrentUser])


class SubKeys(BaseModel):
    """VAPID subscription key material (p256dh and auth)."""

    p256dh: str
    auth: str


class SubIn(BaseModel):
    """Input body for registering a Web Push subscription."""

    endpoint: str
    keys: SubKeys
    ua: str | None = None


@router.post("/subscribe")
def subscribe(body: SubIn):
    """Register or update a Web Push subscription for this device.

    Args:
        body: Endpoint URL, VAPID keys, and optional user-agent string.

    Returns:
        Dict with key 'ok' set to True.
    """
    push.upsert_subscription(get_conn(), body.endpoint, body.keys.p256dh, body.keys.auth, body.ua)
    return {"ok": True}


class UnsubIn(BaseModel):
    """Input body for removing a Web Push subscription."""

    endpoint: str


@router.post("/unsubscribe")
def unsubscribe(body: UnsubIn):
    """Remove a Web Push subscription for this device.

    Args:
        body: Endpoint URL of the subscription to remove.

    Returns:
        Dict with key 'ok' set to True.
    """
    push.delete_subscription(get_conn(), body.endpoint)
    return {"ok": True}


class TestIn(BaseModel):
    """Input body for triggering a test push notification with an optional delay."""

    delay: int = 0   # seconds before firing (server-side; survives app close)


@router.post("/test")
def test(body: TestIn | None = None):
    """Send a test push notification to all subscribed devices, optionally after a delay.

    The delay is server-side and survives the app being closed.

    Args:
        body: Optional delay in seconds before the notification fires (default 0).

    Returns:
        Result dict from push.send_test.
    """
    return push.send_test(get_conn(), body.delay if body else 0)
