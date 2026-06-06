"""Pure-logic tests for wiki-link parsing (no DB / model dependencies)."""
from app.services.wikilinks import (
    _desired_label,
    _iter_display_links,
    extract_links,
)


def test_extracts_simple_links():
    assert extract_links("See [[Alpha]] and [[Beta]].") == ["Alpha", "Beta"]


def test_extracts_aliased_links():
    # [[Title|display]] links resolve to the Title.
    assert extract_links("Read [[Discipline|my note]] now.") == ["Discipline"]


def test_dedupes_and_preserves_order():
    assert extract_links("[[A]] [[B]] [[A]]") == ["A", "B"]


def test_trims_whitespace_and_ignores_empty():
    assert extract_links("[[  Spaced Title  ]] and [[]]") == ["Spaced Title"]


def test_no_links():
    assert extract_links("plain text, no links") == []
    assert extract_links("") == []


def test_iter_display_links_only_yields_explicit_displays():
    md = "Plain [[Alpha]], aliased [[kb/People/Summer E. Hopkins|Jeff Hopkins]]."
    got = list(_iter_display_links(md))
    assert got == [("[[kb/People/Summer E. Hopkins|Jeff Hopkins]]",
                    "kb/People/Summer E. Hopkins", "Jeff Hopkins")]


def test_desired_label_keeps_bare_name_for_nested_titles():
    # Nested title → keep an explicit |bare-name so the label stays short.
    assert _desired_label("kb/People/Summer E. Hopkins", "kb/People/Summer E. Hopkins") \
        == "[[kb/People/Summer E. Hopkins|Summer E. Hopkins]]"


def test_desired_label_drops_alias_when_root_leaf_is_the_name():
    # Title has no folder depth past its root → dropping the alias already renders the name.
    assert _desired_label("kb/Discipline", "kb/Discipline") == "[[kb/Discipline]]"
