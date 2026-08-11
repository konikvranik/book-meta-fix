"""Tests for verify_proposal — validates an LLM/online proposal against the
book's actual page text (title + author fuzzy match, ISBN exact match)."""
from __future__ import annotations

from book_meta_fix.extractors import ExtractedMeta
from book_meta_fix.verifier import _author_in_text, _title_in_text, confirm_identity, verify_proposal


class _Proposal:
	"""Minimal stand-in for ReconciledMeta / EnrichedMeta."""

	def __init__(self, title, authors, isbn=None):
		self.title = title
		self.authors = authors
		self.isbn = isbn


# Real text from a CZ/SK title-page inspection.
_TEXT = "Neznámý EDUARD ŠTORCH ZASTAVENÝ PŘÍVAL List z počátku našich dějin"


class TestAuthorInText:
	def test_exact_author_found(self):
		assert _author_in_text("Eduard Štorch", _TEXT) >= 0.8

	def test_case_insensitive(self):
		assert _author_in_text("eduard štorch", _TEXT) >= 0.8

	def test_diacritics_insensitive(self):
		# 'Storch' (no háček) should still match 'Štorch'.
		assert _author_in_text("Eduard Storch", _TEXT) >= 0.8

	def test_missing_author_low_score(self):
		assert _author_in_text("Karel May", _TEXT) < 0.5


class TestVerifyProposal:
	def test_correct_title_and_author_pass(self):
		ext = ExtractedMeta(first_page_text=_TEXT)
		prop = _Proposal("Zastavený příval", ["Eduard Štorch"])
		passed, fb = verify_proposal(prop, ext)
		assert passed is True
		assert fb == ""

	def test_wrong_title_fails_with_feedback(self):
		ext = ExtractedMeta(first_page_text=_TEXT)
		prop = _Proposal("Jiná Kniha", ["Eduard Štorch"])
		passed, fb = verify_proposal(prop, ext)
		assert passed is False
		assert "Jiná Kniha" in fb
		assert "title" in fb.lower() or "název" in fb.lower()

	def test_wrong_author_fails_with_feedback(self):
		ext = ExtractedMeta(first_page_text=_TEXT)
		prop = _Proposal("Zastavený příval", ["Nesmysl Autor"])
		passed, fb = verify_proposal(prop, ext)
		assert passed is False
		assert "Nesmysl Autor" in fb or "author" in fb.lower() or "autor" in fb.lower()

	def test_empty_text_accepts_low_confidence(self):
		"""Image-only title page (no text): accept rather than loop forever."""
		ext = ExtractedMeta(first_page_text=None)
		prop = _Proposal("Cokoli", ["Někdo"])
		passed, fb = verify_proposal(prop, ext)
		assert passed is True
		assert fb == ""

	def test_isbn_mismatch_fails(self):
		# Real ISBN from the library (valid check digit).
		ext = ExtractedMeta(first_page_text=_TEXT, isbn_from_text="9788072072323")
		prop = _Proposal("Zastavený příval", ["Eduard Štorch"], isbn="9788000018300")
		# 9788000018300 is a valid ISBN (real, from the library sample) but
		# differs from the text-scanned one -> instant fail.
		passed, fb = verify_proposal(prop, ext)
		assert passed is False
		assert "ISBN" in fb or "isbn" in fb.lower()

	def test_multiple_authors_any_match(self):
		"""When the proposal lists several authors, any one matching is enough."""
		ext = ExtractedMeta(first_page_text=_TEXT)
		prop = _Proposal("Zastavený příval", ["Nesmysl", "Eduard Štorch"])
		passed, fb = verify_proposal(prop, ext)
		assert passed is True


class TestConfirmIdentity:
	"""confirm_identity is a POSITIVE gate (does content corroborate?) — the
	flip side of verify_proposal. It returns True only when content confirms,
	including via ISBN agreement even without page text."""

	def test_isbn_agreement_confirms(self):
		"""ISBN in the proposal equals an ISBN mined from content → confirmed,
		even with no title/author text matching."""
		ext = ExtractedMeta(isbn_from_text="9788072072323")
		prop = _Proposal("Anything", ["Anyone"], isbn="978-80-720-7232-3")
		assert confirm_identity(prop, ext) is True

	def test_title_and_author_in_text_confirms(self):
		ext = ExtractedMeta(first_page_text=_TEXT)
		prop = _Proposal("Zastavený příval", ["Eduard Štorch"])
		assert confirm_identity(prop, ext) is True

	def test_isbn_mismatch_not_confirmed_but_title_may_save(self):
		"""A disagreeing ISBN alone doesn't confirm — but title+author in text
		still can (independent signal)."""
		ext = ExtractedMeta(first_page_text=_TEXT, isbn_from_text="9788072072323")
		prop = _Proposal("Zastavený příval", ["Eduard Štorch"], isbn="9788000018300")
		assert confirm_identity(prop, ext) is True  # title+author win

	def test_no_content_not_confirmed(self):
		"""Unlike verify_proposal (which accepts on no text), confirm_identity
		returns False — we cannot confirm what we cannot read."""
		ext = ExtractedMeta(first_page_text=None)
		prop = _Proposal("Zastavený příval", ["Eduard Štorch"])
		assert confirm_identity(prop, ext) is False

	def test_contradicting_content_not_confirmed(self):
		"""Title/author absent from text and no ISBN → not confirmed."""
		ext = ExtractedMeta(first_page_text="úplně jiný text o něčem jiném")
		prop = _Proposal("Zastavený příval", ["Eduard Štorch"])
		assert confirm_identity(prop, ext) is False
