"""Unit tests for the deterministic date-token helpers and the structure-lint surfacing.

Covers clock.tokens_in / malformed_tokens / dropped_tokens (pure) and the
validate_structure warning that flags token-shaped near-misses on every writer path.
"""
import pytest

from app.services import clock, wiki_guides

pytestmark = pytest.mark.unit


def test_tokens_in_collects_only_valid_tokens():
    text = "Born @t[age:1986-03-15], married @t[since:2010-06-01], plus @t[born:1986] (bad)."
    assert clock.tokens_in(text) == {"@t[age:1986-03-15]", "@t[since:2010-06-01]"}


def test_tokens_in_excludes_unparseable_date():
    # Right shape, wrong date → not a valid token.
    assert clock.tokens_in("@t[age:nineteen]") == set()


def test_malformed_tokens_flags_near_misses_not_valid_ones():
    text = (
        "Good: @t[age:1986-03-15] and @t[until:2030-01-01T09:00]. "
        "Bad: @t{age:1986-03-15}, @t(since:2010-06-01), @t[born:1986], @t[age:nineteen]."
    )
    bad = clock.malformed_tokens(text)
    assert "@t{age:1986-03-15}" in bad
    assert "@t(since:2010-06-01)" in bad
    assert "@t[born:1986]" in bad
    assert "@t[age:nineteen]" in bad
    # The two well-formed tokens are never reported.
    assert not any(b.startswith("@t[age:1986-03-15") for b in bad)
    assert not any("until" in b for b in bad)


def test_malformed_tokens_dedupes_in_first_seen_order():
    text = "@t{age:1} then @t[born:x] then @t{age:1} again"
    assert clock.malformed_tokens(text) == ["@t{age:1}", "@t[born:x]"]


def test_malformed_tokens_ignores_ordinary_prose():
    assert clock.malformed_tokens("Email @team and meet at 9. Cost @ $5.") == []


def test_dropped_tokens_reports_a_frozen_value():
    base = "Jeff is @t[age:1986-03-15] and married @t[since:2010-06-01]."
    draft = "Jeff is 40 and married @t[since:2010-06-01]."   # age token frozen to a literal
    assert clock.dropped_tokens(base, draft) == ["@t[age:1986-03-15]"]


def test_dropped_tokens_empty_when_all_preserved():
    base = draft = "Age @t[age:1986-03-15], since @t[since:2010-06-01]."
    assert clock.dropped_tokens(base, draft) == []


def test_validate_structure_warns_on_malformed_token():
    body = "# Jeff\n\nJeff is @t{age:1986-03-15} years old and lives in town.\n"
    res = wiki_guides.validate_structure("kb/People/Jeff", body)
    assert res["ok"] is True                       # a warning never blocks
    assert any("not a valid live time token" in w for w in res["warnings"])


def test_validate_structure_clean_on_valid_token():
    body = "# Jeff\n\nJeff is @t[age:1986-03-15] years old and lives in town.\n"
    res = wiki_guides.validate_structure("kb/People/Jeff", body)
    assert not any("not a valid live time token" in w for w in res["warnings"])
