"""Pure-logic tests for wiki-link parsing (no DB / model dependencies)."""
from app.services.wikilinks import extract_links


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
