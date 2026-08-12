"""Unit tests for pipeline helper functions (_is_better, _looks_broken).

Regression coverage for the crash where an int (year) was passed into
_is_better -> _looks_broken and raised TypeError.
"""
from __future__ import annotations

from unittest.mock import patch

from book_meta_fix.extractors import ExtractedMeta
from book_meta_fix.models import BookMeta, Confidence, Diagnosis, Verdict
from book_meta_fix.pipeline import _is_better, _llm_wants, _looks_broken, _process_book


class TestLooksBroken:
	def test_clean_string_not_broken(self):
		assert _looks_broken("1984") is False
		assert _looks_broken("Karel Čapek") is False

	def test_underscores_broken(self):
		assert _looks_broken("apek_Karel") is True

	def test_file_extension_broken(self):
		assert _looks_broken("title.epub") is True
		assert _looks_broken("title.pdf") is True

	def test_high_symbol_mojibake_broken(self):
		# High control/symbol chars (U+2000+) read as broken. Low Latin-1
		# mojibake like "Ä" (U+00C4) is NOT caught here — that's the encoding
		# module's job.
		assert _looks_broken("title\u2200end") is True  # ∀ U+2200
		assert _looks_broken("a\u202Eb") is True  # RLO U+202E (right-to-left override)

	def test_placeholder_values_broken(self):
		for v in ("Neznamy", "Unknown", "Neznámý", ""):
			assert _looks_broken(v) is True

	def test_none_broken(self):
		assert _looks_broken(None) is True

	def test_int_does_not_crash(self):
		"""Regression: int (year) must not raise TypeError."""
		assert _looks_broken(2020) is False
		# int 0 stringifies to "0", which is not in _BROKEN_VALUES and carries
		# no corruption signal — so it reads as not-broken. That's acceptable
		# (an unusual year, but not textual corruption).
		assert _looks_broken(0) is False

	def test_stringifies_arbitrary_types(self):
		# A float year, or any object — must not crash.
		assert _looks_broken(2020.0) is False


class TestIsBetter:
	def test_clean_beats_underscored(self):
		assert _is_better("Karel Čapek", "apek_Karel") is True

	def test_diacritics_beats_stripped(self):
		# "Čas přílivu" beats "Cas prilivu" (same text, diacritics restored)
		assert _is_better("Čas přílivu", "Cas prilivu") is True

	def test_stripped_does_not_beat_diacritics(self):
		assert _is_better("Cas prilivu", "Čas přílivu") is False

	def test_both_clean_not_better(self):
		assert _is_better("1984", "1984") is False
		assert _is_better("Babička", "Babička") is False

	def test_none_candidate_never_better(self):
		assert _is_better(None, "anything") is False

	def test_candidate_beats_none_current(self):
		assert _is_better("1984", None) is True

	def test_int_year_beats_none_current(self):
		"""Regression: online.year (int) vs missing meta.year."""
		assert _is_better(2020, None) is True

	def test_int_year_vs_int_year_not_better(self):
		assert _is_better(2020, 2020) is False

	def test_int_year_does_not_crash_against_string(self):
		"""Regression for the actual crash: _is_better(online.year, meta.year)
		where online.year is an int and meta.year is None/int."""
		# This is the exact call shape that crashed (pipeline.py:255).
		# Must not raise TypeError.
		result = _is_better(2020, None)
		assert result is True
		result = _is_better(2020, 0)  # current falsy int
		assert result is True
		result = _is_better(2020, 2020)
		assert result is False

	def test_isbn_string_vs_none(self):
		assert _is_better("9788073099992", None) is True


class TestLlmWants:
	def test_all_includes_every_category_except_c9(self):
		# 'ALL' must cover C1..C8, C10 but NOT C9 (legitimate anonyms).
		for cat in ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C10"):
			assert _llm_wants(cat, ("ALL",)) is True, f"{cat} should be included by ALL"
		assert _llm_wants("C9", ("ALL",)) is False

	def test_explicit_list_only_matches_listed(self):
		cats = ("C1", "C4")
		assert _llm_wants("C1", cats) is True
		assert _llm_wants("C4", cats) is True
		# Unlisted categories are excluded even if they exist.
		assert _llm_wants("C2", cats) is False
		assert _llm_wants("C9", cats) is False

	def test_empty_tuple_excludes_everything(self):
		for cat in ("C1", "C2", "C9"):
			assert _llm_wants(cat, ()) is False

	def test_all_with_explicit_exclusion(self):
		# 'ALL' minus a manually-listed exclusion isn't a feature (the tuple is
		# union-style), but ALL alone already excludes C9. Verify the sentinel
		# takes precedence: ALL in tuple -> C9 excluded regardless of others.
		assert _llm_wants("C9", ("ALL", "C9")) is False

	def test_unknown_category_with_all(self):
		# A category we've never heard of (e.g. 'ERROR', custom detectors)
		# still gets sent under ALL — only C9 is special-cased.
		assert _llm_wants("ERROR", ("ALL",)) is True
		assert _llm_wants("CUSTOM_X", ("ALL",)) is True


# ---------------------------------------------------------------------------
# accept_missing_if_identified: MISSING_* books whose author+title were
# confirmed against the book's content are stamped with an identity_confirmed
# EnrichedMeta so review_writer pre-fills action: accept (and `bmf apply`
# prunes them). Covers the gap where no enricher/text_meta recovered the field.
# ---------------------------------------------------------------------------


def _missing_isbn_book(title="Bílá nemoc", author="Karel Čapek", calibre_id=10) -> BookMeta:
	return BookMeta(
		calibre_id=calibre_id, title=title, authors=[author],
		path=f"/lib/{title} ({calibre_id})",
		primary_file=f"/lib/{title} ({calibre_id})/book.epub",
	)


def _empty_stats() -> dict:
	return {
		"ok": 0, "needs_review": 0, "det_fixed": 0, "online_fixed": 0,
		"llm_fixed": 0, "llm_skipped_no_text": 0, "llm_no_result": 0, "llm_error": 0,
		"unfixed": 0, "errors": 0, "content_mismatch": 0,
		"covers_generated": 0, "covers_missing": 0, "accepted_missing": 0,
	}


def _run_accept(meta, *, first_page_text, additional=None, accept=True):
	"""Run _process_book with detect_fn -> MISSING_ISBN (AUTO_FIXABLE) and
	_safe_extract mocked to return an ExtractedMeta with *first_page_text*.
	No enricher, no LLM. Returns (result_tuple, stats)."""
	from book_meta_fix import pipeline as pmod

	def fake_detect(_m):
		d = Diagnosis(category="MISSING_ISBN", reason="no isbn", confidence=Confidence.LOW, verdict=Verdict.AUTO_FIXABLE)
		if additional:
			d.additional = list(additional)
		return d

	def fake_extract(_m):
		if first_page_text is None:
			return None
		return ExtractedMeta(first_page_text=first_page_text)

	patches = [patch.object(pmod, "detect_fn", fake_detect), patch.object(pmod, "_safe_extract", fake_extract)]
	for p in patches:
		p.start()
	try:
		stats = _empty_stats()
		result = _process_book(
			meta, enricher=None, skip_enrich=True, skip_verify=False,
			llm_provider=None, llm_categories=("ALL",), stats=stats,
			accept_missing_if_identified=accept,
		)
	finally:
		for p in patches:
			p.stop()
	return result, stats


class TestAcceptMissingIdentified:
	def test_identity_confirmed_no_proposal_stamps_accept(self):
		# Title + author appear in the first-page text -> _acquire_identity
		# confirms -> a minimal identity_confirmed EnrichedMeta is stamped.
		meta = _missing_isbn_book()
		text = "Bílá nemoc\nKarel Čapek\nRomán o lidské slušnosti."
		result, stats = _run_accept(meta, first_page_text=text)
		_enriched = result[3]
		assert _enriched is not None
		assert _enriched.identity_confirmed is True
		# No fields carried — proposed stays empty (accept-as-is, no fake change).
		assert _enriched.title is None and not _enriched.authors
		assert stats["accepted_missing"] == 1

	def test_identity_not_confirmed_stays_unfixed(self):
		# First-page text does NOT contain the title/author -> identity cannot be
		# confirmed -> no stamp, falls through to unfixed (stays for review).
		meta = _missing_isbn_book()
		text = "Lorem ipsum dolor sit amet, consectetur adipiscing elit."
		result, stats = _run_accept(meta, first_page_text=text)
		assert result[3] is None
		assert stats["accepted_missing"] == 0
		assert stats["unfixed"] == 1

	def test_no_content_stays_unfixed(self):
		# No extractable text at all -> _acquire_identity returns None -> unfixed.
		meta = _missing_isbn_book()
		result, stats = _run_accept(meta, first_page_text=None)
		assert result[3] is None
		assert stats["accepted_missing"] == 0

	def test_additional_needs_review_blocks_accept(self):
		# A co-occurring NEEDS_REVIEW diagnosis (e.g. generated cover C11) keeps
		# the book in review even though identity is confirmed.
		meta = _missing_isbn_book()
		text = "Bíla nemoc\nKarel Čapek"
		extra = [Diagnosis(category="C11", reason="generated cover", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)]
		result, stats = _run_accept(meta, first_page_text=text, additional=extra)
		assert result[3] is None
		assert stats["accepted_missing"] == 0

	def test_disabled_flag_no_stamp(self):
		# --no-accept-missing: never stamp, even when identity is confirmed.
		meta = _missing_isbn_book()
		text = "Bílá nemoc\nKarel Čapek"
		result, stats = _run_accept(meta, first_page_text=text, accept=False)
		assert result[3] is None
		assert stats["accepted_missing"] == 0
		assert stats["unfixed"] == 1

	def test_apply_action_accept_empty_proposal_is_noop(self):
		# `bmf apply` on an accept-as-is entry (empty proposed) must NOT touch
		# metadata — _apply_action gates the whole accept block on item.proposed.
		from types import SimpleNamespace

		from book_meta_fix.pipeline import _apply_action

		meta = _missing_isbn_book()
		before = (meta.title, list(meta.authors), meta.isbn, meta.year, meta.publisher)
		item = SimpleNamespace(action="accept", proposed=None, diagnoses=None, diagnosis=None, id=10, path=meta.path)
		_apply_action(meta, item)
		assert (meta.title, list(meta.authors), meta.isbn, meta.year, meta.publisher) == before
