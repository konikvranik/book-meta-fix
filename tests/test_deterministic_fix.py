"""Tests for the deterministic-fix pipeline ordering (offline → online → LLM).

Verifies the LLM is NOT called when the offline (text_meta) or online phase
already produced a usable proposal — the central goal of the cheap-first
refactor.
"""
from __future__ import annotations

from unittest.mock import patch

from book_meta_fix.extractors import ExtractedMeta
from book_meta_fix.models import BookMeta, Confidence, Diagnosis, Verdict
from book_meta_fix.pipeline import _process_book


def _book(title: str = "Broken_epub", isbn: str | None = None, year: int | None = None) -> BookMeta:
	"""A NEEDS_REVIEW book (C2 filename-as-title) with a primary_file set so
	extraction runs."""
	return BookMeta(
		calibre_id=1, title=title, authors=["Neznamy"],
		path="/lib/A/Broken_epub (1)", primary_file="/lib/A/Broken_epub (1)/book.epub",
		isbn=isbn, year=year,
	)


def _extracted_with_text_title(real_title: str, *, author: str = "Gregory Benford", isbn_from_text: str | None = None) -> ExtractedMeta:
	"""An ExtractedMeta whose embedded title is still broken but whose
	text-mined title (title_from_text) + author are the real ones — and both
	appear in the page text so the identity is content-verifiable."""
	return ExtractedMeta(
		title="Broken_epub",  # embedded OPF still broken
		title_from_text=real_title,
		authors_from_text=[author],
		first_page_text=f"{author.upper()} {real_title.upper()} some body text",
		isbn_from_text=isbn_from_text,
		source_format="epub",
	)


def _stats() -> dict:
	return {
		"ok": 0, "needs_review": 0, "det_fixed": 0, "online_fixed": 0,
		"llm_fixed": 0, "llm_skipped_no_text": 0, "llm_no_result": 0, "llm_error": 0,
		"unfixed": 0, "errors": 0, "content_mismatch": 0,
		"offline_content": 0, "offline_embedded": 0,
		"online_databazeknih": 0, "online_openlibrary": 0, "online_google_books": 0,
	}


class TestLlmNotCalledWhenOfflineSucceeds:
	def test_text_mined_title_skips_llm(self):
		"""When text_meta finds the real title, the LLM is never called."""
		meta = _book()
		extracted = _extracted_with_text_title("Jádro Galaxie")
		stats = _stats()

		# A fake LLM provider that records calls — it must not be invoked.
		llm_calls = {"n": 0}

		class RecordingProvider:
			name = "recording"

			def reconcile(self, evidence):  # noqa: ANN001
				llm_calls["n"] += 1
				return None

		from book_meta_fix import pipeline as pmod

		def fake_detect(m):
			return Diagnosis(category="C2", reason="filename as title", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		with patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "_safe_extract", lambda m: extracted):
			result = _process_book(
				meta, enricher=None, skip_enrich=True, skip_verify=True,
				llm_provider=RecordingProvider(), llm_categories=("ALL",), stats=stats,
			)
		_meta, diag, _verification, enriched = result
		# The text-mined title became the proposal (source=content).
		assert enriched is not None
		assert enriched.title == "Jádro Galaxie"
		assert enriched.source == "content"
		# LLM was not called.
		assert llm_calls["n"] == 0
		# Stats reflect the offline fix, not an LLM fix.
		assert stats["det_fixed"] == 1
		assert stats["llm_fixed"] == 0

	def test_online_lookup_skips_llm(self):
		"""When an online lookup (here: mocked databazeknih hit) returns a
		better title, the LLM is never called."""
		meta = _book()
		extracted = _extracted_with_text_title("Jádro Galaxie")
		stats = _stats()

		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix import pipeline as pmod

		class StubEnricher:
			def lookup(self, *, isbn=None, title=None, author=None, year=None):
				if title:  # title-based lookup (Phase C)
					return EnrichedMeta(title="Jádro Galaxie", authors=["Gregory Benford"], source="databazeknih")
				return None

		llm_calls = {"n": 0}

		class RecordingProvider:
			name = "recording"

			def reconcile(self, evidence):  # noqa: ANN001
				llm_calls["n"] += 1
				return None

		def fake_detect(m):
			return Diagnosis(category="C2", reason="filename as title", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		with patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "_safe_extract", lambda m: extracted):
			result = _process_book(
				meta, enricher=StubEnricher(), skip_enrich=False, skip_verify=True,
				llm_provider=RecordingProvider(), llm_categories=("ALL",), stats=stats,
			)
		_meta, diag, _verification, enriched = result
		assert enriched is not None
		assert enriched.source == "databazeknih"
		assert enriched.title == "Jádro Galaxie"
		assert llm_calls["n"] == 0
		assert stats["online_fixed"] == 1


class TestLlmCalledWhenNothingElseWorks:
	def test_llm_called_when_offline_and_online_miss(self):
		"""When neither offline extraction nor online lookup finds anything,
		the LLM fallback is reached."""
		meta = _book()
		# No text-mined fields, no ISBN, unparseable page text.
		extracted = ExtractedMeta(title="Broken_epub", first_page_text="garbage " * 50)
		stats = _stats()

		from book_meta_fix import pipeline as pmod

		class StubEnricher:
			def lookup(self, **kw):
				return None

		llm_calls = {"n": 0}

		class StubProvider:
			name = "stub"

			def reconcile(self, evidence):  # noqa: ANN001
				llm_calls["n"] += 1
				from book_meta_fix.llm import ReconciledMeta

				return ReconciledMeta(title="LLM Guess", authors=["X"], confidence="medium")

		def fake_detect(m):
			return Diagnosis(category="C2", reason="filename as title", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		with patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "_safe_extract", lambda m: extracted), \
			 patch.object(pmod, "_has_usable_text", lambda t: True):
			result = _process_book(
				meta, enricher=StubEnricher(), skip_enrich=False, skip_verify=True,
				llm_provider=StubProvider(), llm_categories=("ALL",), stats=stats,
			)
		_meta, diag, _verification, enriched = result
		assert llm_calls["n"] == 1
		assert enriched is not None
		assert enriched.title == "LLM Guess"
		assert stats["llm_fixed"] == 1


class TestStatsSourceBreakdown:
	"""The stats dict carries a per-source breakdown (offline/online/llm) that
	the CLI renders as a summary table. These tests pin the exact keys used."""

	def test_offline_content_fix_buckets_into_offline_content(self):
		"""A text-mined fix (source=content) increments det_fixed AND
		offline_content."""
		meta = _book()
		extracted = _extracted_with_text_title("Jádro Galaxie")
		stats = _stats()
		from book_meta_fix import pipeline as pmod

		def fake_detect(m):
			return Diagnosis(category="C2", reason="x", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		with patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "_safe_extract", lambda m: extracted):
			_process_book(
				meta, enricher=None, skip_enrich=True, skip_verify=True,
				llm_provider=None, llm_categories=(), stats=stats,
			)
		assert stats["det_fixed"] == 1
		assert stats["offline_content"] == 1
		assert stats["offline_embedded"] == 0
		assert stats["online_fixed"] == 0

	def test_online_databazeknih_buckets_into_online_databazeknih(self):
		"""A databazeknih hit increments online_fixed AND online_databazeknih."""
		meta = _book()
		extracted = _extracted_with_text_title("Jádro Galaxie")
		stats = _stats()
		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix import pipeline as pmod

		class StubEnricher:
			def lookup(self, *, isbn=None, title=None, author=None, year=None):
				if title:
					return EnrichedMeta(title="Jádro Galaxie", source="databazeknih")
				return None

		def fake_detect(m):
			return Diagnosis(category="C2", reason="x", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		with patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "_safe_extract", lambda m: extracted):
			_process_book(
				meta, enricher=StubEnricher(), skip_enrich=False, skip_verify=True,
				llm_provider=None, llm_categories=(), stats=stats,
			)
		assert stats["online_fixed"] == 1
		assert stats["online_databazeknih"] == 1
		assert stats["det_fixed"] == 0

	def test_unknown_online_source_does_not_crash(self):
		"""An online source we don't have a dedicated counter for (e.g. a
		hypothetical 'openlibrary') still increments online_fixed and creates
		an online_<source> key rather than raising."""
		meta = _book()
		extracted = _extracted_with_text_title("Jádro Galaxie")
		stats = _stats()
		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix import pipeline as pmod

		class StubEnricher:
			def lookup(self, *, isbn=None, title=None, author=None, year=None):
				if title:
					return EnrichedMeta(title="Jádro Galaxie", source="openlibrary")
				return None

		def fake_detect(m):
			return Diagnosis(category="C2", reason="x", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		with patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "_safe_extract", lambda m: extracted):
			_process_book(
				meta, enricher=StubEnricher(), skip_enrich=False, skip_verify=True,
				llm_provider=None, llm_categories=(), stats=stats,
			)
		assert stats["online_fixed"] == 1
		assert stats["online_openlibrary"] == 1

	def test_run_pipeline_seeds_all_stats_keys(self, tmp_path):
		"""run_pipeline, when passed a stats dict, seeds the full key set so the
		CLI summary table can read any key without a KeyError — even when no
		books were processed (empty library)."""
		from book_meta_fix import pipeline as pmod
		from book_meta_fix.pipeline import run_pipeline

		with patch.object(pmod, "scan_library", lambda lib, cache=None: []):
			stats: dict = {}
			run_pipeline(tmp_path, cache=None, workers=1, stats=stats)

		# The keys the CLI summary table reads must all be present.
		for key in (
			"ok", "needs_review", "det_fixed", "online_fixed",
			"llm_flash_fixed", "llm_final_fixed", "llm_low_confidence",
			"llm_skipped_no_text", "llm_no_result", "llm_error",
			"unfixed", "errors", "content_mismatch",
			"covers_generated", "covers_missing",
			"online_databazeknih", "online_openlibrary", "online_google_books",
			"offline_content", "offline_embedded", "total",
		):
			assert key in stats, f"stats dict missing key {key!r}"
		assert stats["total"] == 0


class TestAcquireIdentity:
	"""_acquire_identity: content-verified identity cascade (no network)."""

	def test_content_isbn_wins(self):
		from book_meta_fix.pipeline import _acquire_identity

		meta = _book(isbn="9788072072323")
		ext = ExtractedMeta(title="Broken_epub", isbn_from_text="9788072072323")
		ident = _acquire_identity(meta, ext)
		assert ident is not None and ident.has_isbn
		assert ident.source == "content-isbn"

	def test_metadata_isbn_verified_against_content(self):
		from book_meta_fix.pipeline import _acquire_identity

		meta = _book(isbn="978-80-720-7232-3")
		# ISBN appears in the page text but no content_isbn field set.
		ext = ExtractedMeta(title="Broken_epub", first_page_text="ISBN 9788072072323 here")
		ident = _acquire_identity(meta, ext)
		assert ident is not None and ident.has_isbn
		assert ident.source == "metadata"

	def test_extractor_title_author_for_c2_book(self):
		"""A C2 book (broken title) whose metadata identity isn't in the text
		falls to the offline extractor level — title+author mined from the page
		text and present there."""
		from book_meta_fix.pipeline import _acquire_identity

		meta = _book()  # title "Broken_epub", author "Neznamy" — not in text
		ext = _extracted_with_text_title("Jádro Galaxie")
		ident = _acquire_identity(meta, ext)
		assert ident is not None and ident.has_title_author
		assert ident.title == "Jádro Galaxie"
		assert ident.source == "extractor"

	def test_no_verifiable_identity_returns_none(self):
		from book_meta_fix.pipeline import _acquire_identity

		meta = _book()
		# Garbage text, no ISBN, no extracted title/author.
		ext = ExtractedMeta(title="Broken_epub", first_page_text="random noise " * 20)
		assert _acquire_identity(meta, ext) is None

	def test_no_extracted_returns_none(self):
		from book_meta_fix.pipeline import _acquire_identity

		assert _acquire_identity(_book(), None) is None


class TestOnlineFill:
	"""_online_fill: anchored lookup with false-positive filtering."""

	def _identity_title(self):
		from book_meta_fix.pipeline import IdentityResult

		return IdentityResult(title="Jádro Galaxie", authors=["Gregory Benford"], source="extractor")

	def test_isbn_identity_uses_isbn_lookup(self):
		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix.pipeline import IdentityResult, _online_fill

		seen = {}

		class Stub:
			def lookup(self, *, isbn=None, title=None, author=None, year=None):
				seen["isbn"] = isbn
				return EnrichedMeta(title="X", source="databazeknih", isbn=isbn)

		ident = IdentityResult(isbn="9788072072323", source="content-isbn")
		em = _online_fill(ident, Stub(), skip_enrich=False)
		assert em is not None and seen["isbn"] == "9788072072323"

	def test_title_identity_rejects_author_mismatch(self):
		"""A title lookup that returns a DIFFERENT author is rejected — false
		possession prevention (same title, different book)."""
		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix.pipeline import _online_fill

		class Stub:
			def lookup(self, *, isbn=None, title=None, author=None, year=None):
				return EnrichedMeta(title="Jádro Galaxie", authors=["Někdo Úplně Jiný"], source="databazeknih")

		assert _online_fill(self._identity_title(), Stub(), skip_enrich=False) is None

	def test_title_identity_accepts_author_match(self):
		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix.pipeline import _online_fill

		class Stub:
			def lookup(self, *, isbn=None, title=None, author=None, year=None):
				return EnrichedMeta(title="Jádro Galaxie", authors=["Gregory Benford"], source="databazeknih")

		em = _online_fill(self._identity_title(), Stub(), skip_enrich=False)
		assert em is not None and em.source == "databazeknih"

	def test_skip_enrich_returns_none(self):
		from book_meta_fix.pipeline import IdentityResult, _online_fill

		ident = IdentityResult(isbn="9788072072323", source="content-isbn")
		assert _online_fill(ident, None, skip_enrich=True) is None


class TestLlmBroaderRetry:
	"""When the first-page LLM attempt fails, the pipeline retries with the
	broader text window (title/author aren't always on page 1)."""

	def test_retries_with_broader_when_first_page_fails(self):
		from book_meta_fix.llm import ReconciledMeta

		meta = _book()  # C2 broken title
		# first_page: usable text but no identity; broader: has the real title.
		extracted = ExtractedMeta(
			title="Broken_epub",
			first_page_text="Obsah kapitoly text text text " * 8,
			broader_text="Gregory Benford JÁDRO GALAXIE úvodní kapitola " * 30,
		)
		stats = _stats()
		calls: list[int] = []

		class RetryProvider:
			name = "retry"

			def reconcile(self, evidence):  # noqa: ANN001
				t = evidence.get("first_page_text") or ""
				calls.append(len(t))
				if "JÁDRO GALAXIE" in t.upper():
					return ReconciledMeta(title="Jádro Galaxie", authors=["Gregory Benford"], confidence="high")
				return None

		from book_meta_fix import pipeline as pmod

		def fake_detect(m):
			return Diagnosis(category="C2", reason="x", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		with patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "_safe_extract", lambda m: extracted):
			result = _process_book(
				meta, enricher=None, skip_enrich=True, skip_verify=True,
				llm_provider=RetryProvider(), llm_categories=("ALL",), stats=stats,
			)
		_meta, diag, _ver, enriched = result
		# The LLM was called twice: first-page (shorter), then broader (longer).
		assert len(calls) == 2
		assert calls[0] < calls[1]
		# The broader attempt succeeded and is the adopted proposal.
		assert enriched is not None
		assert enriched.title == "Jádro Galaxie"
		assert stats.get("llm_broader_fixed", 0) == 1

	def test_no_retry_without_broader_text(self):
		"""If no broader window is available, the first-page failure is final."""
		meta = _book()
		extracted = ExtractedMeta(title="Broken_epub", first_page_text="Obsah kapitoly " * 12)
		stats = _stats()
		calls = []

		class OnceProvider:
			name = "once"

			def reconcile(self, evidence):  # noqa: ANN001
				calls.append(1)
				return None

		from book_meta_fix import pipeline as pmod

		def fake_detect(m):
			return Diagnosis(category="C2", reason="x", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		with patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "_safe_extract", lambda m: extracted):
			_process_book(
				meta, enricher=None, skip_enrich=True, skip_verify=True,
				llm_provider=OnceProvider(), llm_categories=("ALL",), stats=stats,
			)
		assert len(calls) == 1  # no broader → no retry
		assert stats["llm_no_result"] == 1
