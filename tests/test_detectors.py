"""Tests for the detector rules in detectors.py.

Focused on rule_c9_anonym, which had a major false-positive: books living in
a 'Neznamy/' folder but already carrying a real author in metadata.json were
flagged C9 (~800 books in the real library). The fix: a real author in
meta.authors means the book is NOT C9, regardless of the folder name.

Also covers rule_c2_filename_title, which had an even bigger false-positive:
~1984 healthy books flagged because the title matched the filename stem
after accent-stripping. Calibre strips diacritics from filenames but not
from the title field, so a healthy book's "Čas přílivu" title always
matched its "Cas prilivu" filename. The fix: compare directly (not
accent-stripped), so only genuine filename-as-title cases fire.
"""
from __future__ import annotations

from book_meta_fix.detectors import (
	detect,
	rule_c12_bad_author,
	rule_c2_filename_title,
	rule_c9_anonym,
)
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


class TestC2FilenameTitle:
	"""rule_c2_filename_title had a massive false-positive: the stem-match
	signal compared title to the filename stem AFTER accent-stripping, which
	matched every healthy CZ/SK book (calibre strips diacritics from
	filenames). Now compares directly — only genuine filename-as-title fires."""

	def test_healthy_title_with_diacritics_not_c2(self):
		"""The bug: title 'Čas přílivu' matched filename 'Cas prilivu' after
		accent-stripping. A healthy title must NOT fire C2."""
		m = _meta(
			title="Čas přílivu",
			primary_file="/lib/!as py!livu/Cas prilivu - !as py!livu.epub",
		)
		assert rule_c2_filename_title(m) is None

	def test_title_equals_filename_stem_fires_c2(self):
		"""Genuine case: the title field IS the filename (no diacritics)."""
		m = _meta(
			title="Cas prilivu",
			primary_file="/lib/Author/Cas prilivu - Author.epub",
		)
		d = rule_c2_filename_title(m)
		assert d is not None
		assert d.category == "C2"
		assert "primary file stem" in d.reason

	def test_extension_in_title_fires_c2(self):
		"""File extension in the title is a strong filename signal."""
		m = _meta(title="Some Book.epub")
		d = rule_c2_filename_title(m)
		assert d is not None
		assert d.category == "C2"

	def test_word_temp_prefix_fires_c2(self):
		"""MS-Word temp-file prefix 'microsoft word -' in the title."""
		m = _meta(title="Microsoft Word - Document1")
		d = rule_c2_filename_title(m)
		assert d is not None
		assert d.category == "C2"

	def test_truncated_marker_fires_c2(self):
		"""Truncated slug markers (_n_, _txt) indicate filename pollution."""
		m = _meta(title="Some_Book_txt")
		d = rule_c2_filename_title(m)
		assert d is not None
		assert d.category == "C2"

	def test_clean_title_not_c2(self):
		"""A normal title with no filename signals must not fire C2."""
		m = _meta(title="Báječná léta pod psa")
		assert rule_c2_filename_title(m) is None

	def test_stem_match_case_insensitive(self):
		"""Title matching the stem in different case still fires C2."""
		m = _meta(
			title="SOME BOOK",
			primary_file="/lib/Author/Some Book - Author.epub",
		)
		d = rule_c2_filename_title(m)
		assert d is not None
		assert d.category == "C2"


class TestC12BadAuthor:
	"""rule_c12_bad_author catches author-field slug/artefact pollution that
	organize() faithfully turned into author-folder names (e.g. 'anthony
	burgess', '_ antologie', 'jsvoboda'). These were previously invisible to
	detection and left sitting in the library root."""

	def test_all_lowercase_author_fires_c12(self):
		"""A real person name always has a capital; all-lowercase is slug pollution."""
		m = _meta(author_folder="anthony burgess", authors=["anthony burgess"])
		d = rule_c12_bad_author(m)
		assert d is not None
		assert d.category == "C12"
		assert d.verdict.value == "NEEDS_REVIEW"
		assert "lowercase" in d.reason

	def test_glued_lowercase_author_fires_c12(self):
		"""'jsvoboda' — glued, all-lowercase. Filename slug artefact."""
		m = _meta(author_folder="jsvoboda", authors=["jsvoboda"])
		d = rule_c12_bad_author(m)
		assert d is not None
		assert d.category == "C12"

	def test_underscore_prefix_fires_c12(self):
		"""Leading underscore is a slug artefact ('_ antologie')."""
		m = _meta(author_folder="_ antologie", authors=["* antologie"])
		d = rule_c12_bad_author(m)
		assert d is not None
		assert d.category == "C12"
		assert "prefix" in d.reason

	def test_asterisk_prefix_fires_c12(self):
		"""Leading asterisk is a slug artefact."""
		m = _meta(author_folder="* edice", authors=["* edice"])
		d = rule_c12_bad_author(m)
		assert d is not None
		assert d.category == "C12"

	def test_proper_name_not_c12(self):
		"""A correctly capitalized name must not fire C12."""
		m = _meta(author_folder="Karel Čapek", authors=["Karel Čapek"])
		assert rule_c12_bad_author(m) is None

	def test_foreign_capitalized_not_c12(self):
		"""Foreign names with capitals are fine."""
		m = _meta(author_folder="Agatha Christie", authors=["Agatha Christie"])
		assert rule_c12_bad_author(m) is None

	def test_anonym_not_swallowed_by_c12(self):
		"""Anonym spellings must reach C9 (which knows the Bible/Koran whitelist),
		not get caught here as 'all-lowercase'."""
		m = _meta(author_folder="anonym", authors=["anonym"])
		# C12 skips anonym; full detect() routes it to C9.
		assert rule_c12_bad_author(m) is None
		d = detect(m)
		assert d.category == "C9"

	def test_detect_routes_bad_author_to_c12(self):
		"""End-to-end: detect() returns C12 for an all-lowercase author."""
		m = _meta(author_folder="anthony burgess", authors=["anthony burgess"])
		d = detect(m)
		assert d.category == "C12"
