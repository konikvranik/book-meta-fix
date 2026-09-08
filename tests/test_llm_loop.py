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
		p.model = "flash"
		p.fallback_model = "final"
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
		# 2 Flash + 1 final; the final call carried the verify feedback too —
		# the stronger model must know why the flash answers were rejected.
		assert calls == [("flash", False), ("flash", True), ("final", True)]

	def test_rate_limited_flash_falls_back_without_feedback(self):
		"""Flash unusable (rate limit) before any verify ran: there is no
		rejection reason to report, so the paid model runs on plain evidence."""
		calls = []
		p = self._provider([], _reconciled("Jádro Galaxie", "Gregory Benford", "high"), calls)
		ext = _extracted("Jádro Galaxie", "Gregory Benford")
		result, src = p.reconcile_loop({"current": {}}, ext)
		assert src == "llm:high"
		assert calls == [("flash", False), ("final", False)]

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

			def reconcile_loop(self, evidence, extracted, **kwargs):
				self.loop_calls["n"] += 1
				return _reconciled("Jádro Galaxie", "Gregory Benford", "high"), "llm:high"

			def reconcile(self, evidence):
				self.reconcile_calls["n"] += 1
				return _reconciled("X", "Y")

		provider = StubProvider()

		def fake_detect(m):
			return Diagnosis(category="C2", reason="x", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		with patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "safe_extract", lambda m: extracted), \
			 patch.object(pmod, "has_usable_text", lambda t: True), \
			 patch.object(pmod, "_try_deterministic_fix", lambda *a, **kw: None):
			_process_book(
				meta, enricher=None, skip_enrich=True, skip_verify=True,
				llm_provider=provider, llm_categories=("ALL",), stats=stats,
				llm_loop=True,
			)
		assert provider.loop_calls["n"] == 1
		assert provider.reconcile_calls["n"] == 0
		assert stats["llm_final_fixed"] == 1

	def test_pipeline_skips_llm_for_decided_prior(self):
		"""A book whose prior review entry is already decided must not pay for
		an LLM call: the review writer carries the prior entry verbatim, so a
		fresh proposal would be discarded unread. Without this gate every
		analyze re-run before apply re-buys the same answers — the single
		biggest token sink in the workflow."""
		from book_meta_fix.models import BookMeta, Confidence, Diagnosis, Verdict

		meta = BookMeta(calibre_id=1, uuid="decided-1", title="x", authors=["A"], path="/x/1", primary_file=None)
		extracted = ExtractedMeta(first_page_text="Neznámý GREGORY BENFORD JÁDRO GALAXIE text")
		stats = {"ok": 0, "needs_review": 0, "det_fixed": 0, "online_fixed": 0, "llm_fixed": 0,
			"llm_flash_fixed": 0, "llm_final_fixed": 0, "llm_low_confidence": 0,
			"llm_skipped_no_text": 0, "llm_skipped_decided": 0, "llm_no_result": 0,
			"llm_error": 0, "unfixed": 0, "errors": 0, "content_mismatch": 0}
		from book_meta_fix import pipeline as pmod

		class StubProvider:
			name = "stub"
			calls = {"n": 0}

			def reconcile_loop(self, evidence, extracted, **kwargs):
				self.calls["n"] += 1
				return _reconciled("Jádro Galaxie", "Gregory Benford", "high"), "llm:high"

			def reconcile(self, evidence):
				self.calls["n"] += 1
				return _reconciled("X", "Y")

		provider = StubProvider()

		def fake_detect(m):
			return Diagnosis(category="C2", reason="x", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)

		with patch.object(pmod, "detect_fn", fake_detect), \
			 patch.object(pmod, "safe_extract", lambda m: extracted), \
			 patch.object(pmod, "has_usable_text", lambda t: True), \
			 patch.object(pmod, "_try_deterministic_fix", lambda *a, **kw: None):
			_process_book(
				meta, enricher=None, skip_enrich=True, skip_verify=True,
				llm_provider=provider, llm_categories=("ALL",), stats=stats,
				llm_loop=True, llm_skip_ids={"decided-1"},
			)
		assert provider.calls["n"] == 0
		assert stats["llm_skipped_decided"] == 1

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
			 patch.object(pmod, "safe_extract", lambda m: extracted), \
			 patch.object(pmod, "has_usable_text", lambda t: True), \
			 patch.object(pmod, "_try_deterministic_fix", lambda *a, **kw: None):
			_process_book(
				meta, enricher=None, skip_enrich=True, skip_verify=True,
				llm_provider=provider, llm_categories=("ALL",), stats=stats,
				llm_loop=False,
			)
		assert provider.reconcile_calls["n"] == 1
		assert provider.loop_calls["n"] == 0


class TestPromptEnhancements:
	def test_build_user_prompt_with_all_enhancements(self):
		from book_meta_fix.llm import build_user_prompt

		evidence = {
			"category": "C1",
			"current": {"title": "Wrong Title", "authors": ["Wrong Author"]},
			"file_name": "book.epub",
			"author_folder": "Author",
			"title_folder": "Title",
			"first_page_text": "A" * 7000,
			"max_text_len": 6000,
			"known_authors": ["Isaac Asimov"],
			"known_series": ["Nadace"],
			"online_candidate": {
				"title": "Nadace a Říše",
				"authors": ["Isaac Asimov"],
				"year": 1952,
				"source": "databazeknih",
			},
			"feedback": "title mismatch",
		}
		prompt = build_user_prompt(evidence)
		assert "Known verified author spellings in this library:" in prompt
		assert "  - Isaac Asimov" in prompt
		assert "Known verified series in this library:" in prompt
		assert "  - Nadace" in prompt
		assert "Unconfirmed online match (source: databazeknih):" in prompt
		assert "Nadace a Říše" in prompt
		assert "Your previous answer was REJECTED because:" in prompt
		assert "title mismatch" in prompt
		# Text length should be capped at max_text_len (6000), not 2000
		assert "A" * 6000 in prompt
		assert "A" * 6001 not in prompt

	def test_reconcile_loop_fallback_uses_broader_text_and_large_window(self):
		p = ZaiProvider("k", min_interval=0.0, burst=10.0)
		p.model = "flash"
		p.fallback_model = "final"

		recorded_evidence = []

		def fake_call(model, evidence, *, max_retries=3):
			recorded_evidence.append((model, dict(evidence)))
			if model == "flash":
				return (_reconciled("Bad Title", "X"), None)
			return (_reconciled("Real Title", "Isaac Asimov", "high"), None)

		p._call = fake_call
		ext = ExtractedMeta(
			first_page_text="Neznámý krátký text",
			broader_text="Neznámý ISAAC ASIMOV REAL TITLE " + "obsah " * 500,
		)
		result, src = p.reconcile_loop({"current": {}}, ext)
		assert src == "llm:high"
		assert result.title == "Real Title"
		# Check fallback call evidence
		assert len(recorded_evidence) == 3  # 2 flash + 1 final
		final_call = recorded_evidence[-1]
		assert final_call[0] == "final"
		assert final_call[1]["max_text_len"] == 6000
		assert final_call[1]["first_page_text"] == ext.broader_text

	def test_build_llm_evidence_gathers_verified_and_online(self, tmp_path):
		from unittest.mock import MagicMock

		from book_meta_fix.enrichers import EnrichedMeta
		from book_meta_fix.library import Cache
		from book_meta_fix.models import BookMeta, Confidence, Diagnosis, Verdict
		from book_meta_fix.pipeline import _build_llm_evidence

		cache = Cache(tmp_path / "cache.db")
		b = BookMeta(
			uuid="u1",
			path=str(tmp_path / "Isaac Asimov" / "Nadace (1)"),
			title="Nadace",
			authors=["Isaac Asimov"],
			series=["Nadace"],
			verified=True,
		)
		cache.put(b)
		cache.commit()

		diag = Diagnosis(category="C1", reason="swap", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)
		meta = BookMeta(
			calibre_id=2,
			title="Nadace a Říše",
			authors=["Asimov"],
			series=["Nadace #2"],
			path="/books/2",
			primary_file="nadace.epub",
		)
		ext = ExtractedMeta(first_page_text="ukázka")

		enricher = MagicMock()
		enricher.lookup.return_value = EnrichedMeta(
			title="Nadace a Říše",
			authors=["Isaac Asimov"],
			year=1952,
			source="databazeknih",
		)

		evidence = _build_llm_evidence(meta, diag, ext, cache=cache, enricher=enricher, skip_enrich=False)
		assert evidence["known_authors"] == ["Isaac Asimov"]
		assert evidence["known_series"] == ["Nadace"]
		assert evidence["online_candidate"]["title"] == "Nadace a Říše"
		assert evidence["online_candidate"]["source"] == "databazeknih"
		cache.close()

