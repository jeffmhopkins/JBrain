"""People registry helpers. People attribute/colour location trails (and can be
linked to a KB page); they are NOT auth accounts. A location fix's `source` is
matched to a person by name or alias, falling back to the default ("Me")."""


def _aliases(row) -> set[str]:
    """Parse a person row's aliases field into a set of lowercased, stripped strings.

    Args:
        row: A people table row with an 'aliases' column.

    Returns:
        Set of lowercased alias strings.
    """
    return {a.strip().lower() for a in (row["aliases"] or "").split(",") if a.strip()}


def by_name(conn, name: str):
    """Resolve an explicit person name or alias to a row with no default fallback.

    Case-insensitive. Used when the user names someone ('where is Allan'); use
    resolve() for attributing an inbound fix's source.

    Args:
        conn: Database connection.
        name: Name or alias string to look up.

    Returns:
        The people row, or None if no match is found.
    """
    n = (name or "").strip().lower()
    if not n:
        return None
    for p in conn.execute("SELECT * FROM people ORDER BY id").fetchall():
        if n == p["name"].lower() or n in _aliases(p):
            return p
    return None


def resolve(conn, source: str):
    """Map a fix's source field to a person row, falling back to the default person.

    Case-insensitive match on name or alias. Returns None only if the people registry
    is empty.

    Args:
        conn: Database connection.
        source: Source string from an inbound location fix.

    Returns:
        The matched people row, the default person row, or None if the registry is empty.
    """
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
    """Return the default person — the note-taker who authors every note.

    First-person voice ('I', 'my truck') in notes refers to this person.

    Args:
        conn: Database connection.

    Returns:
        The default people row, or the first row if none is flagged default, or None if empty.
    """
    people = conn.execute("SELECT * FROM people ORDER BY id").fetchall()
    if not people:
        return None
    return next((p for p in people if p["is_default"]), people[0])


def owner_name(conn) -> str:
    """Return the owner's display name for use in prompts.

    Falls back to 'the owner' when the name is unset or is the placeholder 'Me'.

    Args:
        conn: Database connection.

    Returns:
        Owner display name string.
    """
    o = owner(conn)
    name = (o["name"] if o else "").strip()
    return name if name and name.lower() != "me" else "the owner"

