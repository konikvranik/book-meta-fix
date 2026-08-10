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


def _extracted_with_text_title(real_title: str, *, isbn_from_text: str | None = None) -> ExtractedMeta:
	"""An ExtractedMeta whose embedded title is still broken but whose
	text-mined title (title_from_text) is the real one."""
	return ExtractedMeta(
		title="Broken_epub",  # embedded OPF still broken
		title_from_text=real_title,
		first_page_text=f"Neznámý {real_title.upper()} some body text",
		isbn_from_text=isbn_from_text,
		source_format="epub",
	)


def _stats() -> dict:
	return {
		"ok": 0, "needs_review": 0, "det_fixed": 0, "online_fixed": 0,
		"llm_fixed": 0, "llm_skipped_no_text": 0, "llm_no_result": 0, "llm_error": 0,
		"unfixed": 0, "errors": 0, "content_mismatch": 0,
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
		extracted = ExtractedMeta(title="Broken_epub", first_page_text="Neznámý JÁDRO GALAXIE")
		stats = _stats()

		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix import pipeline as pmod

		class StubEnricher:
			def lookup(self, *, isbn=None, title=None, author=None):
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
