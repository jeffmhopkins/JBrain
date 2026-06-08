"""Interactive staged-write hygiene: a [[link]] to a non-existent note must be neutralized before
it's saved (matching the nightly KB pipeline), a LINK to a missing target is refused, and the user
is warned about both — closing the gap where user-driven article edits introduced dead links."""
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("sqlite_vec")

from app.services import staged_verify

pytestmark = pytest.mark.integration

SCHEMA = Path(__file__).resolve().parents[1] / "app" / "schema.sql"


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA.read_text())
    c.execute("INSERT INTO notes (slug, title, content_md) VALUES ('real', 'notes/Real', 'x')")
    c.execute("INSERT INTO notes (slug, title, content_md, kind) VALUES ('kb1', 'kb/Topic', 'x', 'kb')")
    c.commit()
    return c


def test_verify_content_strips_dead_links_keeps_real(conn):
    clean, dropped = staged_verify.verify_content(conn, "See [[notes/Real]] and [[notes/Ghost]].")
    assert dropped == ["notes/Ghost"]
    assert "[[notes/Real]]" in clean                # real link kept
    assert "[[notes/Ghost]]" not in clean and "Ghost" in clean   # dead link unwrapped to text


def test_verify_content_noop_when_all_real(conn):
    clean, dropped = staged_verify.verify_content(conn, "See [[notes/Real]] and [[kb/Topic]].")
    assert dropped == [] and clean == "See [[notes/Real]] and [[kb/Topic]]."


def test_warnings_flag_dead_link_in_create(conn):
    w = staged_verify.warnings(conn, "CREATE", {"title": "notes/New", "content": "x [[notes/Ghost]]"})
    assert any("don't exist" in x for x in w)


def test_warnings_flag_missing_link_target(conn):
    w = staged_verify.warnings(conn, "LINK", {"source_title": "notes/Real", "target_title": "notes/Ghost"})
    assert w and "Ghost" in w[0] and "refused" in w[0]


def test_warnings_flag_kb_citation_integrity(conn):
    # a kb/ article whose footnote marker has no definition → citation issue surfaced
    w = staged_verify.warnings(conn, "CREATE", {"title": "kb/Topic2", "kind": "kb",
                                                "content": "A fact[^s1] with no reference block."})
    assert any("footnote" in x for x in w)


def test_warnings_quiet_for_quick_note(conn):
    # a clean quick notes/ capture (real link, no kb) → no warnings, stays light
    assert staged_verify.warnings(conn, "CREATE", {"title": "notes/Jot", "content": "saw [[notes/Real]]"}) == []
