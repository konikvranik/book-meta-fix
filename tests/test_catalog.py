"""Tests for the diagnosis-code help catalog (the GUI's clickable codes)."""
from __future__ import annotations

from book_meta_fix.catalog import CATEGORY_HELP, category_help

# Every category the detectors / normalize / duplicates / filecheck passes can
# put into a review entry — the popup must never open empty for one of these.
EXPECTED_CODES = {
	"C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10",
	"C11", "C12", "C13", "C14", "C15", "C16", "C17", "C18", "C19", "C20",
	"EMPTY_BOOK", "MISSING_ISBN", "MISSING_YEAR", "MISSING_COVER",
	"CONTENT_MISMATCH", "ERROR", "OK",
}


def test_every_known_code_has_help():
	assert EXPECTED_CODES <= set(CATEGORY_HELP)


def test_help_entries_are_non_empty():
	for code, (title, detail) in CATEGORY_HELP.items():
		assert title and detail, f"empty help entry for {code}"


def test_category_help_localizes_at_lookup():
	"""The dict values resolve through _() — under a cs locale the C1 title
	comes back Czech (the catalog module was imported with the language
	active), under en it stays English. Assert the shape, not the language:
	the suite pins BMF_LANGUAGE=en, but a cs developer machine still runs it."""
	pair = category_help("C1")
	assert pair is not None
	title, detail = pair
	assert title and len(detail) > 50


def test_unknown_code_returns_none():
	assert category_help("NOPE") is None
	assert category_help("") is None
