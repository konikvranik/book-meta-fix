"""Unit tests for pipeline helper functions (_is_better, _looks_broken).

Regression coverage for the crash where an int (year) was passed into
_is_better -> _looks_broken and raised TypeError.
"""
from __future__ import annotations

from unittest.mock import patch

from book_meta_fix.extractors import ExtractedMeta
from book_meta_fix.models import BookMeta, Confidence, Diagnosis, Verdict
from book_meta_fix.pipeline import _is_better, _llm_wants, _looks_broken, _process_book, _try_known_author_swap


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
	def test_all_includes_every_category_including_c9(self):
		# 'ALL' covers C1..C10. C9 is NO LONGER excluded: genuine anonymous works
		# (Bible/Koran/…) are whitelisted to OK in rule_c9_anonym, so every C9
		# reaching the LLM path is a corrupted record that needs author recovery.
		for cat in ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10"):
			assert _llm_wants(cat, ("ALL",)) is True, f"{cat} should be included by ALL"

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

	def test_all_includes_c9_even_alongside_explicit_listing(self):
		# 'ALL' is union-style; nothing is special-cased out any more, so C9 is
		# included whenever 'ALL' is present (here together with an explicit C9).
		assert _llm_wants("C9", ("ALL", "C9")) is True

	def test_unknown_category_with_all(self):
		# A category we've never heard of (e.g. 'ERROR', custom detectors)
		# still gets sent under ALL — nothing is special-cased any more.
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
	safe_extract mocked to return an ExtractedMeta with *first_page_text*.
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

	patches = [patch.object(pmod, "detect_fn", fake_detect), patch.object(pmod, "safe_extract", fake_extract)]
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
		# Title + author appear in the first-page text -> acquire_identity
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
		# No extractable text at all -> acquire_identity returns None -> unfixed.
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

	def _run_accept_llm_low(self, *, first_page_text):
		"""_process_book with a stub LLM whose best answer is llm:low (the
		proposal failed verification — the realistic shape: garbage title,
		e.g. the 'Neznámý' title page). Deterministic fix finds nothing, so
		the LLM runs; its answer is returned as-is with the llm:low label.
		Returns (result_tuple, stats)."""
		from book_meta_fix import pipeline as pmod
		from book_meta_fix.llm import ReconciledMeta

		meta = _missing_isbn_book()

		class StubProvider:
			name = "stub"

			def reconcile_loop(self, evidence, extracted, **kwargs):
				return (
					ReconciledMeta(title="Neznámý", authors=["Karel Čapek"], genres=["Detektivky"], confidence="low"),
					"llm:low",
				)

		def fake_detect(_m):
			return Diagnosis(category="MISSING_ISBN", reason="no isbn", confidence=Confidence.LOW, verdict=Verdict.AUTO_FIXABLE)

		def fake_extract(_m):
			return ExtractedMeta(first_page_text=first_page_text)

		with patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "safe_extract", fake_extract), \
			 patch.object(pmod, "has_usable_text", lambda t: True), \
			 patch.object(pmod, "_try_deterministic_fix", lambda *a, **kw: None):
			stats = {**_empty_stats(), "llm_low_confidence": 0}
			result = _process_book(
				meta, enricher=None, skip_enrich=True, skip_verify=True,
				llm_provider=StubProvider(), llm_categories=("ALL",), stats=stats,
				accept_missing_if_identified=True,
			)
		return result, stats

	def test_llm_low_answer_does_not_block_accept_missing_stamp(self):
		# The LLM's answer failed verification (llm:low), but the book's own
		# content confirms its identity: the untrusted proposal is DISCARDED
		# (replaced by the minimal content stamp) instead of stranding an
		# acceptable-missing book in pending forever. Both stats coexist.
		result, stats = self._run_accept_llm_low(first_page_text="Bílá nemoc\nKarel Čapek\nRomán o lidské slušnosti.")
		enriched = result[3]
		assert stats["llm_low_confidence"] == 1
		assert stats["accepted_missing"] == 1
		# The minimal stamp: identity confirmed, no fields (accept-as-is).
		assert enriched.identity_confirmed is True
		assert enriched.source == "content"
		assert enriched.title is None and not enriched.authors

	def test_llm_low_without_identity_confirmation_stays_llm_low(self):
		# Identity NOT confirmable from the content: the stamp's own gate
		# (acquire_identity) rejects, the llm:low answer survives untouched
		# and the book stays pending with the LLM hint for a human reviewer.
		result, stats = self._run_accept_llm_low(first_page_text="Lorem ipsum dolor sit amet, consectetur adipiscing elit.")
		enriched = result[3]
		assert stats["accepted_missing"] == 0
		assert enriched.source == "llm:low"
		assert enriched.title == "Neznámý"

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


class TestCoverShadowedC13Routing:
	"""A C13-primary book whose extras carry a cover diagnosis (C11 generated
	cover / MISSING_COVER) is routed through the enrichment path (extraction +
	deterministic/online fill), NOT the cheap no-extraction path: C13 outranks
	the enrichment rules, so without the reroute the location rule shadows the
	cover problem as the primary on every run and no enricher is ever asked
	for a cover_url. The LLM gate still keys on the PRIMARY (is_needs_review)
	— a merely misplaced book never pays for an LLM call."""

	C11 = [Diagnosis(category="C11", reason="generated cover", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)]
	MISSING_ONLY = [Diagnosis(category="MISSING_ISBN", reason="no isbn", confidence=Confidence.LOW, verdict=Verdict.AUTO_FIXABLE)]

	def _run(self, *, additional, llm_provider=None):
		from book_meta_fix import pipeline as pmod

		meta = _missing_isbn_book(title="Kniha", author="Autor", calibre_id=21)
		extract_calls: list[int] = []

		def fake_detect(_m):
			d = Diagnosis(category="C13", reason="umístění", confidence=Confidence.HIGH, verdict=Verdict.AUTO_FIXABLE)
			d.additional = list(additional)
			return d

		def fake_extract(_m):
			extract_calls.append(1)
			return ExtractedMeta(first_page_text="Kniha\nAutor\nPrvní stránka knihy.")

		patches = [patch.object(pmod, "detect_fn", fake_detect), patch.object(pmod, "safe_extract", fake_extract)]
		for p in patches:
			p.start()
		try:
			stats = _empty_stats()
			result = _process_book(
				meta, enricher=None, skip_enrich=True, skip_verify=False,
				llm_provider=llm_provider, llm_categories=("ALL",), stats=stats,
			)
		finally:
			for p in patches:
				p.stop()
		return result, stats, extract_calls

	def test_c13_with_c11_extracts_and_enriches(self):
		result, stats, extract_calls = self._run(additional=self.C11)
		assert extract_calls == [1]  # NOT the cheap path — the enrichers get their chance
		assert stats["needs_review"] == 1
		# No enricher/LLM here and the text offers no better fields -> unfixed.
		assert result[3] is None
		assert stats["unfixed"] == 1

	def test_plain_c13_stays_cheap(self):
		# C13 with only benign extras (MISSING_*): still the cheap no-extraction
		# path — a misplaced-but-fine book must not pay for extraction.
		result, stats, extract_calls = self._run(additional=self.MISSING_ONLY)
		assert extract_calls == []
		assert stats["needs_review"] == 0
		assert result[3] is None

	def test_c13_with_c11_never_calls_llm(self):
		# The LLM gate keys on the primary (C13 is not needs-review): passing a
		# broken provider proves it is never touched (any use would raise inside
		# the reconciled try/except and bump stats["llm_error"]).
		_, stats, _ = self._run(additional=self.C11, llm_provider=object())
		assert stats["llm_error"] == 0
		assert stats["llm_no_result"] == 0


class TestSkipUuids:
	"""run_pipeline(skip_uuids=...) freezes keep-decided books: they are filtered
	out before the processing loop, so they never reach _process_book (no
	detect/extract/enrich/LLM), while the rest are processed normally."""

	def _two_books(self, tmp_path):
		from book_meta_fix.models import BookMeta
		return [
			BookMeta(calibre_id=1, uuid="keep-me", title="A", authors=["X"], path=str(tmp_path / "a")),
			BookMeta(calibre_id=2, uuid="process-me", title="B", authors=["Y"], path=str(tmp_path / "b")),
		]

	def test_skipped_book_is_not_processed(self, tmp_path):
		from book_meta_fix.pipeline import run_pipeline

		seen = []

		def fake_process(meta, *a, **k):
			seen.append(meta.uuid)
			return (meta, None, None, None)

		with patch("book_meta_fix.pipeline.scan_library", return_value=self._two_books(tmp_path)), \
			patch("book_meta_fix.pipeline._process_book", side_effect=fake_process):
			results = run_pipeline(tmp_path, skip_uuids={"keep-me"}, workers=1, only_needs_review=False)
		assert seen == ["process-me"]
		assert {r[0].uuid for r in results} == {"process-me"}

	def test_no_skip_processes_all(self, tmp_path):
		from book_meta_fix.pipeline import run_pipeline

		seen = []

		def fake_process(meta, *a, **k):
			seen.append(meta.uuid)
			return (meta, None, None, None)

		with patch("book_meta_fix.pipeline.scan_library", return_value=self._two_books(tmp_path)), \
			patch("book_meta_fix.pipeline._process_book", side_effect=fake_process):
			run_pipeline(tmp_path, skip_uuids=None, workers=1, only_needs_review=False)
		assert set(seen) == {"keep-me", "process-me"}


class TestSkipVerified:
	"""run_pipeline drops books whose metadata.json carries verified: true
	right after the scan — a closed book never pays detection again."""

	def _two_books(self, tmp_path):
		from book_meta_fix.models import BookMeta
		return [
			BookMeta(calibre_id=1, uuid="v1", verified=True, title="A", authors=["X"], path=str(tmp_path / "a")),
			BookMeta(calibre_id=2, uuid="p1", title="B", authors=["Y"], path=str(tmp_path / "b")),
		]

	def test_verified_book_skipped_by_default(self, tmp_path):
		from book_meta_fix.pipeline import run_pipeline

		seen = []

		def fake_process(meta, *a, **k):
			seen.append(meta.uuid)
			return (meta, None, None, None)

		with patch("book_meta_fix.pipeline.scan_library", return_value=self._two_books(tmp_path)), \
			patch("book_meta_fix.pipeline._process_book", side_effect=fake_process):
			run_pipeline(tmp_path, workers=1, only_needs_review=False)
		assert seen == ["p1"]

	def test_skip_verified_false_processes_all(self, tmp_path):
		from book_meta_fix.pipeline import run_pipeline

		seen = []

		def fake_process(meta, *a, **k):
			seen.append(meta.uuid)
			return (meta, None, None, None)

		with patch("book_meta_fix.pipeline.scan_library", return_value=self._two_books(tmp_path)), \
			patch("book_meta_fix.pipeline._process_book", side_effect=fake_process):
			run_pipeline(tmp_path, skip_verified=False, workers=1, only_needs_review=False)
		assert set(seen) == {"v1", "p1"}


class TestScannedBooksOutParam:
	"""run_pipeline(scanned_books=...) hands the caller the FULL scan — captured
	before the verified filter — so a library-wide pass like `analyze
	--normalize` clusters over the whole library (closed books anchor the
	spelling clusters) without paying a second scan."""

	def _two_books(self, tmp_path):
		from book_meta_fix.models import BookMeta
		return [
			BookMeta(calibre_id=1, uuid="v1", verified=True, title="A", authors=["X"], path=str(tmp_path / "a")),
			BookMeta(calibre_id=2, uuid="p1", title="B", authors=["Y"], path=str(tmp_path / "b")),
		]

	def test_out_param_gets_pre_filter_snapshot(self, tmp_path):
		from book_meta_fix.pipeline import run_pipeline

		seen = []

		def fake_process(meta, *a, **k):
			seen.append(meta.uuid)
			return (meta, None, None, None)

		scanned: list = []
		with patch("book_meta_fix.pipeline.scan_library", return_value=self._two_books(tmp_path)), \
			patch("book_meta_fix.pipeline._process_book", side_effect=fake_process):
			run_pipeline(tmp_path, workers=1, only_needs_review=False, scanned_books=scanned)
		# Processing still drops the verified book…
		assert seen == ["p1"]
		# …but the out-param holds BOTH, in scan order.
		assert [b.uuid for b in scanned] == ["v1", "p1"]

	def test_default_none_changes_nothing(self, tmp_path):
		from book_meta_fix.pipeline import run_pipeline

		seen = []

		def fake_process(meta, *a, **k):
			seen.append(meta.uuid)
			return (meta, None, None, None)

		with patch("book_meta_fix.pipeline.scan_library", return_value=self._two_books(tmp_path)), \
			patch("book_meta_fix.pipeline._process_book", side_effect=fake_process):
			results = run_pipeline(tmp_path, workers=1, only_needs_review=False)
		assert seen == ["p1"]
		assert {r[0].uuid for r in results} == {"p1"}


class TestProgressCallbacks:
	"""run_pipeline reports its two phases distinctly so a progress bar can
	label them separately: the library scan is forwarded to scan_library via
	scan_progress_callback, and the per-book progress_callback fires (0, total)
	BEFORE the first book starts — the first per-book callback would otherwise
	fire only when a book COMPLETES, which on an LLM-bound run can be minutes
	away (a bar pulsing at 0/None the whole time)."""

	def _two_books(self, tmp_path):
		return [
			BookMeta(calibre_id=1, uuid="u1", title="A", authors=["X"], path=str(tmp_path / "a")),
			BookMeta(calibre_id=2, uuid="u2", title="B", authors=["Y"], path=str(tmp_path / "b")),
		]

	def _fake_process(self, seen):
		def fake_process(meta, *a, **k):
			seen.append(meta.uuid)
			return (meta, None, None, None)
		return fake_process

	def test_scan_callback_forwarded_to_scan_library(self, tmp_path):
		from book_meta_fix.pipeline import run_pipeline

		def scan_cb(done, total):
			pass

		seen = []
		with patch("book_meta_fix.pipeline.scan_library", return_value=self._two_books(tmp_path)) as scan_mock, \
			patch("book_meta_fix.pipeline._process_book", side_effect=self._fake_process(seen)):
			run_pipeline(tmp_path, workers=1, only_needs_review=False, scan_progress_callback=scan_cb)
		assert scan_mock.call_args.kwargs.get("progress_callback") is scan_cb

	def test_processing_total_announced_before_first_completion_serial(self, tmp_path):
		from book_meta_fix.pipeline import run_pipeline

		calls = []
		seen = []
		with patch("book_meta_fix.pipeline.scan_library", return_value=self._two_books(tmp_path)), \
			patch("book_meta_fix.pipeline._process_book", side_effect=self._fake_process(seen)):
			run_pipeline(tmp_path, workers=1, only_needs_review=False, progress_callback=lambda d, t: calls.append((d, t)))
		# (0, total) first — the bar total is known before any book completes.
		assert calls == [(0, 2), (1, 2), (2, 2)]

	def test_processing_total_announced_before_first_completion_parallel(self, tmp_path):
		from book_meta_fix.pipeline import run_pipeline

		calls = []
		seen = []
		with patch("book_meta_fix.pipeline.scan_library", return_value=self._two_books(tmp_path)), \
			patch("book_meta_fix.pipeline._process_book", side_effect=self._fake_process(seen)):
			run_pipeline(tmp_path, workers=2, only_needs_review=False, progress_callback=lambda d, t: calls.append((d, t)))
		assert calls[0] == (0, 2)
		assert sorted(calls[1:]) == [(1, 2), (2, 2)]

	def test_no_callback_still_fine(self, tmp_path):
		from book_meta_fix.pipeline import run_pipeline

		seen = []
		with patch("book_meta_fix.pipeline.scan_library", return_value=self._two_books(tmp_path)), \
			patch("book_meta_fix.pipeline._process_book", side_effect=self._fake_process(seen)):
			results = run_pipeline(tmp_path, workers=1, only_needs_review=False)
		assert {r[0].uuid for r in results} == {"u1", "u2"}


class TestFilterNotOk:
	"""The incremental OK-filter fans out over a thread pool (the C11 cover
	decode dominates its cost and Pillow releases the GIL). Selection and
	order must be IDENTICAL to the serial list comprehension."""

	@staticmethod
	def _books(n: int = 20) -> list[BookMeta]:
		return [
			BookMeta(calibre_id=i, uuid=f"u{i}", title=f"B{i}", authors=["A"], path=f"/lib/B{i} ({i})")
			for i in range(n)
		]

	@staticmethod
	def _detect_odd_needs_review(meta: BookMeta) -> Diagnosis:
		# Odd calibre_ids -> OK verdict, even -> NEEDS_REVIEW (filtered IN).
		verdict = Verdict.OK if meta.calibre_id % 2 else Verdict.NEEDS_REVIEW
		return Diagnosis(category="C2", reason="r", confidence=Confidence.HIGH, verdict=verdict)

	def test_parallel_selection_matches_serial(self):
		from book_meta_fix.pipeline import _filter_not_ok

		books = self._books()
		serial = _filter_not_ok(books, self._detect_odd_needs_review, workers=1)
		parallel = _filter_not_ok(books, self._detect_odd_needs_review, workers=4)
		assert [b.calibre_id for b in serial] == [b.calibre_id for b in parallel]
		# Order preserved, not completion order.
		assert [b.calibre_id for b in parallel] == sorted(b.calibre_id for b in parallel)

	def test_detect_runs_once_per_book(self):
		from book_meta_fix.pipeline import _filter_not_ok

		books = self._books(6)
		calls: list[int] = []

		def counting_detect(meta: BookMeta) -> Diagnosis:
			calls.append(meta.calibre_id)
			return self._detect_odd_needs_review(meta)

		_filter_not_ok(books, counting_detect, workers=4)
		assert sorted(calls) == sorted(b.calibre_id for b in books)

	def test_single_book_stays_serial(self):
		from book_meta_fix.pipeline import _filter_not_ok

		one = self._books(1)

		def ok_detect(_meta: BookMeta) -> Diagnosis:
			return Diagnosis(category="OK", reason="clean", confidence=Confidence.HIGH, verdict=Verdict.OK)

		# No pool is spawned for a single book (workers is capped by len());
		# the serial path must still select correctly.
		assert _filter_not_ok(one, ok_detect, workers=8) == []


class TestLocationAwarePipeline:
	"""With location_root the incremental filter sees C13; without it, a
	misplaced-but-clean book stays OK (location-blind) and is skipped."""

	def _lib(self, tmp_path):
		import json as _json

		lib = tmp_path / "lib"
		for rel in ("Jan Novak/Kniha (7)", "Spatne/Misto (8)"):
			folder = lib / rel
			folder.mkdir(parents=True)
			(folder / "metadata.json").write_text(_json.dumps({
				"title": "Kniha" if "Kniha" in rel else "Misto",
				"authors": ["Jan Novak"],
				"isbn": "9788020403117",
				"publishedYear": "2001",
			}), encoding="utf-8")
			# A real book file — without it the EMPTY_BOOK rule (which runs
			# first) would claim the folder instead of C13.
			(folder / "book.epub").write_text("x", encoding="utf-8")
		return lib

	def test_misplaced_book_reaches_pipeline_with_location_root(self, tmp_path):
		from book_meta_fix.pipeline import run_pipeline

		lib = self._lib(tmp_path)
		results = run_pipeline(lib, workers=1, skip_enrich=True, skip_verify=True, location_root=lib)
		cats = {r[1].category for r in results}
		assert "C13" in cats

	def test_location_blind_without_location_root(self, tmp_path):
		from book_meta_fix.pipeline import run_pipeline

		lib = self._lib(tmp_path)
		results = run_pipeline(lib, workers=1, skip_enrich=True, skip_verify=True, location_root=None)
		# Books may still be picked up for other reasons (no cover here), but
		# never with a C13 primary — detection stays location-blind.
		assert all(r[1].category != "C13" for r in results)


class TestKnownAuthorSwap:
	"""The C1 pool repair tier (_try_known_author_swap + its Step-2d wiring in
	_process_book): the record's TITLE holds a known library author, so the
	author comes from the pool canonical and the title from the book's own
	text, bound by confirm_identity. Every failure stays for review."""

	def _pool(self):
		from book_meta_fix.normalize import build_known_author_pool

		books = [
			BookMeta(calibre_id="1", uuid="u1", title="Den zkázy", authors=["Anatolij Dněprov"], path="/lib/1"),
			BookMeta(calibre_id="2", uuid="u2", title="Návrat", authors=["A. Dněprov"], path="/lib/2"),
			BookMeta(calibre_id="3", uuid="u3", title="Biografie", authors=["Jan Novák"], path="/lib/3"),
		]
		return build_known_author_pool(books)

	def _extract(self, **kw):
		text = kw.pop(
			"text",
			"Den zkázy\nAnatolij Dněprov\nRomán o osudu lidstva, pokračování slavné sci-fi série.",
		)
		return ExtractedMeta(first_page_text=text, **kw)

	def test_variant_pair_recovers_title_from_content(self):
		# title + author are the same person in two spellings: the record
		# never held the title — it comes from the mined text.
		meta = BookMeta(calibre_id=1, title="Anatolij Dněprov", authors=["A. Dněprov"], path="/lib/x (1)")
		r = _try_known_author_swap(meta, self._pool(), self._extract(title_from_text="Den zkázy"))
		assert r is not None
		assert r.title == "Den zkázy" and r.authors == ["Anatolij Dněprov"]
		assert r.source == "content" and r.identity_confirmed is True

	def test_classic_swap_needs_content_agreement(self):
		# The real title sits in the AUTHOR field — the swap is only trusted
		# when the book's own text says the same thing.
		meta = BookMeta(calibre_id=1, title="Anatolij Dněprov", authors=["Den zkázy"], path="/lib/x (1)")
		pool = self._pool()
		agree = _try_known_author_swap(meta, pool, self._extract(title_from_text="Den zkázy"))
		assert agree is not None
		assert agree.title == "Den zkázy" and agree.authors == ["Anatolij Dněprov"]
		disagree = _try_known_author_swap(meta, pool, self._extract(title_from_text="Návrat"))
		assert disagree is None

	def test_biography_vetoed_by_mining_disagreement(self):
		# A biography titled with its subject has both names in its own text
		# and would survive a naive swap self-test — the mined title (the
		# book's REAL title) matches the current title, not the author field,
		# and the disagreement vetoes the swap.
		meta = BookMeta(calibre_id=1, title="Anatolij Dněprov", authors=["Jan Novák"], path="/lib/x (1)")
		ex = self._extract(title_from_text="Anatolij Dněprov")
		assert _try_known_author_swap(meta, self._pool(), ex) is None

	def test_variant_pair_no_change_vetoed(self):
		# Mined title == current title: nothing would change (the biography
		# shape inside a variant pair).
		meta = BookMeta(calibre_id=1, title="Anatolij Dněprov", authors=["A. Dněprov"], path="/lib/x (1)")
		ex = self._extract(title_from_text="Anatolij Dněprov")
		assert _try_known_author_swap(meta, self._pool(), ex) is None

	def test_title_still_the_author_name_vetoed(self):
		# A mined "title" that is (a fuzzy variant of) the canonical author
		# repairs nothing — the C1 shape would survive the swap.
		meta = BookMeta(calibre_id=1, title="Anatolij Dněprov", authors=["A. Dněprov"], path="/lib/x (1)")
		ex = self._extract(title_from_text="Anatolije Dněprov")
		assert _try_known_author_swap(meta, self._pool(), ex) is None

	def test_unknown_title_author_no_repair(self):
		meta = BookMeta(calibre_id=1, title="Vinnetou", authors=["Karel May"], path="/lib/x (1)")
		assert _try_known_author_swap(meta, self._pool(), self._extract(title_from_text="Vinnetou")) is None

	def test_no_content_no_repair(self):
		meta = BookMeta(calibre_id=1, title="Anatolij Dněprov", authors=["A. Dněprov"], path="/lib/x (1)")
		assert _try_known_author_swap(meta, self._pool(), ExtractedMeta()) is None

	def test_process_book_wiring_counts_swap_fixed(self):
		from book_meta_fix import pipeline as pmod

		meta = BookMeta(calibre_id=1, title="Anatolij Dněprov", authors=["A. Dněprov"], path="/lib/x (1)", primary_file="/lib/x (1)/book.epub")

		def fake_detect(_m):
			return Diagnosis(category="C1", reason="variant pair", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		def fake_extract(_m):
			return ExtractedMeta(
				title_from_text="Den zkázy",
				first_page_text="Den zkázy\nAnatolij Dněprov\nRomán o osudu lidstva, pokračování slavné sci-fi série.",
			)

		with patch.object(pmod, "detect_fn", fake_detect), patch.object(pmod, "safe_extract", fake_extract):
			stats = _empty_stats()
			result = _process_book(
				meta, enricher=None, skip_enrich=True, skip_verify=False,
				llm_provider=None, llm_categories=("ALL",), stats=stats,
				known_authors=self._pool(),
			)
		enriched = result[3]
		assert enriched is not None
		assert enriched.title == "Den zkázy" and enriched.authors == ["Anatolij Dněprov"]
		assert enriched.identity_confirmed is True
		assert stats["swap_fixed"] == 1 and stats["unfixed"] == 0

	def test_process_book_without_pool_stays_unfixed(self):
		# known_authors=None (report/epubgen-style callers): the swap tier
		# never runs, the C1 book stays for review exactly as before.
		from book_meta_fix import pipeline as pmod

		meta = BookMeta(calibre_id=1, title="Anatolij Dněprov", authors=["A. Dněprov"], path="/lib/x (1)", primary_file="/lib/x (1)/book.epub")

		def fake_detect(_m):
			return Diagnosis(category="C1", reason="variant pair", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		def fake_extract(_m):
			return ExtractedMeta(
				title_from_text="Den zkázy",
				first_page_text="Den zkázy\nAnatolij Dněprov\nRomán o osudu lidstva, pokračování slavné sci-fi série.",
			)

		with patch.object(pmod, "detect_fn", fake_detect), patch.object(pmod, "safe_extract", fake_extract):
			stats = _empty_stats()
			result = _process_book(
				meta, enricher=None, skip_enrich=True, skip_verify=False,
				llm_provider=None, llm_categories=("ALL",), stats=stats,
			)
		assert result[3] is None
		assert stats.get("swap_fixed", 0) == 0 and stats["unfixed"] == 1
