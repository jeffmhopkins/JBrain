"""Pure-logic tests for wiki-link parsing (no DB / model dependencies)."""
from app.services.wikilinks import (
    _iter_display_links,
    _path_forms,
    extract_links,
    wiki_label,
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


def test_wiki_label_drops_kb_category_folders():
    # kb/<Category>/<Name> → the bare article name (category folders are taxonomy).
    assert wiki_label("kb/People/Jeffrey Mark Hopkins") == "Jeffrey Mark Hopkins"
    assert wiki_label("kb/Reference/Medicine/Conditions/Asthma") == "Asthma"
    assert wiki_label("kb/Discipline") == "Discipline"
    # Non-kb roots keep their root-stripped leaf (unchanged behaviour).
    assert wiki_label("notes/2026/06/04/03 - Run") == "2026/06/04/03 - Run"


def test_path_forms_are_progressive_suffixes():
    assert _path_forms("kb/People/Jeff Hopkins") == {
        "jeff hopkins", "people/jeff hopkins", "kb/people/jeff hopkins",
    }
