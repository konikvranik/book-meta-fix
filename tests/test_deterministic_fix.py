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
			 patch.object(pmod, "safe_extract", lambda m: extracted):
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

		from book_meta_fix import pipeline as pmod
		from book_meta_fix.enrichers import EnrichedMeta

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
			 patch.object(pmod, "safe_extract", lambda m: extracted):
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
			 patch.object(pmod, "safe_extract", lambda m: extracted), \
			 patch.object(pmod, "has_usable_text", lambda t: True):
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
			 patch.object(pmod, "safe_extract", lambda m: extracted):
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
		from book_meta_fix import pipeline as pmod
		from book_meta_fix.enrichers import EnrichedMeta

		class StubEnricher:
			def lookup(self, *, isbn=None, title=None, author=None, year=None):
				if title:
					return EnrichedMeta(title="Jádro Galaxie", source="databazeknih")
				return None

		def fake_detect(m):
			return Diagnosis(category="C2", reason="x", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		with patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "safe_extract", lambda m: extracted):
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
		from book_meta_fix import pipeline as pmod
		from book_meta_fix.enrichers import EnrichedMeta

		class StubEnricher:
			def lookup(self, *, isbn=None, title=None, author=None, year=None):
				if title:
					return EnrichedMeta(title="Jádro Galaxie", source="openlibrary")
				return None

		def fake_detect(m):
			return Diagnosis(category="C2", reason="x", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		with patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "safe_extract", lambda m: extracted):
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

		with patch.object(pmod, "scan_library", lambda lib, cache=None, progress_callback=None, workers=8: []):
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


class TestLookupKey:
	"""_lookup_key: the ONLINE QUERY key built from the record without any
	content verification (the content check moved AFTER the enrichment)."""

	def test_valid_record_isbn_wins(self):
		from book_meta_fix.pipeline import _lookup_key

		meta = _book(isbn="978-80-7191-176-0")
		key = _lookup_key(meta, ExtractedMeta(title="x"))
		assert key is not None and key.has_isbn
		assert key.isbn == "9788071911760"

	def test_invalid_isbn_falls_through_to_title(self):
		from book_meta_fix.pipeline import _lookup_key

		meta = _book(isbn="978-0-0000-0000-0")  # wrong check digit
		key = _lookup_key(meta, ExtractedMeta(title="x"))
		# "Broken_epub" is a broken title and no mined fields exist -> no key.
		assert key is None

	def test_clean_title_authorless_record_gives_title_only_key(self):
		"""The C9/anonym shape: a clean title with NO usable author is a valid
		key now — the author is exactly what the lookup should recover."""
		from book_meta_fix.pipeline import _lookup_key

		meta = BookMeta(calibre_id=1, title="Ocelová krysa prezidentem", authors=[],
			path="/lib/A/B", primary_file="/lib/A/B/book.epub")
		key = _lookup_key(meta, ExtractedMeta(title="x"))
		assert key is not None
		assert key.title == "Ocelová krysa prezidentem"
		assert key.authors == []

	def test_broken_author_value_dropped_from_key(self):
		"""'Neznámý'/anonym authors must not ride along on the query — they
		pollute the search and would trip the author-match filter against the
		recovered author."""
		from book_meta_fix.pipeline import _lookup_key

		meta = BookMeta(calibre_id=1, title="Ocelová krysa prezidentem", authors=["Neznámý"],
			path="/lib/A/B", primary_file="/lib/A/B/book.epub")
		key = _lookup_key(meta, ExtractedMeta(title="x"))
		assert key is not None and key.authors == []

	def test_broken_record_title_falls_to_mined_identity(self):
		from book_meta_fix.pipeline import _lookup_key

		meta = _book()  # title "Broken_epub"
		ext = _extracted_with_text_title("Jádro Galaxie")
		key = _lookup_key(meta, ext)
		assert key is not None
		assert key.title == "Jádro Galaxie"
		assert key.authors == ["Gregory Benford"]


class TestSeriesLookupKey:
	"""_series_lookup_key: (name, index) when both halves of a series entry
	survive — the fallback lookup key for records without a usable title."""

	def test_string_entry_with_index(self):
		from book_meta_fix.pipeline import _series_lookup_key

		meta = BookMeta(calibre_id=1, title="x", authors=["A"], series=["Ocelová krysa #5"],
			path="/lib/A/B", primary_file="/lib/A/B/book.epub")
		assert _series_lookup_key(meta) == ("Ocelová krysa", "5")

	def test_dict_entry_with_index(self):
		from book_meta_fix.pipeline import _series_lookup_key

		meta = BookMeta(calibre_id=1, title="x", authors=["A"], series=[{"name": "Agent JFK", "index": 3}],
			path="/lib/A/B", primary_file="/lib/A/B/book.epub")
		assert _series_lookup_key(meta) == ("Agent JFK", "3")

	def test_name_only_entry_returns_none(self):
		from book_meta_fix.pipeline import _series_lookup_key

		meta = BookMeta(calibre_id=1, title="x", authors=["A"], series=["Ocelová krysa"],
			path="/lib/A/B", primary_file="/lib/A/B/book.epub")
		assert _series_lookup_key(meta) is None

	def test_no_series_returns_none(self):
		from book_meta_fix.pipeline import _series_lookup_key

		meta = _book()
		assert _series_lookup_key(meta) is None


class TestOnlineFillSeriesAndTitleOnly:
	"""_online_fill: title-only keys (when allowed) and the series fallback."""

	def test_title_only_key_queried_when_allowed(self):
		"""The anonym shape: a title WITHOUT an author queries with author=None
		and accepts the hit (nothing to author-filter against)."""
		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix.pipeline import IdentityResult, _online_fill

		seen = {}

		class Stub:
			def lookup(self, *, isbn=None, title=None, author=None, year=None):
				seen["author"] = author
				return EnrichedMeta(title="Ocelová krysa prezidentem", authors=["Harry Harrison"], source="databazeknih")

		key = IdentityResult(title="Ocelová krysa prezidentem", authors=[], source="metadata")
		em, axis = _online_fill(key, Stub(), skip_enrich=False, allow_title_only=True)
		assert em is not None and em.authors == ["Harry Harrison"]
		assert axis == "title"
		assert seen["author"] is None

	def test_title_only_key_refused_without_flag(self):
		"""The LLM path (no allow_title_only) keeps the old title+author
		requirement — a title-only LLM answer does not query online."""
		from book_meta_fix.pipeline import IdentityResult, _online_fill

		calls = {"n": 0}

		class Stub:
			def lookup(self, **kw):
				calls["n"] += 1
				return None

		key = IdentityResult(title="X", authors=[], source="metadata")
		assert _online_fill(key, Stub(), skip_enrich=False) == (None, None)
		assert calls["n"] == 0

	def test_series_fallback_when_no_title_key(self):
		"""A record with no usable title but a surviving series entry queries
		by (series, volume)."""
		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix.pipeline import _online_fill

		seen = {}

		class Stub:
			def lookup(self, **kw):
				return None

			def lookup_series(self, *, series, index):
				seen["series"] = (series, index)
				return EnrichedMeta(title="Ocelová krysa prezidentem", authors=["Harry Harrison"], source="databazeknih")

		em, axis = _online_fill(None, Stub(), skip_enrich=False, series=("Ocelová krysa", "5"))
		assert em is not None and em.authors == ["Harry Harrison"]
		assert axis == "series"
		assert seen["series"] == ("Ocelová krysa", "5")

	def test_series_fallback_when_title_lookup_misses(self):
		"""A broken-title record whose series survived: the mined/text query
		found nothing online, the series key gets its chance."""
		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix.pipeline import IdentityResult, _online_fill

		class Stub:
			def lookup(self, **kw):
				return None

			def lookup_series(self, *, series, index):
				return EnrichedMeta(title="Návrat ocelové krysy", authors=["Harry Harrison"], source="databazeknih")

		key = IdentityResult(title="Jádro Galaxie", authors=["Gregory Benford"], source="extractor")
		em, axis = _online_fill(key, Stub(), skip_enrich=False, series=("Ocelová krysa", "11"))
		assert em is not None and em.title == "Návrat ocelové krysy"
		assert axis == "series"

	def test_enricher_without_lookup_series_is_safe(self):
		"""Stub/test enrichers that predate lookup_series must not crash the
		series fallback."""
		from book_meta_fix.pipeline import _online_fill

		class Stub:
			def lookup(self, **kw):
				return None

		assert _online_fill(None, Stub(), skip_enrich=False, series=("X", "1")) == (None, None)

	def test_axes_chain_isbn_miss_then_title(self):
		"""Gather what you can: an ISBN miss falls through to the title query
		instead of giving up (the key carries both axes)."""
		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix.pipeline import IdentityResult, _online_fill

		calls = []

		class Stub:
			def lookup(self, *, isbn=None, title=None, author=None, year=None):
				calls.append(("isbn" if isbn else "title", title))
				if isbn:
					return None  # ISBN unknown online
				return EnrichedMeta(title=title, source="databazeknih")

		key = IdentityResult(isbn="9788071911760", title="Ocelová krysa prezidentem", authors=[], source="metadata")
		em, axis = _online_fill(key, Stub(), skip_enrich=False, allow_title_only=True)
		assert em is not None and axis == "title"
		assert [c[0] for c in calls] == ["isbn", "title"]


class TestVerifyEnrichmentOnline:
	"""_verify_enrichment_online: the ONLINE tier of the post-enrichment
	verification — online record base corroborates the answer per the axis
	it rode in on; the local content check is the fallback elsewhere."""

	@staticmethod
	def _author_exists_stub(exists: bool):
		class Stub:
			def author_exists(self, name):
				return exists
		return Stub()

	def test_title_with_author_key_confirms(self):
		"""The source matched BOTH halves of the query (and the author filter
		passed in _online_fill) — the online base confirms the claim."""
		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix.pipeline import IdentityResult, _verify_enrichment_online

		key = IdentityResult(title="Jádro Galaxie", authors=["Gregory Benford"], source="metadata")
		online = EnrichedMeta(title="Jádro Galaxie", authors=["Gregory Benford"], source="databazeknih")
		assert _verify_enrichment_online(online, key, None, "title", None) is True

	def test_title_only_near_exact_with_known_author_confirms(self):
		"""The anonym shape: near-exact title agreement (>= 90) + the
		RECOVERED author exists online."""
		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix.pipeline import IdentityResult, _verify_enrichment_online

		key = IdentityResult(title="Ocelová krysa prezidentem", authors=[], source="metadata")
		online = EnrichedMeta(title="Ocelová krysa prezidentem", authors=["Harry Harrison"], source="databazeknih")
		assert _verify_enrichment_online(online, key, None, "title", self._author_exists_stub(True)) is True

	def test_title_only_similar_title_does_not_confirm(self):
		"""A merely similar title (the search floor of 70 admits similar-
		titled different books) is NOT online verification."""
		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix.pipeline import IdentityResult, _verify_enrichment_online

		key = IdentityResult(title="Ocelová krysa", authors=[], source="metadata")
		online = EnrichedMeta(title="Ocelová krysa se mstí", authors=["Harry Harrison"], source="databazeknih")
		assert _verify_enrichment_online(online, key, None, "title", self._author_exists_stub(True)) is False

	def test_title_only_unknown_author_does_not_confirm(self):
		"""The recovered author is not in the online base (hallucination
		filter) — local fallback decides."""
		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix.pipeline import IdentityResult, _verify_enrichment_online

		key = IdentityResult(title="Ocelová krysa prezidentem", authors=[], source="metadata")
		online = EnrichedMeta(title="Ocelová krysa prezidentem", authors=["Někdo Neexistující"], source="databazeknih")
		assert _verify_enrichment_online(online, key, None, "title", self._author_exists_stub(False)) is False

	def test_title_only_stub_without_author_exists_cannot_confirm(self):
		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix.pipeline import IdentityResult, _verify_enrichment_online

		key = IdentityResult(title="Ocelová krysa prezidentem", authors=[], source="metadata")
		online = EnrichedMeta(title="Ocelová krysa prezidentem", authors=["Harry Harrison"], source="databazeknih")

		class NoLadder:
			pass

		assert _verify_enrichment_online(online, key, None, "title", NoLadder()) is False

	def test_isbn_axis_cross_checks_other_fields(self):
		"""The ISBN may itself be the corrupt half: the answer must agree
		with the key's title (>= 70) or author (>= 80); a bare ISBN key with
		nothing to compare stays for the local fallback."""
		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix.pipeline import IdentityResult, _verify_enrichment_online

		online = EnrichedMeta(title="Ocelová krysa prezidentem", authors=["Harry Harrison"], source="databazeknih")
		key_agree = IdentityResult(isbn="9788071911760", title="Ocelová krysa prezidentem", authors=[], source="metadata")
		assert _verify_enrichment_online(online, key_agree, None, "isbn", None) is True
		author_agree = IdentityResult(isbn="9788071911760", title=None, authors=["Harry Harrison"], source="metadata")
		assert _verify_enrichment_online(online, author_agree, None, "isbn", None) is True
		key_disagree = IdentityResult(isbn="9788071911760", title="Jádro Galaxie", authors=[], source="metadata")
		assert _verify_enrichment_online(online, key_disagree, None, "isbn", None) is False
		key_bare = IdentityResult(isbn="9788071911760", source="metadata")
		assert _verify_enrichment_online(online, key_bare, None, "isbn", None) is False

	def test_series_axis_needs_series_and_author_agreement(self):
		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix.pipeline import _verify_enrichment_online

		online = EnrichedMeta(title="Ocelová krysa prezidentem", authors=["Harry Harrison"], series="Ocelová krysa", source="databazeknih")
		assert _verify_enrichment_online(online, None, ("Ocelová krysa", "5"), "series", self._author_exists_stub(True)) is True
		# Answer's series disagrees with the queried series.
		assert _verify_enrichment_online(online, None, ("Agent JFK", "5"), "series", self._author_exists_stub(True)) is False
		# No series box on the detail page.
		online_noseries = EnrichedMeta(title="Ocelová krysa prezidentem", authors=["Harry Harrison"], source="databazeknih")
		assert _verify_enrichment_online(online_noseries, None, ("Ocelová krysa", "5"), "series", self._author_exists_stub(True)) is False
		# Author not in the online base.
		assert _verify_enrichment_online(online, None, ("Ocelová krysa", "5"), "series", self._author_exists_stub(False)) is False


class TestVerifyAfterEnrichment:
	"""Verification runs AFTER the online enrichment, on the ANSWER: ONLINE
	first (the record base corroborates title/author/series per the query
	axis), then the LOCAL-DB fallback (the recovered author/series known to
	the library), and the TEXT check keeps its confirming role
	(confirm_identity). Unverified hits stay proposals for review."""

	@staticmethod
	def _rat_book() -> BookMeta:
		"""The real 'Ocelová krysa' shape: clean title, author gone, series
		entry survived, no ISBN."""
		return BookMeta(
			calibre_id=9, title="Ocelová krysa prezidentem", authors=[],
			path="/lib/Anonym/Ocelová krysa prezidentem (noid)",
			primary_file="/lib/Anonym/Ocelová krysa prezidentem (noid)/b.epub",
			series=["Ocelová krysa #5"],
		)

	@staticmethod
	def _harrison_enricher():
		from book_meta_fix.enrichers import EnrichedMeta

		class StubEnricher:
			def lookup(self, *, isbn=None, title=None, author=None, year=None):
				if title and "prezidentem" in title.lower():
					return EnrichedMeta(
						title="Ocelová krysa prezidentem", authors=["Harry Harrison"],
						isbn="9788071911760", source="databazeknih",
					)
				return None

		return StubEnricher()

	def test_unconfirmed_hit_not_identity_confirmed(self):
		"""The txt-conversion shape: the annotation text names neither the
		author nor the title AND the enricher cannot verify online (no
		author_exists ladder) — the hit lands as a proposal but NOT
		identity_confirmed (no accept pre-fill)."""
		meta = self._rat_book()
		extracted = ExtractedMeta(title="x", first_page_text="Prohnaný Jim diGriz je již usedlejší pán. " * 10)
		stats = _stats()

		from book_meta_fix import pipeline as pmod

		def fake_detect(m):
			return Diagnosis(category="C9", reason="author lost", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		with patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "safe_extract", lambda m: extracted):
			_meta, diag, _ver, enriched = _process_book(
				meta, enricher=self._harrison_enricher(), skip_enrich=False, skip_verify=True,
				llm_provider=None, llm_categories=("ALL",), stats=stats,
			)
		assert enriched is not None and enriched.source == "databazeknih"
		assert enriched.authors == ["Harry Harrison"]
		assert enriched.identity_confirmed is False
		assert stats["online_fixed"] == 1 and stats["online_databazeknih"] == 1

	def test_confirmed_via_online_ladder_when_content_silent(self):
		"""The same txt-conversion shape, but the online tier CAN verify: the
		answer's title is a near-exact match of the query and the recovered
		author exists in the online base (the LLM ladder's cached check) —
		identity_confirmed even though the book's own text names nobody."""
		from book_meta_fix.enrichers import EnrichedMeta

		meta = self._rat_book()
		extracted = ExtractedMeta(title="x", first_page_text="Prohnaný Jim diGriz je již usedlejší pán. " * 10)
		stats = _stats()

		class LadderEnricher:
			def lookup(self, *, isbn=None, title=None, author=None, year=None):
				if title and "prezidentem" in title.lower():
					return EnrichedMeta(
						title="Ocelová krysa prezidentem", authors=["Harry Harrison"],
						isbn="9788071911760", source="databazeknih",
					)
				return None

			def author_exists(self, name):
				return name == "Harry Harrison"

		from book_meta_fix import pipeline as pmod

		def fake_detect(m):
			return Diagnosis(category="C9", reason="author lost", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		with patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "safe_extract", lambda m: extracted):
			_meta, diag, _ver, enriched = _process_book(
				meta, enricher=LadderEnricher(), skip_enrich=False, skip_verify=True,
				llm_provider=None, llm_categories=("ALL",), stats=stats,
			)
		assert enriched is not None
		assert enriched.identity_confirmed is True

	def test_confirmed_when_content_agrees(self):
		"""Title page present: the online answer is confirmed against the
		content and carries identity_confirmed (auto-accept tier)."""
		meta = self._rat_book()
		extracted = ExtractedMeta(
			title="x",
			first_page_text="HARRY HARRISON: OCELOVÁ KRYSA PREZIDENTEM — anotace a úvodní kapitola. " * 3,
		)
		stats = _stats()

		from book_meta_fix import pipeline as pmod

		def fake_detect(m):
			return Diagnosis(category="C9", reason="author lost", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		with patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "safe_extract", lambda m: extracted):
			_meta, diag, _ver, enriched = _process_book(
				meta, enricher=self._harrison_enricher(), skip_enrich=False, skip_verify=True,
				llm_provider=None, llm_categories=("ALL",), stats=stats,
			)
		assert enriched is not None and enriched.identity_confirmed is True

	def test_offline_content_proposal_keeps_binding_contract(self):
		"""The offline fallback's identity_confirmed keeps the OLD
		acquire_identity contract (the record/mined identity bound to the
		text) instead of riding the removed pre-lookup gate: a mined title
		whose pair is verifiable in the text is confirmed..."""
		from book_meta_fix.detectors import Diagnosis as _D
		from book_meta_fix.pipeline import _try_deterministic_fix

		meta = self._rat_book()  # no author; text has neither title nor author
		extracted = ExtractedMeta(
			title="x",
			title_from_text="Dorskyho Internátní Škola",  # junk-mined
			authors_from_text=["Jim diGriz"],  # a character, not the author
			first_page_text="Dorskyho Internátní Škola Jim diGriz " * 10,
		)

		class NoHit:
			def lookup(self, **kw):
				return None

		diag = _D(category="C9", reason="author lost", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)
		proposal = _try_deterministic_fix(meta, diag, extracted, NoHit(), skip_enrich=False)
		# The mined pair IS present in the text (acquire_identity's extractor
		# tier binds it), so the proposal carries the same confirmation the
		# old pre-lookup gate granted.
		assert proposal is not None
		assert proposal.identity_confirmed is True

	def test_offline_content_proposal_unbound_stays_unconfirmed(self):
		"""...while a record with NO verifiable identity in the text (broken
		title, no mined author) yields an UNCONFIRMED offline proposal."""
		from book_meta_fix.detectors import Diagnosis as _D
		from book_meta_fix.pipeline import _try_deterministic_fix

		meta = BookMeta(
			calibre_id=9, title="Ocelov_ krysa-05_txt", authors=[],
			path="/lib/Anonym/B (noid)", primary_file="/lib/Anonym/B (noid)/b.epub",
		)
		extracted = ExtractedMeta(
			title="x",
			title_from_text="Ocelová krysa prezidentem",  # mined, no author mined
			authors_from_text=[],
			first_page_text="Ocelová krysa prezidentem " * 10,
		)

		class NoHit:
			def lookup(self, **kw):
				return None

		diag = _D(category="C2", reason="filename as title", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)
		proposal = _try_deterministic_fix(meta, diag, extracted, NoHit(), skip_enrich=False)
		# Mined title proposed (record title is broken); acquire_identity
		# needs BOTH halves -> no binding -> unconfirmed.
		assert proposal is not None
		assert proposal.title == "Ocelová krysa prezidentem"
		assert proposal.identity_confirmed is False


class TestAcquireIdentity:
	"""acquire_identity: content-verified identity cascade (no network)."""

	def test_content_isbn_wins(self):
		from book_meta_fix.pipeline import acquire_identity

		meta = _book(isbn="9788072072323")
		ext = ExtractedMeta(title="Broken_epub", isbn_from_text="9788072072323")
		ident = acquire_identity(meta, ext)
		assert ident is not None and ident.has_isbn
		assert ident.source == "content-isbn"

	def test_metadata_isbn_verified_against_content(self):
		from book_meta_fix.pipeline import acquire_identity

		meta = _book(isbn="978-80-720-7232-3")
		# ISBN appears in the page text but no content_isbn field set.
		ext = ExtractedMeta(title="Broken_epub", first_page_text="ISBN 9788072072323 here")
		ident = acquire_identity(meta, ext)
		assert ident is not None and ident.has_isbn
		assert ident.source == "metadata"

	def test_extractor_title_author_for_c2_book(self):
		"""A C2 book (broken title) whose metadata identity isn't in the text
		falls to the offline extractor level — title+author mined from the page
		text and present there."""
		from book_meta_fix.pipeline import acquire_identity

		meta = _book()  # title "Broken_epub", author "Neznamy" — not in text
		ext = _extracted_with_text_title("Jádro Galaxie")
		ident = acquire_identity(meta, ext)
		assert ident is not None and ident.has_title_author
		assert ident.title == "Jádro Galaxie"
		assert ident.source == "extractor"

	def test_no_verifiable_identity_returns_none(self):
		from book_meta_fix.pipeline import acquire_identity

		meta = _book()
		# Garbage text, no ISBN, no extracted title/author.
		ext = ExtractedMeta(title="Broken_epub", first_page_text="random noise " * 20)
		assert acquire_identity(meta, ext) is None

	def test_no_extracted_returns_none(self):
		from book_meta_fix.pipeline import acquire_identity

		assert acquire_identity(_book(), None) is None

	def test_identity_deep_in_broader_window_confirms(self):
		"""Title+author sit past the 4000-char search cap (title page a few
		pages in). Real extractor output is prefix-aligned — broader_text
		starts where first_page_text starts — so only a whole-text search of
		the broader window can find them; a capped search just re-sees page 1
		(and acquire_identity then disagreed with confirm_identity, which
		already searched the windows)."""
		from book_meta_fix.pipeline import acquire_identity

		meta = BookMeta(calibre_id=1, title="Zastavený příval", authors=["Eduard Štorch"],
			path="/lib/A/B", primary_file="/lib/A/B/book.epub")
		pad = "Obyčejný text úplně jiné kapitoly románu. " * 150  # ~6k chars, no identity
		broader = pad + " EDUARD ŠTORCH ZASTAVENÝ PŘÍVAL "
		ext = ExtractedMeta(title="Zastavený příval", first_page_text=broader[:5000], broader_text=broader)
		ident = acquire_identity(meta, ext)
		assert ident is not None and ident.source == "metadata"
		assert ident.title == "Zastavený příval"

	def test_deep_title_without_author_stays_unconfirmed(self):
		"""A deep-only title hit with the author nowhere in the text is a
		chapter-heading-shaped false positive — the author requirement must
		still reject it under the whole-window search."""
		from book_meta_fix.pipeline import acquire_identity

		meta = BookMeta(calibre_id=1, title="Prolog", authors=["Eduard Štorch"],
			path="/lib/A/B", primary_file="/lib/A/B/book.epub")
		pad = "Kapitola první obyčejný běžný text vyprávění. " * 150
		broader = pad + " Prolog " + pad
		ext = ExtractedMeta(title="Prolog", first_page_text=broader[:5000], broader_text=broader)
		assert acquire_identity(meta, ext) is None


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
		em, axis = _online_fill(ident, Stub(), skip_enrich=False)
		assert em is not None and seen["isbn"] == "9788072072323"
		assert axis == "isbn"

	def test_title_identity_rejects_author_mismatch(self):
		"""A title lookup that returns a DIFFERENT author is rejected — false
		possession prevention (same title, different book)."""
		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix.pipeline import _online_fill

		class Stub:
			def lookup(self, *, isbn=None, title=None, author=None, year=None):
				return EnrichedMeta(title="Jádro Galaxie", authors=["Někdo Úplně Jiný"], source="databazeknih")

		assert _online_fill(self._identity_title(), Stub(), skip_enrich=False) == (None, None)

	def test_title_identity_accepts_author_match(self):
		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix.pipeline import _online_fill

		class Stub:
			def lookup(self, *, isbn=None, title=None, author=None, year=None):
				return EnrichedMeta(title="Jádro Galaxie", authors=["Gregory Benford"], source="databazeknih")

		em, axis = _online_fill(self._identity_title(), Stub(), skip_enrich=False)
		assert em is not None and em.source == "databazeknih"
		assert axis == "title"

	def test_skip_enrich_returns_none(self):
		from book_meta_fix.pipeline import IdentityResult, _online_fill

		ident = IdentityResult(isbn="9788072072323", source="content-isbn")
		assert _online_fill(ident, None, skip_enrich=True) == (None, None)


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
			 patch.object(pmod, "safe_extract", lambda m: extracted):
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
			 patch.object(pmod, "safe_extract", lambda m: extracted):
			_process_book(
				meta, enricher=None, skip_enrich=True, skip_verify=True,
				llm_provider=OnceProvider(), llm_categories=("ALL",), stats=stats,
			)
		assert len(calls) == 1  # no broader → no retry
		assert stats["llm_no_result"] == 1


class TestVerifyEnrichmentLocalDb:
	"""The LOCAL-DB fallback tier: when the online tiers cannot decide, the
	recovered author or series must be KNOWN to the library (it exists on a
	verified book — Cache.is_verified_author / is_verified_series)."""

	@staticmethod
	def _online(**kw):
		from book_meta_fix.enrichers import EnrichedMeta
		return EnrichedMeta(source="databazeknih", **kw)

	@staticmethod
	def _cache(authors: tuple[str, ...] = (), series: tuple[str, ...] = ()):
		class StubCache:
			def is_verified_author(self, name):
				return name.lower() in authors
			def is_verified_series(self, name):
				return name.lower() in series
		return StubCache()

	def test_known_author_confirms(self):
		from book_meta_fix.pipeline import _verify_enrichment_local_db

		online = self._online(title="Ocelová krysa prezidentem", authors=["Harry Harrison"])
		assert _verify_enrichment_local_db(online, self._cache(authors=("harry harrison",))) is True

	def test_known_series_confirms(self):
		from book_meta_fix.pipeline import _verify_enrichment_local_db

		online = self._online(title="X", authors=["Někdo Neznámý"], series="Ocelová krysa")
		assert _verify_enrichment_local_db(online, self._cache(series=("ocelová krysa",))) is True

	def test_unknown_everything_rejects(self):
		from book_meta_fix.pipeline import _verify_enrichment_local_db

		online = self._online(title="X", authors=["Někdo Jiný"], series="Neznámá série")
		assert _verify_enrichment_local_db(online, self._cache(authors=("harry harrison",))) is False

	def test_none_cache_rejects(self):
		from book_meta_fix.pipeline import _verify_enrichment_local_db

		online = self._online(title="X", authors=["Harry Harrison"])
		assert _verify_enrichment_local_db(online, None) is False

	def test_author_known_falls_back_to_local_db(self):
		"""The ONLINE existence check (enricher.author_exists) falls back to
		the local DB when online cannot answer — here the stub has NO ladder
		method, the cache knows the author: the title-only axis still
		confirms."""
		from book_meta_fix.pipeline import IdentityResult, _verify_enrichment_online

		key = IdentityResult(title="Ocelová krysa prezidentem", authors=[], source="metadata")
		online = self._online(title="Ocelová krysa prezidentem", authors=["Harry Harrison"])

		class NoLadder:
			pass

		assert _verify_enrichment_online(online, key, None, "title", NoLadder(), cache=self._cache(authors=("harry harrison",))) is True

	def test_full_ladder_online_then_local_db_then_text(self):
		"""_try_deterministic_fix order: online tier first; when it cannot
		decide (stub without author_exists, title-only key), the LOCAL-DB
		fallback confirms; the text check would confirm last."""
		from book_meta_fix.detectors import Diagnosis as _D
		from book_meta_fix.pipeline import _try_deterministic_fix

		meta = BookMeta(
			calibre_id=9, title="Ocelová krysa prezidentem", authors=[],
			path="/lib/Anonym/B (noid)", primary_file="/lib/Anonym/B (noid)/b.epub",
		)
		extracted = ExtractedMeta(title="x", first_page_text="ani titul ani autor tu nejsou " * 10)

		class OfflineEnricher:
			"""Serves the hit but offers no author_exists ladder (offline)."""
			def lookup(self, *, isbn=None, title=None, author=None, year=None):
				from book_meta_fix.enrichers import EnrichedMeta
				if title and "prezidentem" in title.lower():
					return EnrichedMeta(title="Ocelová krysa prezidentem", authors=["Harry Harrison"], source="databazeknih")
				return None

		diag = _D(category="C9", reason="author lost", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)
		em = _try_deterministic_fix(meta, diag, extracted, OfflineEnricher(), skip_enrich=False, cache=self._cache(authors=("harry harrison",)))
		assert em is not None
		# Online could not decide (no ladder) -> local DB knows Harrison.
		assert em.identity_confirmed is True
