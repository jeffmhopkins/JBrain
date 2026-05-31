"""The Chief Knowledge Architect: a Socratic Claude agent that grounds itself in
your existing notes and proposes (never silently applies) wiki changes.

Exposes an async generator that streams SSE-friendly event dicts to the chat
router. Tools are executed server-side against SQLite.
"""
from __future__ import annotations

import json
from typing import AsyncGenerator

from anthropic import AsyncAnthropic

from ..config import get_settings
from ..db import get_conn
from . import embeddings
from . import notes as notes_svc

MAX_TOOL_ITERATIONS = 6


def _system_prompt(brain_name: str) -> str:
    return f"""You are the Conversational Facade and Chief Knowledge Architect for \
"{brain_name}", a personal wiki stored in a SQL database.

Your goal: extract knowledge from the user through Socratic dialogue, then \
organise it into well-linked wiki notes — so they never have to write a note \
themselves.

Operating rules:
1. TONALITY: Curious, collaborative, Socratic. Ask targeted questions that \
clarify and deepen one concept at a time before moving on.
2. GROUNDING: Use `search_notes`, `read_note`, and `list_recent_notes` to check \
what already exists. At the start of a session, look at recent notes (and any \
"Master Index" note) and greet the user to pick up where they left off. Prefer \
UPDATING an existing note over creating a near-duplicate. Check `read_inbox` for \
quick captures the user dictated earlier.
3. STAGING AREA (CRITICAL): You cannot write to the wiki directly. When a topic \
is ready, call `propose_actions` to stage CREATE/UPDATE/LINK proposals, then \
clearly summarise what you proposed and ask the user to confirm in the staging \
area. Never say "I've saved this" — say you've *proposed* it pending their \
confirmation.
4. LINKING: Use [[Note Title]] wiki-links inside note content to connect ideas."""


TOOLS = [
    {
        "name": "search_notes",
        "description": "Search existing notes by keyword and meaning. Use to avoid duplicates and find related notes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_note",
        "description": "Read the full markdown content of a note by its exact title.",
        "input_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    },
    {
        "name": "list_recent_notes",
        "description": "List the most recently updated notes to orient at the start of a session.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
        },
    },
    {
        "name": "read_inbox",
        "description": "Read unprocessed quick-capture inbox items (e.g. dictations) to fold into the wiki.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "propose_actions",
        "description": "Stage wiki changes for the user to confirm. Does NOT apply them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["CREATE", "UPDATE", "LINK"]},
                            "title": {"type": "string", "description": "Note title (CREATE/UPDATE)"},
                            "content": {"type": "string", "description": "Full markdown content (CREATE/UPDATE)"},
                            "source_title": {"type": "string", "description": "LINK: note that links out"},
                            "target_title": {"type": "string", "description": "LINK: note being linked to"},
                            "summary": {"type": "string", "description": "Short human-readable description"},
                        },
                        "required": ["type", "summary"],
                    },
                }
            },
            "required": ["actions"],
        },
    },
]


# --- Tool implementations ---------------------------------------------------

def _tool_search_notes(conn, query: str, limit: int = 8) -> str:
    rows = embeddings.semantic_search(conn, query, limit)
    if not rows:
        return "No matching notes."
    return "\n".join(f"- {r['title']}" for r in rows)


def _tool_read_note(conn, title: str) -> str:
    row = notes_svc.get_by_title(conn, title)
    if not row:
        return f"No note titled '{title}'."
    return f"# {row['title']}\n\n{row['content_md']}"


def _tool_list_recent(conn, limit: int = 10) -> str:
    rows = conn.execute(
        "SELECT title FROM notes WHERE deleted_at IS NULL ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    if not rows:
        return "The wiki is empty — this is a fresh brain."
    return "Recent notes:\n" + "\n".join(f"- {r['title']}" for r in rows)


def _tool_read_inbox(conn) -> str:
    rows = conn.execute(
        "SELECT id, content FROM inbox WHERE processed = 0 ORDER BY created_at LIMIT 50"
    ).fetchall()
    if not rows:
        return "Inbox is empty."
    return "Unprocessed captures:\n" + "\n".join(f"- (#{r['id']}) {r['content']}" for r in rows)


def _tool_propose_actions(conn, conversation_id: int | None, actions: list[dict]) -> tuple[str, list[dict]]:
    staged = []
    for a in actions:
        conn.execute(
            "INSERT INTO staging_actions (conversation_id, type, payload_json) VALUES (?, ?, ?)",
            (conversation_id, a["type"], json.dumps(a)),
        )
        staged.append(a)
    conn.commit()
    return f"Staged {len(staged)} proposed action(s) for the user to confirm.", staged


def _run_tool(conn, conversation_id, name: str, args: dict):
    """Returns (result_text, staged_actions_or_None)."""
    if name == "search_notes":
        return _tool_search_notes(conn, args["query"], args.get("limit", 8)), None
    if name == "read_note":
        return _tool_read_note(conn, args["title"]), None
    if name == "list_recent_notes":
        return _tool_list_recent(conn, args.get("limit", 10)), None
    if name == "read_inbox":
        return _tool_read_inbox(conn), None
    if name == "propose_actions":
        return _tool_propose_actions(conn, conversation_id, args["actions"])
    return f"Unknown tool: {name}", None


# --- Agent loop -------------------------------------------------------------

async def run(conversation_id: int, user_text: str) -> AsyncGenerator[dict, None]:
    """Stream the architect's reply. Yields event dicts: {type, ...}."""
    settings = get_settings()
    if not settings.has_anthropic:
        yield {"type": "error", "message": "No Anthropic API key configured."}
        return

    conn = get_conn()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Build message history from the DB, then append the new user turn.
    history = conn.execute(
        "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id",
        (conversation_id,),
    ).fetchall()
    messages = [{"role": r["role"], "content": r["content"]} for r in history]
    messages.append({"role": "user", "content": user_text})
    conn.execute(
        "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'user', ?)",
        (conversation_id, user_text),
    )
    conn.commit()

    system = _system_prompt(get_settings().brain_name)
    assistant_text_parts: list[str] = []

    for _ in range(MAX_TOOL_ITERATIONS):
        async with client.messages.stream(
            model=settings.anthropic_model,
            max_tokens=2048,
            system=system,
            tools=TOOLS,
            messages=messages,
        ) as stream:
            async for event in stream:
                if (
                    event.type == "content_block_delta"
                    and getattr(event.delta, "type", None) == "text_delta"
                ):
                    assistant_text_parts.append(event.delta.text)
                    yield {"type": "token", "text": event.delta.text}
            final = await stream.get_final_message()

        # Append the assistant turn (text + any tool_use blocks) to the context.
        messages.append({"role": "assistant", "content": final.content})

        tool_uses = [b for b in final.content if b.type == "tool_use"]
        if not tool_uses:
            break

        tool_results = []
        for tu in tool_uses:
            result_text, staged = _run_tool(conn, conversation_id, tu.name, tu.input)
            if staged is not None:
                yield {"type": "staging", "actions": staged}
            tool_results.append(
                {"type": "tool_result", "tool_use_id": tu.id, "content": result_text}
            )
        messages.append({"role": "user", "content": tool_results})

    final_text = "".join(assistant_text_parts).strip()
    if final_text:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'assistant', ?)",
            (conversation_id, final_text),
        )
        conn.commit()
    yield {"type": "done"}
