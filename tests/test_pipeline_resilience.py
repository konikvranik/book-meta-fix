"""Tests for pipeline resilience: one book's failure must not abort the run.

Regression for the crash where a single book raising an unhandled exception
(e.g. TypeError in _is_better) propagated through fut.result() and killed the
whole pipeline — throwing away LLM tokens already spent on other books.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from book_meta_fix.models import BookMeta, Verdict
from book_meta_fix.pipeline import run_pipeline


def _make_book(calibre_id: int, title: str = "Test", author: str = "Author") -> BookMeta:
	"""Build a minimal BookMeta that won't trigger I/O."""
	return BookMeta(
		calibre_id=calibre_id,
		title=title,
		authors=[author],
		path=f"/tmp/fake/{calibre_id}",
		primary_file=None,  # no file -> no extraction/verify I/O
	)


class TestPipelineResilience:
	def test_one_book_failure_does_not_abort_run(self, tmp_path):
		"""A book that raises mid-processing still yields a result tuple, and
		the other books are processed normally."""
		books = [_make_book(1, "Good"), _make_book(2, "Boom"), _make_book(3, "Also Good")]

		# Patch scan_library to return our fake books, and detect_fn to mark
		# them all NEEDS_REVIEW so _process_book walks the extract path.
		# Then make book id=2 blow up inside _try_deterministic_fix by patching
		# _safe_extract to raise for it.
		from book_meta_fix import pipeline as pmod

		def fake_scan(library, cache=None):
			return books

		def fake_detect(meta):
			from book_meta_fix.models import Confidence, Diagnosis
			return Diagnosis(category="C2", reason="test", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		call_count = {"extract": 0}

		def fake_extract(meta):
			# Book 2 raises an unhandled error (simulating the original crash).
			call_count["extract"] += 1
			if meta.calibre_id == 2:
				raise TypeError("simulated crash")
			return None  # no content -> no deterministic fix

		with patch.object(pmod, "scan_library", fake_scan), \
			 patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "_safe_extract", fake_extract):
			# workers=1 for deterministic, debuggable ordering
			results = run_pipeline(
				tmp_path, cache=None, enricher=None,
				skip_enrich=True, skip_verify=True,
				llm_provider=None, workers=1,
			)

		# All 3 books present in results (none dropped).
		ids = [r[0].calibre_id for r in results]
		assert ids == [1, 2, 3]
		# Book 2 got an ERROR diagnosis with NEEDS_REVIEW verdict so it lands
		# in the review report despite the crash.
		boom = next(r for r in results if r[0].calibre_id == 2)
		assert boom[1].category == "ERROR"
		assert boom[1].verdict == Verdict.NEEDS_REVIEW
		assert boom[3] is None  # no enriched proposal (crashed before producing one)
		# The crash didn't prevent books 1 and 3 from being extracted.
		assert call_count["extract"] == 3

	def test_threaded_path_also_resilient(self, tmp_path):
		"""Same as above but via the ThreadPool path (workers > 1). This is
		the path that produced the original crash via fut.result()."""
		books = [_make_book(i, f"B{i}") for i in range(6)]

		from book_meta_fix import pipeline as pmod

		def fake_scan(library, cache=None):
			return books

		def fake_detect(meta):
			from book_meta_fix.models import Confidence, Diagnosis
			return Diagnosis(category="C2", reason="test", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		def fake_extract(meta):
			# Books with even id crash.
			if meta.calibre_id % 2 == 0:
				raise RuntimeError("simulated worker crash")
			return None

		with patch.object(pmod, "scan_library", fake_scan), \
			 patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "_safe_extract", fake_extract):
			results = run_pipeline(
				tmp_path, cache=None, enricher=None,
				skip_enrich=True, skip_verify=True,
				llm_provider=None, workers=3,
			)

		ids = sorted(r[0].calibre_id for r in results)
		assert ids == [0, 1, 2, 3, 4, 5]  # all present, order may vary then sort
		# Even-id books recorded as ERROR; odd ones as C2 (no crash).
		by_id = {r[0].calibre_id: r for r in results}
		assert by_id[0][1].category == "ERROR"
		assert by_id[1][1].category == "C2"
		assert by_id[2][1].category == "ERROR"
		assert by_id[3][1].category == "C2"

	def test_errors_counted_in_stats_via_log(self, tmp_path, caplog):
		"""The final log line reports the error count (smoke check that the
		stats dict's 'errors' key is populated and logged)."""
		books = [_make_book(1, "Boom")]
		from book_meta_fix import pipeline as pmod
		import logging

		def fake_scan(library, cache=None):
			return books

		def fake_detect(meta):
			from book_meta_fix.models import Confidence, Diagnosis
			return Diagnosis(category="C2", reason="test", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		def fake_extract(meta):
			raise ValueError("boom")

		with patch.object(pmod, "scan_library", fake_scan), \
			 patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "_safe_extract", fake_extract), \
			 caplog.at_level(logging.INFO, logger="book_meta_fix.pipeline"):
			results = run_pipeline(tmp_path, cache=None, workers=1)

		# The summary log line mentions errors=1.
		summary_lines = [r.getMessage() for r in caplog.records if "pipeline:" in r.getMessage() and "errors=" in r.getMessage()]
		assert summary_lines, "expected a pipeline summary log line mentioning errors="
		assert "errors=1" in summary_lines[-1]


class TestInterruptHandling:
	"""Ctrl-C (KeyboardInterrupt) must yield partial results, not raise."""

	def test_serial_interrupt_returns_partial_results(self, tmp_path):
		"""In the serial path, a KeyboardInterrupt on the 3rd book must return
		the first 2 results and not propagate."""
		books = [_make_book(i, f"B{i}") for i in range(1, 6)]  # ids 1..5
		from book_meta_fix import pipeline as pmod

		def fake_scan(library, cache=None):
			return books

		def fake_detect(meta):
			from book_meta_fix.models import Confidence, Diagnosis
			return Diagnosis(category="C2", reason="test", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		call_count = [0]

		def fake_extract(meta):
			call_count[0] += 1
			# Interrupt on the 3rd book (id=3).
			if meta.calibre_id == 3:
				raise KeyboardInterrupt
			return None

		with patch.object(pmod, "scan_library", fake_scan), \
			 patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "_safe_extract", fake_extract):
			# workers=1 forces the serial path.
			results = run_pipeline(tmp_path, cache=None, workers=1)

		ids = [r[0].calibre_id for r in results]
		assert ids == [1, 2]  # 3rd was interrupted, 4/5 never started

	def test_threaded_interrupt_returns_partial_results(self, tmp_path):
		"""In the threaded path, a KeyboardInterrupt out of fut.result() (which
		is how a real Ctrl-C surfaces during a blocking result() call) must be
		caught: already-collected results are returned and pending futures are
		cancelled, without propagating the exception."""
		books = [_make_book(i, f"B{i}") for i in range(1, 6)]
		from book_meta_fix import pipeline as pmod

		def fake_scan(library, cache=None):
			return books

		def fake_detect(meta):
			from book_meta_fix.models import Confidence, Diagnosis
			return Diagnosis(category="C2", reason="test", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		# Pipeline now reads futures via concurrent.futures.as_completed. We
		# patch BOTH ThreadPoolExecutor (so no real threads spawn) and
		# as_completed (so we control the iteration order and the interrupt).
		from book_meta_fix.models import Confidence, Diagnosis

		def _ok_result(meta):
			diag = Diagnosis(category="C2", reason="ok", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)
			return (meta, diag, None, None)

		class FakeFuture:
			def __init__(self, meta, fail_with_keyboard_interrupt):
				self._meta = meta
				self._kbd = fail_with_keyboard_interrupt

			def result(self, timeout=None):
				if self._kbd:
					raise KeyboardInterrupt
				return _ok_result(self._meta)

			def cancel(self):
				return True

		class FakePool:
			def __init__(self, max_workers):
				self._counter = 0

			def submit(self, fn, meta):
				self._counter += 1
				return FakeFuture(meta, fail_with_keyboard_interrupt=(self._counter == 3))

			def __enter__(self):
				return self

			def __exit__(self, *a):
				return False

		# Yield the futures in submit order; the 3rd raises on result().
		def fake_as_completed(futures):
			return list(futures.keys())

		with patch.object(pmod, "scan_library", fake_scan), \
			 patch.object(pmod, "detect_fn", fake_detect), \
			 patch("book_meta_fix.pipeline.ThreadPoolExecutor", FakePool), \
			 patch("book_meta_fix.pipeline.as_completed", fake_as_completed):
			results = run_pipeline(tmp_path, cache=None, workers=2)

		# Books 1 and 2 completed before the interrupt; book 3 raised KbdInt;
		# books 4 and 5 were cancelled (never collected). So we expect exactly
		# the first two results (order is completion order = submit order here).
		ids = [r[0].calibre_id for r in results]
		assert ids == [1, 2]

	def test_no_crash_when_interrupted_before_any_result(self, tmp_path):
		"""Ctrl-C before the first book completes still returns cleanly (empty
		results list) rather than raising."""
		from book_meta_fix import pipeline as pmod
		books = [_make_book(1, "B1")]

		def fake_scan(library, cache=None):
			return books

		def fake_detect(meta):
			from book_meta_fix.models import Confidence, Diagnosis
			return Diagnosis(category="C2", reason="test", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		def fake_extract(meta):
			raise KeyboardInterrupt

		with patch.object(pmod, "scan_library", fake_scan), \
			 patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "_safe_extract", fake_extract):
			results = run_pipeline(tmp_path, cache=None, workers=1)

		assert results == []

