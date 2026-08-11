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
	_is_anonym_spelling,
	all_diagnoses,
	detect,
	detect_all,
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


class TestAnonymSpellings:
	"""_is_anonym_spelling and the C9 detector must recognize the full range
	of anonym spellings found in the library ('Neznamy', 'neznámý - neuveden')
	AND defensible variants not yet seen ('autor neuveden', 'Neznámý autor',
	'enznámý' typo) — while leaving real-name phrases like 'Neznámý vojín'
	(Unknown Soldier) alone."""

	def test_basic_spellings(self):
		"""The canonical spellings are all recognized."""
		for s in ("anonym", "anonymní", "anonymous", "neznamy", "neznámý",
		          "neuveden", "unknown", ""):
			assert _is_anonym_spelling(s), f"{s!r} should be anonym"

	def test_compound_phrase_is_anonym(self):
		"""'neznámý - neuveden' — compound of two anonym spellings joined by
		a separator. Found in the real library (Bible - Nový zákon)."""
		assert _is_anonym_spelling("neznámý - neuveden") is True

	def test_compound_with_autor_is_anonym(self):
		"""'autor neuveden' and 'Neznámý autor' — 'autor' is a neutral token
		that, combined with an anonym spelling, denotes anonym."""
		assert _is_anonym_spelling("autor neuveden") is True
		assert _is_anonym_spelling("Neznámý autor") is True

	def test_enznamy_typo_is_anonym(self):
		"""'enznámý' is a common typo of 'neznámý' (transposed first letters)."""
		assert _is_anonym_spelling("enznámý") is True

	def test_unknown_soldier_not_anonym(self):
		"""'Neznámý vojín' (Unknown Soldier) is NOT anonym — 'vojín' is a real
		noun. This is the key false-positive the user warned about."""
		assert _is_anonym_spelling("neznámý vojín") is False
		assert _is_anonym_spelling("Neznámý vojín") is False

	def test_bare_autor_is_not_anonym(self):
		"""The bare word 'autor' is a placeholder, not an anonym spelling —
		it must be caught by C5 (_PLACEHOLDER_RE), not reach C9 via this path."""
		assert _is_anonym_spelling("autor") is False

	def test_real_author_not_anonym(self):
		"""A real person name is never an anonym spelling."""
		assert _is_anonym_spelling("Karel Čapek") is False
		assert _is_anonym_spelling("Agatha Christie") is False

	def test_none_not_anonym(self):
		"""Defensive: None input returns False, not an exception."""
		assert _is_anonym_spelling(None) is False

	def test_detect_routes_compound_anonym_to_c9(self):
		"""End-to-end: detect() returns C9 for a 'autor neuveden' author."""
		m = _meta(author_folder="autor neuveden", authors=["autor neuveden"])
		d = detect(m)
		assert d.category == "C9"

	def test_bible_in_neznamy_neuveden_is_ok(self):
		"""Regression for the real library: Bible with author 'neznámý - neuveden'
		is a genuine anonymous work and must be C9 verdict OK (whitelisted), not
		C12 (all-lowercase) or C9 NEEDS_REVIEW."""
		m = _meta(
			author_folder="neznámý - neuveden",
			authors=["neznámý - neuveden"],
			title="Bible - Nový zákon",
		)
		d = detect(m)
		assert d.category == "C9"
		assert d.verdict.value == "OK"


class TestDetectAll:
	"""detect_all surfaces every matching rule, not just the first. detect()
	returns the first match as primary with the rest in .additional."""

	def test_multiple_enrichment_diagnoses(self):
		"""A clean book missing ISBN/year/cover matches several enrichment rules
		at once — detect_all returns all of them."""
		# Default _meta: clean title/author, no isbn/year, no cover.jpg on disk.
		m = _meta()
		diags = detect_all(m)
		cats = [d.category for d in diags]
		# All three enrichment problems are present.
		assert "MISSING_ISBN" in cats
		assert "MISSING_YEAR" in cats
		assert "MISSING_COVER" in cats

	def test_detect_carries_additional(self):
		"""detect() primary is the first match; the rest land in .additional."""
		m = _meta()
		diags = detect_all(m)
		d = detect(m)
		assert d.category == diags[0].category
		assert {a.category for a in d.additional} == {x.category for x in diags[1:]}

	def test_all_diagnoses_flattens(self):
		m = _meta()
		d = detect(m)
		assert [x.category for x in all_diagnoses(d)] == [d.category, *(a.category for a in d.additional)]

	def test_all_diagnoses_none_safe(self):
		"""Callers (e.g. _build_proposed) may pass diag=None — must not raise."""
		assert all_diagnoses(None) == []

	def test_clean_book_has_no_enrichment_or_cover_diagnoses(self, tmp_path):
		"""A book with isbn, year and a real (non-placeholder) cover triggers no
		enrichment or cover rule."""
		from PIL import Image

		# A colour-rich gradient (not a solid fill) so analyze_cover does NOT
		# classify it as a generated placeholder.
		img = Image.new("RGB", (300, 450))
		px = img.load()
		for y in range(450):
			for x in range(300):
				px[x, y] = ((x + y) % 256, (x * 2) % 256, (y * 2) % 256)
		img.save(tmp_path / "cover.jpg")
		m = _meta(path=str(tmp_path), isbn="9788020403114", year=2001)
		cats = {d.category for d in detect_all(m)}
		assert cats.isdisjoint({"MISSING_ISBN", "MISSING_YEAR", "MISSING_COVER", "C11"})
