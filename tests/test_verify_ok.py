"""Tests for the --verify-ok audit path: OK books whose metadata do not match
their content are reclassified to NEEDS_REVIEW and run through the same
enrichment + LLM fix path as detector-flagged books.

Covers: MISMATCH reclassifies; UNCERTAIN respects --strict-verify on/off;
verify_ok=False keeps OK books as OK (fast path); a reclassified book reuses
the content extracted during verify() instead of reading the file twice.
"""
from __future__ import annotations

from unittest.mock import patch

from book_meta_fix.extractors import ExtractedMeta
from book_meta_fix.models import BookMeta, Confidence, Diagnosis, Verdict
from book_meta_fix.pipeline import _process_book
from book_meta_fix.verifier import Verification


def _ok_book(calibre_id: int = 1) -> BookMeta:
	"""An OK book (no detector fires) with a primary_file so verify() can run."""
	return BookMeta(
		calibre_id=calibre_id,
		title="Some Title",
		authors=["Some Author"],
		path=f"/tmp/fake/{calibre_id}",
		primary_file="/tmp/fake/book.epub",
	)


def _stats() -> dict:
	return {
		"ok": 0, "needs_review": 0, "det_fixed": 0, "online_fixed": 0,
		"llm_fixed": 0, "llm_skipped_no_text": 0, "llm_no_result": 0, "llm_error": 0,
		"unfixed": 0, "errors": 0, "content_mismatch": 0,
	}


def _process(meta, *, verify_ok=False, strict_verify=True, verify_result=None):
	"""Run _process_book with verify() patched to return *verify_result*, and
	extraction/enrichment/LLM disabled (None providers). Returns the result
	tuple and the stats dict."""
	stats = _stats()

	# detect_fn -> OK (no structural corruption).
	from book_meta_fix import pipeline as pmod

	def fake_detect(m):
		return Diagnosis(category="OK", reason="ok", confidence=Confidence.HIGH, verdict=Verdict.OK)

	patches = [
		patch.object(pmod, "detect_fn", fake_detect),
		patch.object(pmod, "_safe_extract", lambda m: None),
	]
	if verify_result is not None:
		patches.append(patch.object(pmod, "verify", lambda m: verify_result))

	for p in patches:
		p.start()
	try:
		result = _process_book(
			meta, enricher=None, skip_enrich=True, skip_verify=False,
			llm_provider=None, llm_categories=("ALL",), stats=stats,
			verify_ok=verify_ok, strict_verify=strict_verify,
		)
	finally:
		for p in patches:
			p.stop()
	return result, stats


class TestVerifyOkReclassify:
	def test_mismatch_reclassifies_to_needs_review(self):
		"""OK book + verify_ok + MISMATCH -> NEEDS_REVIEW (CONTENT_MISMATCH),
		and the fix path runs (extracted reused from verify, no second read)."""
		meta = _ok_book(1)
		extracted = ExtractedMeta(title="Real Title", first_page_text="real title text")
		vr = Verification(result="MISMATCH", reason="title not in first page", extracted=extracted)
		result, stats = _process(meta, verify_ok=True, verify_result=vr)
		_meta, diag, verification, enriched = result
		assert diag.verdict == Verdict.NEEDS_REVIEW
		assert diag.category == "CONTENT_MISMATCH"
		assert verification is vr
		assert stats["content_mismatch"] == 1
		assert stats["needs_review"] == 1
		# enriched None because no enricher/LLM configured, but the fix path ran
		# (needs_review counter incremented, not ok).
		assert stats["ok"] == 0

	def test_uncertain_strict_reclassifies(self):
		"""OK book + verify_ok + UNCERTAIN + strict=True -> NEEDS_REVIEW."""
		meta = _ok_book(2)
		vr = Verification(result="UNCERTAIN", reason="fuzzy 0.65", extracted=None)
		result, stats = _process(meta, verify_ok=True, strict_verify=True, verify_result=vr)
		_, diag, _verification, _enriched = result
		assert diag.verdict == Verdict.NEEDS_REVIEW
		assert stats["content_mismatch"] == 1

	def test_uncertain_non_strict_stays_ok(self):
		"""OK book + verify_ok + UNCERTAIN + strict=False -> stays OK."""
		meta = _ok_book(3)
		vr = Verification(result="UNCERTAIN", reason="fuzzy 0.65", extracted=None)
		result, stats = _process(meta, verify_ok=True, strict_verify=False, verify_result=vr)
		_, diag, _verification, _enriched = result
		assert diag.verdict == Verdict.OK
		assert stats["ok"] == 1
		assert stats["content_mismatch"] == 0

	def test_verified_stays_ok(self):
		"""OK book + verify_ok + VERIFIED -> stays OK."""
		meta = _ok_book(4)
		vr = Verification(result="VERIFIED", reason="title matches", extracted=None)
		result, stats = _process(meta, verify_ok=True, verify_result=vr)
		_, diag, verification, _enriched = result
		assert diag.verdict == Verdict.OK
		assert verification is vr
		assert stats["ok"] == 1
		assert stats["content_mismatch"] == 0

	def test_verify_ok_false_skips_verify(self):
		"""Without verify_ok, an OK book is never verified (fast path). verify()
		is NOT patched here — if it were called, it would do real I/O and fail.
		Instead we only assert the result without a verify result."""
		meta = _ok_book(5)
		stats = _stats()
		from book_meta_fix import pipeline as pmod

		def fake_detect(m):
			return Diagnosis(category="OK", reason="ok", confidence=Confidence.HIGH, verdict=Verdict.OK)

		called = {"verify": False}

		def boom_verify(m):
			called["verify"] = True
			raise AssertionError("verify must not be called without verify_ok")

		with patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "verify", boom_verify):
			result = _process_book(
				meta, enricher=None, skip_enrich=True, skip_verify=False,
				llm_provider=None, llm_categories=("ALL",), stats=stats,
				verify_ok=False, strict_verify=True,
			)
		_meta, diag, verification, _enriched = result
		assert diag.verdict == Verdict.OK
		assert verification is None
		assert stats["ok"] == 1
		assert called["verify"] is False


class TestReclassifiedReusesExtracted:
	def test_extracted_from_verify_is_not_re_extracted(self):
		"""When an OK book is reclassified, the fix path must reuse the
		ExtractedMeta already produced by verify(), not call _safe_extract again."""
		meta = _ok_book(6)
		extracted = ExtractedMeta(title="Real", first_page_text="some text")
		vr = Verification(result="MISMATCH", reason="mismatch", extracted=extracted)
		stats = _stats()

		from book_meta_fix import pipeline as pmod

		def fake_detect(m):
			return Diagnosis(category="OK", reason="ok", confidence=Confidence.HIGH, verdict=Verdict.OK)

		safe_extract_calls = {"n": 0}

		def counting_safe_extract(m):
			safe_extract_calls["n"] += 1
			return None

		with patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "verify", lambda m: vr), \
			 patch.object(pmod, "_safe_extract", counting_safe_extract):
			result = _process_book(
				meta, enricher=None, skip_enrich=True, skip_verify=False,
				llm_provider=None, llm_categories=("ALL",), stats=stats,
				verify_ok=True, strict_verify=True,
			)
		# _safe_extract must NOT have been called: verification.extracted reused.
		assert safe_extract_calls["n"] == 0
		_, diag, _verification, _enriched = result
		assert diag.verdict == Verdict.NEEDS_REVIEW
