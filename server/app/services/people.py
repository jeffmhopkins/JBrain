"""People registry helpers. People attribute/colour location trails (and can be
linked to a KB page); they are NOT auth accounts. A location fix's `source` is
matched to a person by name or alias, falling back to the default ("Me")."""


def _aliases(row) -> set[str]:
    return {a.strip().lower() for a in (row["aliases"] or "").split(",") if a.strip()}


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
