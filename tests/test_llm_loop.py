"""Tests for the LLM self-correction loop and the pipeline integration.

The loop (ZaiProvider.reconcile_loop) tries the free Flash model first,
injects verify_proposal feedback between attempts, and falls back to the paid
final model. These tests stub _call (no real HTTP) and verify_proposal (no
fuzzy math) to exercise the control flow.
"""
from __future__ import annotations

from unittest.mock import patch

from book_meta_fix.extractors import ExtractedMeta
from book_meta_fix.llm import ReconciledMeta, ZaiProvider
from book_meta_fix.pipeline import _process_book


def _reconciled(title, author, confidence="medium"):
	return ReconciledMeta(title=title, authors=[author], confidence=confidence)


def _extracted(title_text, author_text):
	"""An ExtractedMeta whose first_page_text contains the given title+author
	(in ALL-CAPS, mirroring real CZ/SK title pages)."""
	return ExtractedMeta(first_page_text=f"Neznámý {author_text.upper()} {title_text.upper()}")


class TestReconcileLoop:
	def _provider(self, flash_results, final_result, calls):
		"""Build a ZaiProvider whose _call records calls and returns scripted results.

		*flash_results* is a list (one per Flash attempt); *final_result* is the
		paid-fallback return. *calls* accumulates (model, had_feedback) tuples.
		"""
		p = ZaiProvider("k", min_interval=0.0, burst=10.0)
		p.flash_model = "flash"
		p.final_model = "final"
		state = {"flash_i": 0}

		def fake_call(model, evidence, *, max_retries=3):
			had_fb = "feedback" in evidence
			calls.append((model, had_fb))
			if model == "flash":
				if state["flash_i"] < len(flash_results):
					r = flash_results[state["flash_i"]]
					state["flash_i"] += 1
					return (r, None)
				return (None, "rate limit exceeded")
			return (final_result, None)

		p._call = fake_call
		return p

	def test_flash_passes_first_try(self):
		calls = []
		p = self._provider([_reconciled("Jádro Galaxie", "Gregory Benford")], None, calls)
		ext = _extracted("Jádro Galaxie", "Gregory Benford")
		result, src = p.reconcile_loop({"current": {}}, ext)
		assert src == "llm:flash"
		assert result.title == "Jádro Galaxie"
		# Only one Flash call, no fallback.
		assert calls == [("flash", False)]

	def test_flash_fails_then_succeeds_with_feedback(self):
		"""First Flash returns a wrong title (verify fails); second attempt,
		with feedback, returns the right one."""
		calls = []
		p = self._provider(
			[_reconciled("Špatný Název", "X"), _reconciled("Jádro Galaxie", "Gregory Benford")],
			None,
			calls,
		)
		ext = _extracted("Jádro Galaxie", "Gregory Benford")
		result, src = p.reconcile_loop({"current": {}}, ext)
		assert src == "llm:loop"
		assert result.title == "Jádro Galaxie"
		# Two Flash calls; the second carried feedback.
		assert calls == [("flash", False), ("flash", True)]

	def test_flash_fails_twice_then_final_model_passes(self):
		calls = []
		p = self._provider(
			[_reconciled("Špatný 1", "X"), _reconciled("Špatný 2", "Y")],
			_reconciled("Jádro Galaxie", "Gregory Benford", "high"),
			calls,
		)
		ext = _extracted("Jádro Galaxie", "Gregory Benford")
		result, src = p.reconcile_loop({"current": {}}, ext)
		assert src == "llm:high"
		assert result.title == "Jádro Galaxie"
		# 2 Flash + 1 final.
		assert calls == [("flash", False), ("flash", True), ("final", False)]

	def test_everything_fails_returns_low_confidence(self):
		"""When Flash and final both fail verify, the last proposal is returned
		with confidence='low' so the human reviewer still sees something."""
		calls = []
		p = self._provider(
			[_reconciled("Špatný 1", "X"), _reconciled("Špatný 2", "Y")],
			_reconciled("Také Špatný", "Z", "high"),
			calls,
		)
		ext = _extracted("Jádro Galaxie", "Gregory Benford")
		result, src = p.reconcile_loop({"current": {}}, ext)
		assert src == "llm:low"
		assert result is not None
		assert result.confidence == "low"

	def test_no_text_accepts_flash_immediately(self):
		"""Image-only title page (no first_page_text): accept the first Flash
		result without looping (we have nothing to verify against)."""
		calls = []
		p = self._provider([_reconciled("Cokoli", "X")], None, calls)
		ext = ExtractedMeta(first_page_text=None)
		result, src = p.reconcile_loop({"current": {}}, ext)
		assert src == "llm:flash"
		assert calls == [("flash", False)]


class TestPipelineLoopIntegration:
	def test_pipeline_calls_reconcile_loop(self):
		"""_process_book should call reconcile_loop (not reconcile) when the
		provider supports it and llm_loop is True."""
		from book_meta_fix.models import BookMeta, Confidence, Diagnosis, Verdict

		meta = BookMeta(calibre_id=1, title="x", authors=["A"], path="/x/1", primary_file=None)
		extracted = ExtractedMeta(first_page_text="Neznámý GREGORY BENFORD JÁDRO GALAXIE text")
		stats = {"ok": 0, "needs_review": 0, "det_fixed": 0, "online_fixed": 0, "llm_fixed": 0,
			"llm_flash_fixed": 0, "llm_final_fixed": 0, "llm_low_confidence": 0,
			"llm_skipped_no_text": 0, "llm_no_result": 0, "llm_error": 0,
			"unfixed": 0, "errors": 0, "content_mismatch": 0}
		from book_meta_fix import pipeline as pmod

		class StubProvider:
			name = "stub"
			loop_calls = {"n": 0}
			reconcile_calls = {"n": 0}

			def reconcile_loop(self, evidence, extracted):
				self.loop_calls["n"] += 1
				return _reconciled("Jádro Galaxie", "Gregory Benford", "high"), "llm:high"

			def reconcile(self, evidence):
				self.reconcile_calls["n"] += 1
				return _reconciled("X", "Y")

		provider = StubProvider()

		def fake_detect(m):
			return Diagnosis(category="C2", reason="x", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		with patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "_safe_extract", lambda m: extracted), \
			 patch.object(pmod, "_has_usable_text", lambda t: True), \
			 patch.object(pmod, "_try_deterministic_fix", lambda *a, **kw: None):
			_process_book(
				meta, enricher=None, skip_enrich=True, skip_verify=True,
				llm_provider=provider, llm_categories=("ALL",), stats=stats,
				llm_loop=True,
			)
		assert provider.loop_calls["n"] == 1
		assert provider.reconcile_calls["n"] == 0
		assert stats["llm_final_fixed"] == 1

	def test_no_llm_loop_falls_back_to_reconcile(self):
		"""With llm_loop=False, _process_book calls reconcile (single call)."""
		from book_meta_fix.models import BookMeta, Confidence, Diagnosis, Verdict

		meta = BookMeta(calibre_id=1, title="x", authors=["A"], path="/x/1", primary_file=None)
		extracted = ExtractedMeta(first_page_text="text " * 50)
		stats = {"ok": 0, "needs_review": 0, "det_fixed": 0, "online_fixed": 0, "llm_fixed": 0,
			"llm_flash_fixed": 0, "llm_final_fixed": 0, "llm_low_confidence": 0,
			"llm_skipped_no_text": 0, "llm_no_result": 0, "llm_error": 0,
			"unfixed": 0, "errors": 0, "content_mismatch": 0}
		from book_meta_fix import pipeline as pmod

		class StubProvider:
			name = "stub"
			loop_calls = {"n": 0}
			reconcile_calls = {"n": 0}

			def reconcile_loop(self, evidence, extracted):
				self.loop_calls["n"] += 1
				return None, ""

			def reconcile(self, evidence):
				self.reconcile_calls["n"] += 1
				return _reconciled("X", "Y")

		provider = StubProvider()

		def fake_detect(m):
			return Diagnosis(category="C2", reason="x", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		with patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "_safe_extract", lambda m: extracted), \
			 patch.object(pmod, "_has_usable_text", lambda t: True), \
			 patch.object(pmod, "_try_deterministic_fix", lambda *a, **kw: None):
			_process_book(
				meta, enricher=None, skip_enrich=True, skip_verify=True,
				llm_provider=provider, llm_categories=("ALL",), stats=stats,
				llm_loop=False,
			)
		assert provider.reconcile_calls["n"] == 1
		assert provider.loop_calls["n"] == 0
