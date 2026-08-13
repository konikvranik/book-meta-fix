"""Tests for the unified classifier (book_meta_fix.classify).

Covers the identity gate (identified MISSING_* → OK), the is_acceptable_missing
rule, and the opt-in OK-audit (verify_ok). These are the rules shared by report,
organize, epubgen and the pipeline gate, so they must hold exactly here.
"""
from __future__ import annotations

from unittest.mock import patch

from book_meta_fix import classify as cmod
from book_meta_fix.classify import classify, is_acceptable_missing
from book_meta_fix.extractors import ExtractedMeta
from book_meta_fix.models import BookMeta, Confidence, Diagnosis, Verdict


def _meta(title: str = "Bílá nemoc", author: str = "Karel Čapek", cid: int = 10) -> BookMeta:
	return BookMeta(
		calibre_id=cid,
		title=title,
		authors=[author],
		path=f"/lib/{title} ({cid})",
		primary_file=f"/lib/{title} ({cid})/book.epub",
	)


def _diag(category: str, verdict: Verdict, additional: list[Diagnosis] | None = None) -> Diagnosis:
	d = Diagnosis(category=category, reason="r", confidence=Confidence.LOW, verdict=verdict)
	if additional:
		d.additional = list(additional)
	return d


def _run(
	meta: BookMeta,
	*,
	diag: Diagnosis,
	first_page_text: str | None = "<sentinel>",
	accept_missing: bool = True,
	verify_ok: bool = False,
	strict_verify: bool = True,
):
	"""Classify with detect + safe_extract mocked.

	first_page_text=None makes safe_extract return None (no content). acquire_identity
	and verify run for real (acquire_identity is pure; verify is only reached with
	verify_ok=True, where the caller mocks it separately).
	"""

	def fake_detect(_m):
		return diag

	def fake_extract(_m):
		return None if first_page_text is None else ExtractedMeta(first_page_text=first_page_text)

	with patch.object(cmod, "detect", fake_detect), patch.object(cmod, "safe_extract", fake_extract):
		return classify(meta, accept_missing=accept_missing, verify_ok=verify_ok, strict_verify=strict_verify)


class TestIsAcceptableMissing:
	def test_missing_categories_acceptable(self):
		for cat in ("MISSING_ISBN", "MISSING_YEAR", "MISSING_COVER"):
			assert is_acceptable_missing(_diag(cat, Verdict.AUTO_FIXABLE)) is True

	def test_coreview_additional_blocks(self):
		# A co-occurring NEEDS_REVIEW (e.g. generated cover C11) blocks acceptance.
		extra = [_diag("C11", Verdict.NEEDS_REVIEW)]
		assert is_acceptable_missing(_diag("MISSING_ISBN", Verdict.AUTO_FIXABLE, extra)) is False

	def test_autofixable_additional_does_not_block(self):
		# MISSING_COVER is AUTO_FIXABLE, so it does not block a MISSING_ISBN primary.
		extra = [_diag("MISSING_COVER", Verdict.AUTO_FIXABLE)]
		assert is_acceptable_missing(_diag("MISSING_ISBN", Verdict.AUTO_FIXABLE, extra)) is True

	def test_non_missing_category_not_acceptable(self):
		assert is_acceptable_missing(_diag("C2", Verdict.NEEDS_REVIEW)) is False
		assert is_acceptable_missing(_diag("OK", Verdict.OK)) is False


class TestIdentityGate:
	def test_identified_missing_routes_to_ok(self):
		# Title + author both appear in the first-page text -> identity confirmed.
		text = "Bílá nemoc\nKarel Čapek\nRomán o lidské slušnosti."
		c = _run(_meta(), diag=_diag("MISSING_ISBN", Verdict.AUTO_FIXABLE), first_page_text=text)
		assert c.verdict == Verdict.OK
		assert c.identified is True
		# The raw category is preserved (still reported as MISSING_ISBN).
		assert c.diag.category == "MISSING_ISBN"

	def test_identity_not_confirmed_stays(self):
		# First-page text does NOT contain the title/author -> not identified.
		text = "Lorem ipsum dolor sit amet, consectetur adipiscing elit."
		c = _run(_meta(), diag=_diag("MISSING_ISBN", Verdict.AUTO_FIXABLE), first_page_text=text)
		assert c.verdict == Verdict.AUTO_FIXABLE
		assert c.identified is False

	def test_no_content_stays(self):
		c = _run(_meta(), diag=_diag("MISSING_ISBN", Verdict.AUTO_FIXABLE), first_page_text=None)
		assert c.verdict == Verdict.AUTO_FIXABLE
		assert c.identified is False

	def test_coreview_additional_blocks_promotion(self):
		# Identity would confirm, but a co-occurring NEEDS_REVIEW blocks the gate.
		text = "Bílá nemoc\nKarel Čapek"
		extra = [_diag("C11", Verdict.NEEDS_REVIEW)]
		c = _run(_meta(), diag=_diag("MISSING_ISBN", Verdict.AUTO_FIXABLE, extra), first_page_text=text)
		assert c.identified is False
		assert c.verdict == Verdict.AUTO_FIXABLE

	def test_accept_missing_false_skips_gate_and_reads_nothing(self):
		# With the gate disabled, safe_extract must not be called at all (fast path).
		calls = {"n": 0}

		def fake_detect(_m):
			return _diag("MISSING_ISBN", Verdict.AUTO_FIXABLE)

		def fake_extract(_m):
			calls["n"] += 1
			return ExtractedMeta(first_page_text="Bílá nemoc\nKarel Čapek")

		with patch.object(cmod, "detect", fake_detect), patch.object(cmod, "safe_extract", fake_extract):
			c = classify(_meta(), accept_missing=False)
		assert c.verdict == Verdict.AUTO_FIXABLE
		assert c.identified is False
		assert calls["n"] == 0

	def test_needs_review_primary_not_promoted(self):
		# A genuinely broken book (C2) is not rescued by a confirmable identity.
		c = _run(_meta(), diag=_diag("C2", Verdict.NEEDS_REVIEW), first_page_text="Bílá nemoc\nKarel Čapek")
		assert c.verdict == Verdict.NEEDS_REVIEW
		assert c.identified is False


class TestVerifyOk:
	"""The opt-in OK-audit (default off, like report/analyze)."""

	def _run_verify(self, result: str, *, strict: bool = True, diag=None) -> Verdict:
		from book_meta_fix.verifier import Verification

		ver = Verification(result=result, extracted=ExtractedMeta(first_page_text="x"))
		diag = diag or _diag("OK", Verdict.OK)

		def fake_detect(_m):
			return diag

		def fake_verify(_m, **kw):  # noqa: ARG001
			return ver

		with patch.object(cmod, "detect", fake_detect), patch.object(cmod, "verify", fake_verify):
			return classify(_meta(), verify_ok=True, strict_verify=strict).verdict

	def test_mismatch_demotes(self):
		assert self._run_verify("MISMATCH") == Verdict.NEEDS_REVIEW

	def test_uncertain_strict_demotes(self):
		assert self._run_verify("UNCERTAIN", strict=True) == Verdict.NEEDS_REVIEW

	def test_uncertain_non_strict_stays_ok(self):
		assert self._run_verify("UNCERTAIN", strict=False) == Verdict.OK

	def test_verified_stays_ok(self):
		assert self._run_verify("VERIFIED") == Verdict.OK

	def test_no_content_stays_ok(self):
		# verify() returns NO_CONTENT -> we cannot say it's broken -> stays OK.
		assert self._run_verify("NO_CONTENT") == Verdict.OK

	def test_verify_ok_false_does_not_call_verify(self):
		calls = {"n": 0}

		def fake_detect(_m):
			return _diag("OK", Verdict.OK)

		def fake_verify(*a, **kw):  # noqa: ANN001, ARG001
			calls["n"] += 1

		with patch.object(cmod, "detect", fake_detect), patch.object(cmod, "verify", fake_verify):
			c = classify(_meta(), verify_ok=False)
		assert c.verdict == Verdict.OK
		assert calls["n"] == 0
