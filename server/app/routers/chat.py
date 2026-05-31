"""Chat: conversations, message history, and the streaming architect endpoint."""
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import architect

router = APIRouter(prefix="/api/chat", tags=["chat"], dependencies=[CurrentUser])


class MessageIn(BaseModel):
    text: str


@router.post("/conversations")
def create_conversation():
    conn = get_conn()
    cur = conn.execute("INSERT INTO conversations DEFAULT VALUES")
    conn.commit()
    return {"id": cur.lastrowid}


@router.get("/conversations")
def list_conversations():
    rows = get_conn().execute(
        "SELECT id, title, started_at FROM conversations ORDER BY started_at DESC LIMIT 100"
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/conversations/{conversation_id}/messages")
def get_messages(conversation_id: int):
    rows = get_conn().execute(
        "SELECT role, content, created_at FROM messages "
        "WHERE conversation_id = ? ORDER BY id",
        (conversation_id,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.post("/conversations/{conversation_id}/message")
def send_message(conversation_id: int, body: MessageIn):
    conn = get_conn()
    exists = conn.execute(
        "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    if not exists:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Title the conversation from its first user message.
    has_title = conn.execute(
        "SELECT title FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()["title"]
    if not has_title:
        conn.execute(
            "UPDATE conversations SET title = ? WHERE id = ?",
            (body.text[:60], conversation_id),
        )
        conn.commit()

    async def event_stream():
        try:
            async for event in architect.run(conversation_id, body.text):
                yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
        except Exception as exc:  # surface to the client rather than hanging
            yield f"event: error\ndata: {json.dumps({'message': str(exc)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
