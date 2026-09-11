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
	rule_c1_swap,
	rule_c2_filename_title,
	rule_c8_translator,
	rule_c9_anonym,
	rule_c12_bad_author,
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
	"""rule_c2_filename_title catches filename ARTEFACTS in the title value
	(extension, Word temp prefix, truncated markers) — not the title merely
	equaling the file stem. The historic stem-match signal was removed
	(2026-09-11, owner decision): the filename is an artifact nothing reads,
	and flagging on it dragged fine books through the extract/online/LLM
	ladder. (Before that, the stem match itself had a false-positive era:
	it compared accent-stripped, matching every healthy CZ/SK book.)"""

	def test_healthy_title_with_diacritics_not_c2(self):
		"""A healthy title 'Čas přílivu' next to filename 'Cas prilivu' must
		not fire C2."""
		m = _meta(
			title="Čas přílivu",
			primary_file="/lib/!as py!livu/Cas prilivu - !as py!livu.epub",
		)
		assert rule_c2_filename_title(m) is None

	def test_title_equals_filename_stem_not_c2(self):
		"""The retired signal: title == filename stem alone is NOT corruption —
		the filename is an artifact nothing reads (the library shows folder
		names, placement regenerates them)."""
		m = _meta(
			title="Cas prilivu",
			primary_file="/lib/Author/Cas prilivu - Author.epub",
		)
		assert rule_c2_filename_title(m) is None

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

	def test_stem_match_case_insensitive_not_c2(self):
		"""Title matching the stem in different case must not fire either —
		case never rescues the retired signal."""
		m = _meta(
			title="SOME BOOK",
			primary_file="/lib/Author/Some Book - Author.epub",
		)
		assert rule_c2_filename_title(m) is None


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

	def test_lowercase_folder_clean_authors_not_c12(self):
		"""The false positive: garbage all-lowercase FOLDER (needfix/crosscheck
		quarantine paths, mojibake title fragments) while the metadata author
		is a proper name. The folder is C13's business (a move), not an author
		problem — C12 must stay silent so C13 can lead with a pre-filled
		accept."""
		m = _meta(author_folder="!as py!livu", authors=["Agatha Christie"])
		assert rule_c12_bad_author(m) is None

	def test_prefix_folder_clean_authors_not_c12(self):
		"""Same shape with an artefact-prefix folder ('_'): metadata author
		clean → location problem, not author pollution."""
		m = _meta(author_folder="_", authors=["Jan Matzál Troska"])
		assert rule_c12_bad_author(m) is None

	def test_folder_is_evidence_when_authors_empty(self):
		"""No author in the metadata → the folder is the only author evidence
		and still fires C12."""
		m = _meta(author_folder="anthony burgess", authors=[])
		d = rule_c12_bad_author(m)
		assert d is not None
		assert d.category == "C12"
		assert "lowercase" in d.reason

	def test_folder_fallback_when_authors_blank(self):
		"""An authors list of empty strings gives nothing to judge — the
		folder fallback must engage (the folder candidate is not silently
		lost)."""
		m = _meta(author_folder="_ antologie", authors=[""])
		d = rule_c12_bad_author(m)
		assert d is not None
		assert "prefix" in d.reason


class TestC8Translator:
	"""rule_c8_translator: a translator may sit AMONG the comma-separated
	authors (Audiobookshelf has no translator field — the authors entry is
	what keeps the book searchable by translator), but must not sit IN THE
	AUTHOR'S PLACE (leading the list / carrying a translator label)."""

	def test_translator_next_to_author_not_c8(self):
		"""The accepted shape: foreign author first, CZ translator after.
		Measured 2026-09-09: every mixed list in the library looks like
		this — the old rule flagged all of them for nothing."""
		m = _meta(author_folder="Arthur C. Clarke", authors=["Arthur C. Clarke", "Josef Škvorecký"])
		assert rule_c8_translator(m) is None

	def test_coauthor_pair_not_c8(self):
		"""The old false positive: Kuttner + Moore are co-authors (Moore's
		Czech-married spelling looks CZ to the heuristic)."""
		m = _meta(author_folder="Henry Kuttner", authors=["Henry Kuttner", "Catherine L. Mooreová"])
		assert rule_c8_translator(m) is None

	def test_translator_label_fires_c8(self):
		"""A translator label in an author entry is unambiguous — flag HIGH,
		strip the label, keep the bare name in the list."""
		m = _meta(author_folder="Arthur C. Clarke", authors=["Arthur C. Clarke", "přeložil Josef Škvorecký"])
		d = rule_c8_translator(m)
		assert d is not None
		assert d.category == "C8"
		assert d.confidence.value == "HIGH"
		assert "označení" in d.reason

	def test_translator_first_not_c8(self):
		"""Name ORDER alone must not fire: a CZ-looking name leading with a
		foreign name behind cannot be told apart from a Czech
		adaptor/editor legitimately first (measured false positives: Kate
		Wilhelmová — an American with a feminized Czech surname; Josef V.
		Pleva leading Daniel Defoe). The content flows own the wrong-author
		case."""
		m = _meta(author_folder="Kate Wilhelmová", authors=["Kate Wilhelmová", "Theodor L. Thomas"])
		assert rule_c8_translator(m) is None
		m = _meta(author_folder="Josef V. Pleva", authors=["Josef V. Pleva", "Daniel Defoe"])
		assert rule_c8_translator(m) is None

	def test_lone_cz_author_not_c8(self):
		"""A single CZ name is usually a real Czech author — a lone
		translator with the author lost is not metadata-detectable (the
		content/identity flows own that case)."""
		m = _meta(author_folder="Karel Čapek", authors=["Karel Čapek"])
		assert rule_c8_translator(m) is None

	def test_four_authors_no_c10(self):
		"""The retired C10: a 4+ author list is no longer flagged. Both of
		its hypotheses are accepted states now — real co-authors (an
		anthology) or translators riding along — so there is nothing left
		for a human to verify (measured: the 12 C10 entries were genuine
		anthologies, mostly already accepted)."""
		m = _meta(author_folder="Jaroslav Veis", authors=["Jaroslav Veis", "Robert Silverberg", "J. G. Ballard", "Henry Kuttner"])
		assert rule_c8_translator(m) is None

	def test_detect_routes_translator_label_to_c8(self):
		"""End-to-end: detect() returns C8 for the label shape."""
		m = _meta(author_folder="Agatha Christie", authors=["Agatha Christie", "přeložil Marek Roesel"])
		d = detect(m)
		assert d.category == "C8"


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


class TestLocationRule:
	"""C13: the book's folder must match the pattern-derived target path.

	The rule exists only when detect()/detect_all() get library_root — every
	legacy caller stays location-blind by default.
	"""

	def _placed(self, tmp_path, rel="Jan Novak/Kniha (7)", *, title="Kniha", author="Jan Novak", **kw):
		"""A book folder at *rel* (relative to the library root) whose metadata
		say *title*/*author*. The immediate parent is the author folder and the
		calibre_id comes from the "(N)" folder suffix — the same shape
		readers._parse_path produces for real folders."""
		import re as _re
		from pathlib import Path

		relp = Path(rel)
		m = _re.search(r"\((\d+)\)\s*$", relp.name)
		kw.setdefault("calibre_id", int(m.group(1)) if m else 1)
		return _meta(path=str(tmp_path / rel), author_folder=relp.parent.name,
		             title_folder=relp.name, title=title, authors=[author], **kw)

	def test_correct_path_no_c13(self, tmp_path):
		m = self._placed(tmp_path, isbn="9788020403114", year=2001)
		d = detect(m, library_root=tmp_path)
		assert "C13" not in {x.category for x in all_diagnoses(d)}

	def test_misplaced_fires_c13_with_location_proposal(self, tmp_path):
		m = self._placed(tmp_path, "Spatne/Misto (7)", isbn="9788020403114", year=2001)
		d = detect(m, library_root=tmp_path)
		assert d.category == "C13"
		assert d.verdict.value == "AUTO_FIXABLE"
		assert d.proposed == {"location": "Jan Novak/Kniha (7)"}

	def test_without_library_root_location_blind(self, tmp_path):
		"""Back-compat: detect() with no library_root never yields C13."""
		m = self._placed(tmp_path, "Spatne/Misto (7)", isbn="9788020403114", year=2001)
		d = detect(m)
		assert "C13" not in {x.category for x in all_diagnoses(d)}

	def test_custom_pattern(self, tmp_path):
		m = self._placed(tmp_path, "Spatne/Misto (7)", isbn="9788020403114", year=2001)
		d = detect(m, library_root=tmp_path, pattern="{author}/{title} [{year}]")
		assert d.proposed == {"location": "Jan Novak/Kniha [2001]"}

	def test_broken_metadata_stays_primary_over_c13(self, tmp_path):
		"""A book with a real problem (C2 filename title) keeps it primary —
		C13 rides in additional and the book routes to manual review."""
		m = self._placed(tmp_path, "Spatne/Misto (7)", title="soubor_epub.epub",
		                 isbn="9788020403114", year=2001)
		d = detect(m, library_root=tmp_path)
		assert d.category == "C2"
		assert "C13" in {x.category for x in d.additional}

	def test_c13_promoted_over_ok_verdict_primary(self, tmp_path):
		"""A genuine anonym (C9 whitelisted to OK) that is ALSO misplaced must
		not be masked by the OK verdict — C13 becomes primary so the book can
		be moved through review."""
		m = self._placed(tmp_path, "Neznamy/Bible (3)", title="Bible",
		                 author="Neznámý", isbn="9788020403114", year=2001)
		d = detect(m, library_root=tmp_path)
		assert d.category == "C13"
		assert "C9" in {x.category for x in d.additional}

	def test_genuine_anonym_at_correct_path_stays_ok(self, tmp_path):
		"""The promotion is C13-only: without a location mismatch the C9-OK
		verdict keeps ruling (and the book stays out of review)."""
		m = self._placed(tmp_path, "Anonym/Bible (3)", title="Bible",
		                 author="Neznámý", isbn="9788020403114", year=2001)
		d = detect(m, library_root=tmp_path)
		assert d.category == "C9"
		assert d.verdict.value == "OK"
		assert "C13" not in {x.category for x in all_diagnoses(d)}

	def test_needfix_path_fires_c13(self, tmp_path):
		"""A book sitting under needfix/ is misplaced by definition — the C13
		target is always the root pattern path (needfix/ stripped)."""
		m = self._placed(tmp_path, "needfix/Jan Novak/Kniha (7)",
		                 isbn="9788020403114", year=2001)
		d = detect(m, library_root=tmp_path)
		assert d.category == "C13"
		assert d.proposed == {"location": "Jan Novak/Kniha (7)"}


class TestEmptyBook:
	"""EMPTY_BOOK: a folder holding only metadata sidecars / backups / cover —
	the book file is gone. Any other file or a subdirectory disqualifies."""

	def _folder(self, tmp_path, files=("metadata.json", "metadata.opf")):
		folder = tmp_path / "A" / "T (1)"
		folder.mkdir(parents=True)
		for f in files:
			(folder / f).write_text("x", encoding="utf-8")
		return folder

	def _meta_at(self, folder, **kw):
		return _meta(path=str(folder), author_folder="A", title_folder="T (1)", **kw)

	def test_only_metadata_fires(self, tmp_path):
		from book_meta_fix.detectors import rule_empty_book

		folder = self._folder(tmp_path, ("metadata.json", "metadata.opf", "cover.jpg", "metadata.json.bak", "cover.jpg.bak"))
		m = self._meta_at(folder)
		d = rule_empty_book(m)
		assert d is not None and d.category == "EMPTY_BOOK"
		assert d.verdict.value == "AUTO_FIXABLE"

	def test_with_ebook_file_not_empty(self, tmp_path):
		from book_meta_fix.detectors import rule_empty_book

		folder = self._folder(tmp_path, ("metadata.json", "t.epub"))
		assert rule_empty_book(self._meta_at(folder)) is None

	def test_with_subdirectory_not_empty(self, tmp_path):
		from book_meta_fix.detectors import rule_empty_book

		folder = self._folder(tmp_path)
		(folder / "sub").mkdir()
		assert rule_empty_book(self._meta_at(folder)) is None

	def test_with_unrelated_file_not_empty(self, tmp_path):
		from book_meta_fix.detectors import rule_empty_book

		folder = self._folder(tmp_path, ("metadata.json", "poznamky.txt"))
		assert rule_empty_book(self._meta_at(folder)) is None

	def test_primary_over_other_rules(self, tmp_path):
		"""EMPTY_BOOK runs FIRST — with the book gone, no other rule's verdict
		about the metadata matters."""
		folder = self._folder(tmp_path)
		m = self._meta_at(folder, title="soubor_epub.epub", isbn="9788020403114", year=2001)
		d = detect(m, library_root=tmp_path)
		assert d.category == "EMPTY_BOOK"


class TestC14SeriesIndexInName:
	"""Series order glued into the series NAME ("Mark Stone #73") — the fix
	is a deterministic split into bare name + series_index."""

	def test_fires_on_hash_with_empty_index(self):
		from book_meta_fix.detectors import rule_c14_series_index_in_name

		m = _meta(series=[{"name": "Mark Stone #73", "index": ""}])
		d = rule_c14_series_index_in_name(m)
		assert d is not None and d.category == "C14"
		assert d.verdict.value == "AUTO_FIXABLE"
		assert d.proposed["series"] == "Mark Stone"
		assert d.proposed["series_index"] == "73"
		assert d.proposed["action"] == "accept"  # lossless split → bulk-approvable

	def test_does_not_fire_on_plain_string_series(self):
		# A plain "Name #N" string is the ABS-NATIVE manifest form: current
		# Audiobookshelf stores and parses series exactly that way, and
		# series_entry_pair splits the index back out at read time — there is
		# nothing left to repair, so C14 must not fire (only a glued NAME in
		# dict form is corruption).
		from book_meta_fix.detectors import rule_c14_series_index_in_name

		m = _meta(series=["Mark Stone - Kapitán Služby #77"])
		assert rule_c14_series_index_in_name(m) is None

	def test_decimal_index_normalized(self):
		from book_meta_fix.detectors import split_series_index

		assert split_series_index("Nadace #3,5") == ("Nadace", "3.5")
		assert split_series_index("Nadace # 12") == ("Nadace", "12")
		assert split_series_index("Nadace#7") == ("Nadace", "7")

	def test_equal_index_still_fires_name_only_wrong(self):
		from book_meta_fix.detectors import rule_c14_series_index_in_name

		m = _meta(series=[{"name": "Asterion #1", "index": "1"}])
		d = rule_c14_series_index_in_name(m)
		assert d is not None
		assert d.proposed["series"] == "Asterion"

	def test_differing_index_does_not_fire(self):
		# An explicit index that DISAGREES with the embedded one is a
		# deliberate state, not an obvious glitch — leave it to the human.
		from book_meta_fix.detectors import rule_c14_series_index_in_name

		m = _meta(series=[{"name": "Asterion #1", "index": "7"}])
		assert rule_c14_series_index_in_name(m) is None

	def test_healthy_series_does_not_fire(self):
		from book_meta_fix.detectors import rule_c14_series_index_in_name

		assert rule_c14_series_index_in_name(_meta(series=[{"name": "Nadace", "index": "3"}])) is None
		assert rule_c14_series_index_in_name(_meta(series=[])) is None

	def test_trailing_bare_number_not_split(self):
		# "MR-362 Espace 4" is a real series name — only the explicit "#N"
		# form is unambiguous (library survey: 353 #N vs 2 such names).
		from book_meta_fix.detectors import rule_c14_series_index_in_name, split_series_index

		assert split_series_index("MR-362 Espace 4") is None
		assert rule_c14_series_index_in_name(_meta(series=["MR-362 Espace 4"])) is None

	def test_bare_only_hash_is_not_a_series(self):
		from book_meta_fix.detectors import split_series_index

		assert split_series_index("#73") is None  # nothing would remain
		assert split_series_index("") is None

	def test_detect_routes_c14_as_primary(self):
		m = _meta(series=[{"name": "Mark Stone #73", "index": ""}])
		d = detect(m)
		assert d.category == "C14"


class TestC1KnownAuthor:
	"""The pool-armed C1 pattern: the TITLE field holds a KNOWN LIBRARY
	author. No per-book heuristic can see this — only the library-wide pool
	(build_known_author_pool) can answer "is this string an author?"."""

	def _pool(self):
		from book_meta_fix.normalize import build_known_author_pool

		# Dněprov cluster = 3 books (2 spellings union), Novák = 3 books —
		# the pool pattern requires >= _POOL_AUTHOR_MIN_BOOKS per author.
		books = [
			BookMeta(calibre_id="1", uuid="u1", title="Den zkázy", authors=["Anatolij Dněprov"], path="/lib/1"),
			BookMeta(calibre_id="2", uuid="u2", title="Návrat", authors=["A. Dněprov"], path="/lib/2"),
			BookMeta(calibre_id="3", uuid="u3", title="Třetí síla", authors=["Anatolij Dněprov"], path="/lib/4"),
			BookMeta(calibre_id="4", uuid="u4", title="Biografie", authors=["Jan Novák"], path="/lib/3"),
			BookMeta(calibre_id="5", uuid="u5", title="Podruhé", authors=["Jan Novák"], path="/lib/5"),
			BookMeta(calibre_id="6", uuid="u6", title="Potřetí", authors=["Jan Novák"], path="/lib/6"),
		]
		return build_known_author_pool(books)

	def test_variant_pair_fires_c1(self):
		"""title AND author are the same known person in two spellings —
		the real title is lost from the record entirely."""
		m = _meta(authors=["A. Dněprov"], title="Anatolij Dněprov", author_folder="A. Dněprov")
		d = rule_c1_swap(m, known_authors=self._pool())
		assert d is not None and d.category == "C1"
		assert "variant pair" in d.reason
		assert d.confidence.value == "HIGH" and d.verdict.value == "NEEDS_REVIEW"

	def test_classic_swap_fires_c1(self):
		"""The title is a known author, the author field holds the real
		title (not a person from the pool)."""
		m = _meta(authors=["Setkání s Rámou"], title="Anatolij Dněprov", author_folder="Setkání s Rámou")
		d = rule_c1_swap(m, known_authors=self._pool())
		assert d is not None and d.category == "C1"
		assert "author/title swap" in d.reason
		assert "ambiguous" not in d.reason

	def test_biography_shape_flags_ambiguity(self):
		"""Title is a known author AND the author field is ANOTHER known
		author: possibly a biography titled with its subject — still flagged,
		but the reason says so (the pipeline never auto-accepts this shape
		without content agreeing)."""
		m = _meta(authors=["Jan Novák"], title="Anatolij Dněprov", author_folder="Jan Novák")
		d = rule_c1_swap(m, known_authors=self._pool())
		assert d is not None and d.category == "C1"
		assert "ambiguous" in d.reason

	def test_without_pool_no_new_pattern(self):
		"""The library-blind rule keeps its historic behaviour: a title that
		is an author name but shares no ≥5-char token with the author field
		does not fire without the pool."""
		m = _meta(authors=["Setkání s Rámou"], title="Anatolij Dněprov", author_folder="Setkání s Rámou")
		assert rule_c1_swap(m) is None

	def test_unknown_title_author_not_fired(self):
		# A title that is not any library author never triggers the pool
		# pattern (here the author shares no token either).
		m = _meta(authors=["Karel May"], title="Vinnetou", author_folder="Karel May")
		assert rule_c1_swap(m, known_authors=self._pool()) is None

	def test_small_cluster_not_fired(self):
		"""The R.U.R. regression: ONE corrupted record (a title stored in an
		author field) mints a 1-book "author" cluster — below the pool bar it
		is not a known author, so a legitimate title of the same name is never
		flagged as a swap."""
		from book_meta_fix.normalize import build_known_author_pool

		pool = build_known_author_pool([
			BookMeta(calibre_id="9", uuid="u9", title="Světové drama", authors=["R.U.R."], path="/lib/9"),
		])
		m = _meta(authors=["Karel Čapek"], title="R.U.R.", author_folder="Karel Čapek")
		assert rule_c1_swap(m, known_authors=pool) is None

	def test_particle_name_folder_is_not_a_title(self):
		"""Pattern 2: a multi-token PERSON name with nobility/origin particles
		("Antoine de Saint-Exupéry") is a name, not a sentence-length title —
		Citadela is a legitimate short title, not a swapped author."""
		m = _meta(authors=["Antoine de Saint-Exupéry"], title="Citadela", author_folder="Antoine de Saint-Exupéry")
		assert rule_c1_swap(m) is None

	def test_detect_passes_pool_through(self):
		# detect(known_authors=...) threads the pool into the rule (the
		# run_pipeline wrapper relies on it).
		m = _meta(authors=["A. Dněprov"], title="Anatolij Dněprov", author_folder="A. Dněprov")
		d = detect(m, known_authors=self._pool())
		assert d.category == "C1" and "variant pair" in d.reason

	def test_detect_without_pool_unchanged(self):
		m = _meta(authors=["A. Dněprov"], title="Anatolij Dněprov", author_folder="A. Dněprov")
		d = detect(m)
		# Library-blind: still C1 here (token overlap), but with the generic
		# heuristic reason — the pool refinement must not leak in.
		assert d.category == "C1"
		assert "variant pair" not in d.reason
