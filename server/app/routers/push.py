"""Web Push subscription API (owner-only)."""
from fastapi import APIRouter
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import push

router = APIRouter(prefix="/api/push", tags=["push"], dependencies=[CurrentUser])


class SubKeys(BaseModel):
    p256dh: str
    auth: str


class SubIn(BaseModel):
    endpoint: str
    keys: SubKeys
    ua: str | None = None


@router.post("/subscribe")
def subscribe(body: SubIn):
    push.upsert_subscription(get_conn(), body.endpoint, body.keys.p256dh, body.keys.auth, body.ua)
    return {"ok": True}


class UnsubIn(BaseModel):
    endpoint: str


@router.post("/unsubscribe")
def unsubscribe(body: UnsubIn):
    push.delete_subscription(get_conn(), body.endpoint)
    return {"ok": True}
