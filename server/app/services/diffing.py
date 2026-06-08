"""Line-level markdown diffing using stdlib difflib (no extra dependency)."""
from __future__ import annotations

import difflib

# Guard against pathological inputs (very large notes); skip diffing instead.
MAX_DIFF_CHARS = 500_000


def line_diff(a: str, b: str) -> list[dict]:
    """Return a line-level diff of two Markdown strings as a list of typed hunks.

    Falls back to a single 'equal' hunk with a placeholder message if either input
    exceeds MAX_DIFF_CHARS.

    Args:
        a: Original Markdown string.
        b: Modified Markdown string.

    Returns:
        List of hunk dicts, each with 'type' (equal/delete/insert) and 'lines' keys.
    """
    if len(a) > MAX_DIFF_CHARS or len(b) > MAX_DIFF_CHARS:
        return [{"type": "equal", "lines": ["(content too large to diff inline)"]}]

    al, bl = a.splitlines(), b.splitlines()
    sm = difflib.SequenceMatcher(None, al, bl, autojunk=False)
    hunks: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            hunks.append({"type": "equal", "lines": al[i1:i2]})
        elif tag == "delete":
            hunks.append({"type": "delete", "lines": al[i1:i2]})
        elif tag == "insert":
            hunks.append({"type": "insert", "lines": bl[j1:j2]})
        else:  # replace -> show as delete then insert
            hunks.append({"type": "delete", "lines": al[i1:i2]})
            hunks.append({"type": "insert", "lines": bl[j1:j2]})
    return hunks
