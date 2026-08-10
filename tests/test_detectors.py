"""Tests for the detector rules in detectors.py.

Focused on rule_c9_anonym, which had a major false-positive: books living in
a 'Neznamy/' folder but already carrying a real author in metadata.json were
flagged C9 (~800 books in the real library). The fix: a real author in
meta.authors means the book is NOT C9, regardless of the folder name.
"""
from __future__ import annotations

from book_meta_fix.detectors import detect, rule_c9_anonym
from book_meta_fix.models import BookMeta


def _meta(**kw) -> BookMeta:
	"""Build a BookMeta with sensible defaults for detector tests."""
	defaults = dict(
		calibre_id=1,
		path="Some Author/Some Book (1)",
		author_folder="Some Author",
		authors=["Some Author"],
		title="Some Book",
	)
	defaults.update(kw)
	return BookMeta(**defaults)


class TestC9Anonym:
	def test_real_author_in_neznamy_folder_is_not_c9(self):
		"""The bug: book lives in Neznamy/ but metadata already has a real
		author (calibre was fixed, folder was never moved). Must NOT fire C9."""
		m = _meta(
			path="Neznamy/- Heinlein Robertw_L_P_7 (1706)",
			author_folder="Neznamy",
			authors=["Robert A. Heinlein"],
			title="…také venčíme psy",
		)
		assert rule_c9_anonym(m) is None

	def test_multiple_neznamy_authors_with_one_real_is_not_c9(self):
		"""Even one real author in the list means the record is not anonym."""
		m = _meta(
			path="Neznamy/X (2)",
			author_folder="Neznamy",
			authors=["Neznámý", "Karel Čapek"],
			title="R.U.R.",
		)
		assert rule_c9_anonym(m) is None

	def test_neznamy_author_and_folder_fires_c9(self):
		"""Both the author and the folder are 'Neznamy' — genuine C9."""
		m = _meta(
			path="Neznamy/Some Book (3)",
			author_folder="Neznamy",
			authors=["Neznámý"],
			title="Some Title",
		)
		d = rule_c9_anonym(m)
		assert d is not None
		assert d.category == "C9"
		assert d.verdict.value == "NEEDS_REVIEW"

	def test_empty_authors_with_neznamy_folder_fires_c9(self):
		"""No author in metadata, folder is Neznamy — C9 (author is lost)."""
		m = _meta(
			path="Neznamy/Some Book (4)",
			author_folder="Neznamy",
			authors=[],
			title="Some Title",
		)
		d = rule_c9_anonym(m)
		assert d is not None
		assert d.category == "C9"

	def test_anonym_in_authors_without_neznamy_folder_fires_c9(self):
		"""Author is 'Anonymní', folder is something else — still C9."""
		m = _meta(
			path="Some Folder/Some Book (5)",
			author_folder="Some Folder",
			authors=["Anonymní"],
			title="Some Title",
		)
		d = rule_c9_anonym(m)
		assert d is not None
		assert d.category == "C9"

	def test_genuine_anonym_bible_is_ok(self):
		"""Bible and similar religious/folkloric titles are whitelisted as
		genuine anonymous works — verdict OK, not NEEDS_REVIEW."""
		m = _meta(
			path="Neznamy/Bible (6)",
			author_folder="Neznamy",
			authors=["Neznámý"],
			title="Bible",
		)
		d = rule_c9_anonym(m)
		assert d is not None
		assert d.category == "C9"
		assert d.verdict.value == "OK"

	def test_clean_book_not_flagged_c9(self):
		"""A normal book with a real author and a normal folder is not C9."""
		m = _meta(
			authors=["Agatha Christie"],
			title="Smrt lorda Edgwarea",
		)
		assert rule_c9_anonym(m) is None

	def test_detect_chains_c9_after_real_author_check(self):
		"""detect() returns MISSING_ISBN (not C9) for a Neznamy-folder book
		that already has a real author but is missing an ISBN."""
		m = _meta(
			path="Neznamy/Some Book (7)",
			author_folder="Neznamy",
			authors=["Roger Zelazny"],
			title="Devět princů Amberu",
			isbn=None,
			year=2012,
		)
		d = detect(m)
		assert d.category == "MISSING_ISBN"
