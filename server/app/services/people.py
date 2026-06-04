"""People registry helpers. People attribute/colour location trails (and can be
linked to a KB page); they are NOT auth accounts. A location fix's `source` is
matched to a person by name or alias, falling back to the default ("Me")."""


def _aliases(row) -> set[str]:
    return {a.strip().lower() for a in (row["aliases"] or "").split(",") if a.strip()}


def by_name(conn, name: str):
    """Resolve an EXPLICIT person name or alias to a row (case-insensitive), with NO
    default fallback — returns None if there's no such person. Used when the user names
    someone ('where is Allan'); resolve() is for attributing an inbound fix's source."""
    n = (name or "").strip().lower()
    if not n:
        return None
    for p in conn.execute("SELECT * FROM people ORDER BY id").fetchall():
        if n == p["name"].lower() or n in _aliases(p):
            return p
    return None


def resolve(conn, source: str):
    """Map a fix's `source` to a person row (by name or alias, case-insensitive),
    else the default person. Returns None only if the registry is somehow empty."""
    people = conn.execute("SELECT * FROM people ORDER BY id").fetchall()
    if not people:
        return None
    src = (source or "").strip().lower()
    if src:
        for p in people:
            if src == p["name"].lower() or src in _aliases(p):
                return p
    return next((p for p in people if p["is_default"]), people[0])


def owner(conn):
    """The default person — the note-taker. Every note is authored by them, so a note's
    first-person voice ('I', 'my truck') refers to this person."""
    people = conn.execute("SELECT * FROM people ORDER BY id").fetchall()
    if not people:
        return None
    return next((p for p in people if p["is_default"]), people[0])


def owner_name(conn) -> str:
    """The owner's display name (for prompts), or 'the owner' if unset/placeholder."""
    o = owner(conn)
    name = (o["name"] if o else "").strip()
    return name if name and name.lower() != "me" else "the owner"

