"""Unit tests for the static nickname lexicon (no DB / no app fixtures needed)."""
from app.services import nickname_lexicon as nl
import pytest

pytestmark = pytest.mark.unit


def test_canon_token_is_bidirectional():
    """Every token in a group canonicalizes to the same representative — both the
    diminutive and the formal form."""
    assert nl.canon_token("jeff") == nl.canon_token("jeffrey") == nl.canon_token("geoff")
    assert nl.canon_token("bob") == nl.canon_token("rob") == nl.canon_token("robert")
    assert nl.canon_token("liz") == nl.canon_token("beth") == nl.canon_token("elizabeth")
    # The canonical is the formal/longest form.
    assert nl.canon_token("bob") == "robert"
    assert nl.canon_token("jeff") == "jeffrey"


def test_canon_token_passthrough_for_unknown():
    """A token in no group is returned unchanged (case-normalized lookup, original kept)."""
    assert nl.canon_token("hopkins") == "hopkins"
    assert nl.canon_token("smith") == "smith"
    assert nl.canon_token("") == ""


def test_canon_token_case_insensitive():
    assert nl.canon_token("BOB") == nl.canon_token("bob")


def test_map_tokens_maps_each_and_returns_frozenset():
    out = nl.map_tokens(["jeff", "hopkins"])
    assert out == frozenset({"jeffrey", "hopkins"})
    assert isinstance(out, frozenset)
    # Nickname subset relationship surfaces after mapping.
    a = nl.map_tokens(["jeff", "hopkins"])
    b = nl.map_tokens(["jeffrey", "mark", "hopkins"])
    assert a <= b


def test_suffixes_detects_generational_markers():
    assert nl.suffixes(["jeff", "hopkins", "jr"]) == {"jr"}
    assert nl.suffixes(["jeffrey", "hopkins", "sr"]) == {"sr"}
    assert nl.suffixes(["jeff", "hopkins"]) == set()
    assert nl.suffixes(["robert", "iii"]) == {"iii"}
    # Case-insensitive.
    assert nl.suffixes(["JR"]) == {"jr"}


def test_suffixes_constant_present():
    assert {"jr", "sr", "ii", "iii", "iv", "2nd", "3rd"} <= nl.SUFFIXES


def test_no_token_belongs_to_two_groups():
    """_build_canon uses first-group-wins setdefault, so a token in two groups would be
    silently mis-routed. Guard: every token (formal + variants) is unique across groups."""
    seen: dict[str, str] = {}
    dups = []
    for formal, variants in nl._GROUPS:
        for tok in (formal, *variants):
            if tok in seen and seen[tok] != formal:
                dups.append((tok, seen[tok], formal))
            seen[tok] = formal
    assert not dups, f"tokens in multiple groups: {dups}"


def test_ambiguous_short_forms_are_not_mapped():
    """Cross-gender / cross-name short forms must NOT be lexicon entries, or they'd
    auto-merge different real people who share a surname. Pin the ones we deliberately
    excluded so a future edit can't silently re-introduce them."""
    for tok in ("sandy", "jack", "harry", "al", "kit", "jamie", "sally", "alfred", "gerard"):
        assert nl.canon_token(tok) == tok, f"{tok!r} should pass through unmapped"
