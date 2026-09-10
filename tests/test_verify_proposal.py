"""Tests for verify_proposal — validates an LLM/online proposal against the
book's actual page text (title + author fuzzy match, ISBN exact match)."""
from __future__ import annotations

from book_meta_fix.extractors import ExtractedMeta
from book_meta_fix.verifier import (
	_author_in_text,
	_author_variant_in_text,
	_isbn_in_content,
	confirm_identity,
	identity_agrees,
	verify_proposal,
)


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


class TestAuthorFormatVariants:
	"""The printed credit is the same author in a different format.

	Variant classes measured on the real library (see the survey comment in
	verifier.py): initials, dropped middle names/initials, inflected or
	transliterated surnames, a co-author joined by a connective. The FIRST
	given name stays mandatory — that is the homonym guard."""

	# --- positive: format variants of the same author ---

	def test_initial_for_given_name(self):
		# Real title page: 'A. Buškov' credits 'Alexandr Buškov'.
		text = "Neznámý A. Buškov - Rytířka Natal Kilometrovník první"
		assert _author_in_text("Alexandr Buškov", text) >= 0.8

	def test_bare_initial_without_dot_next_to_surname(self):
		# Some e-book credits lose the dots; the bare 'A' counts only
		# directly beside the surname (adjacency is the evidence it is a
		# name-forming initial, not a preposition).
		assert _author_in_text("Alexandr Buškov", "A Buškov: Rytířka Natal") >= 0.8

	def test_glued_initials_one_token(self):
		# Real credit shape: 'G.J.Arnaud' — dots survive but glue the name
		# into one whitespace token.
		text = "Neznámý G.J.Arnaud 16. Ledová společnost Lien Rag se probudil"
		assert _author_in_text("Gérard-J. Arnaud", text) >= 0.8
		assert _author_in_text("G. J. Arnaud", text) >= 0.8

	def test_dropped_middle_name(self):
		text = "Neznámý Brian Aldiss Starý stý Prašná cesta se svažovala"
		assert _author_in_text("Brian Wilson Aldiss", text) >= 0.8

	def test_dropped_middle_initials(self):
		text = "Neznámý Avram Davidson I. Draka vyplašili v belroských lesích"
		assert _author_in_text("Avram J. A. Davidson", text) >= 0.8

	def test_metadata_initials_text_full_names(self):
		text = "Phyllis Dorothy James Ponurá stará vila na okraji Cornwallu"
		assert _author_in_text("P. D. James", text) >= 0.8

	def test_inflected_surname_czech(self):
		# Genitive credit: 'od A. Buškova'.
		text = "román od A. Buškova, přeložila Marie Kovalová"
		assert _author_in_text("Alexandr Buškov", text) >= 0.8

	def test_coauthor_with_connective(self):
		# authors[0] credit inside 'Arkadij a Boris Strugačtí'.
		text = "Neznámý Arkadij a Boris Strugačtí - Nepokoj (Bespokojstvo)"
		assert _author_in_text("Arkadij Strugackij", text) >= 0.8

	def test_transliterated_surname(self):
		# 'Andersen' typo in an e-book credit vs 'Anderson' in metadata.
		text = "Neznámý Poul Andersen Formace malých úderných lodí"
		assert _author_in_text("Poul Anderson", text) >= 0.8

	def test_comma_form_reversed_credit(self):
		assert _author_in_text("Egon Bondy", "BONDY, EGON 3x Egon Bondy") >= 0.8

	# --- negative: prose mentions and homonyms must NOT confirm ---
	# The gate everywhere is fuzzy_strong = 0.8, so negatives assert < 0.8
	# (the legacy fuzzy fallback stays lenient around 0.5-0.7 on shared
	# surnames — that is pre-existing and below every gate).

	def test_surname_mention_without_given_name(self):
		# Real false positive: 'Poe' mentioned inside a Feist novel.
		text = "Trůn v městě podivném, Poe, Město v moři, Neznámý"
		assert _author_in_text("Edgar Allan Poe", text) < 0.8

	def test_surname_in_url(self):
		# Real false positive: 'kosek.cz' in an XML handbook.
		text = "více na http www kosek cz clanky cw sgml"
		assert _author_in_text("Jiří Kosek", text) < 0.8

	def test_homonym_same_surname_different_author(self):
		# The first given name is mandatory — no in-position skipping.
		text = "Kevin J. Anderson Formace malých úderných lodí"
		assert _author_in_text("Poul Anderson", text) < 0.8

	def test_homonym_reverse_direction(self):
		text = "Poul Anderson Formace malých úderných lodí"
		assert _author_in_text("Kevin J. Anderson", text) < 0.8

	def test_homonym_capbrothers(self):
		text = "Josef Čapek Ze života hmyzu"
		assert _author_in_text("Karel Čapek", text) < 0.8

	def test_wrong_initial_does_not_confirm(self):
		assert _author_in_text("Alexandr Buškov", "J. Buškov - úplně jiná kniha") < 0.8

	def test_preposition_posing_as_initial(self):
		# Real false positive: the Baxter dedication mentions the SON
		# ('Jamesi Baxterovi'), and a nearby Czech preposition 's' must not
		# pose as the initial of 'Stephen'.
		text = "Mému synovci Jamesi Baxterovi, když se s ní uvidíme zas"
		assert _author_in_text("Stephen Baxter", text) < 0.8

	def test_inflected_given_name_is_not_a_variant(self):
		# 'Jamesi' (a different person, the son) must not match 'James' in
		# the structural matcher. (The legacy fuzzy fallback alone still
		# scores this ~0.9 — pre-existing leniency, kept because the joint
		# title+author gate and the upstream identity gates bear the risk.)
		from book_meta_fix.verifier import _normalize

		text = "Mému synovci Jamesi Baxterovi věnoval"
		assert _author_variant_in_text(_normalize("James Baxter"), _normalize(text)) is False


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

	def test_strong_title_passes_even_with_nonmatching_author(self):
		"""A STRONG title match identifies the book even when the proposed author
		isn't in the text — the author is often printed only on the cover. Before
		the title-only relaxation this failed; now it passes (title ≥ 0.90)."""
		ext = ExtractedMeta(first_page_text=_TEXT)
		prop = _Proposal("Zastavený příval", ["Nesmysl Autor"])
		passed, fb = verify_proposal(prop, ext)
		assert passed is True

	def test_author_feedback_when_title_not_strong(self):
		"""When the title-only path cannot fire (title_strong set above reach),
		a non-matching author still fails and names the author in feedback."""
		ext = ExtractedMeta(first_page_text=_TEXT)
		prop = _Proposal("Zastavený příval", ["Nesmysl Autor"])
		passed, fb = verify_proposal(prop, ext, title_strong=1.01)
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


# Text where the title is present but the author is NOT — the common novel case
# (the author is printed on the cover/copyright page, which the extractor can't
# read). Used to exercise the title-only-strong and broader-search paths.
_BODY_WITH_TITLE = (
	"Kapitola první. Ranní mlha ležela nad údolím a nikdo netušil, co přinese. "
	"Zastavený příval — to byl přece ten slavný román, který všichni znali. "
	"Pak přišla zima a s ní i další události, jež změnily běh dějin."
)
# A copyright-page-style text a few pages into the book (→ broader window).
_COPYRIGHT_PAGE = (
	"Vydalo nakladatelství Toulec 2017. Zastavený příval, Eduard Štorch. "
	"ISBN 978-80-720-7232-3. Tisk Toulec, s. r. o."
)


class TestBroaderSearch:
	"""The verifier must search the broader window too, not just page 1 — a
	title/author/ISBN on the title or copyright page (a few pages in) is just
	as much evidence as one on the first page."""

	def test_title_author_in_broader_only_passes_verify(self):
		"""Title+author absent from page 1 but present in broader → passes."""
		ext = ExtractedMeta(first_page_text="Obsah Knihy neboli tahák.", broader_text=_COPYRIGHT_PAGE)
		prop = _Proposal("Zastavený příval", ["Eduard Štorch"])
		passed, fb = verify_proposal(prop, ext)
		assert passed is True
		assert fb == ""

	def test_isbn_in_content_searches_broader(self):
		"""_isbn_in_content finds an ISBN that is only in the broader window."""
		ext = ExtractedMeta(first_page_text="žádné isbn na první straně", broader_text=_COPYRIGHT_PAGE)
		assert _isbn_in_content("978-80-720-7232-3", ext) is True
		# And the matching proposal is confirmed via the ISBN-only path.
		prop = _Proposal("Cokoli", ["Nikdo"], isbn="9788072072323")
		passed, _ = verify_proposal(prop, ext)
		assert passed is True

	def test_isbn_confirms_anywhere_even_without_title_author(self):
		ext = ExtractedMeta(first_page_text="úplně jiný text", broader_text=_COPYRIGHT_PAGE)
		prop = _Proposal("Jiný titul", ["Jiný Autor"], isbn="9788072072323")
		# ISBN confirmed in broader → verify passes, identity confirmed.
		assert verify_proposal(prop, ext)[0] is True
		assert confirm_identity(prop, ext) is True

	def test_prefix_aligned_broader_reaches_past_search_cap(self):
		"""Real extractor output is prefix-aligned: broader_text starts where
		first_page_text starts, so its only ADDED evidence sits past the
		4000-char search cap. A capped window search just re-sees page 1 and
		finds nothing new — the copyright page a few pages in needs the whole
		window."""
		pad = "Obyčejný text úplně jiné kapitoly románu. " * 150  # ~6k chars
		broader = pad + _COPYRIGHT_PAGE
		ext = ExtractedMeta(first_page_text=broader[:5000], broader_text=broader)
		prop = _Proposal("Zastavený příval", ["Eduard Štorch"])
		assert verify_proposal(prop, ext)[0] is True
		assert confirm_identity(prop, ext) is True


class TestTitleOnlyStrong:
	"""A STRONG title match (>= 0.90) confirms identity even when the author is
	genuinely absent from the book's text — the #1 unlock for novels whose
	author is only printed on the cover."""

	def test_verify_passes_on_strong_title_without_author(self):
		ext = ExtractedMeta(first_page_text=_BODY_WITH_TITLE)
		prop = _Proposal("Zastavený příval", ["Neznámý Autor"])  # author not in text
		passed, fb = verify_proposal(prop, ext)
		assert passed is True
		assert fb == ""

	def test_confirm_identity_on_strong_title_without_author(self):
		ext = ExtractedMeta(first_page_text=_BODY_WITH_TITLE)
		prop = _Proposal("Zastavený příval", ["Neznámý Autor"])
		assert confirm_identity(prop, ext) is True

	def test_weak_title_without_author_still_fails(self):
		"""A title that only partially matches (below title_strong) must NOT get
		a free pass without the author."""
		ext = ExtractedMeta(first_page_text="úplně jiný text o něčem jiném")
		prop = _Proposal("Zastavený příval", ["Neznámý Autor"])
		passed, fb = verify_proposal(prop, ext)
		assert passed is False
		assert confirm_identity(prop, ext) is False

	def test_short_generic_title_does_not_confirm_alone(self):
		"""A tiny/generic title ('It') partial_ratio-matches almost any text, so
		the title-only path must require a substantive title (>= 10 chars)."""
		ext = ExtractedMeta(first_page_text="It was a dark and stormy night, it rained.")
		prop = _Proposal("It", ["Neznámý Autor"])  # author absent, title too short
		passed, _ = verify_proposal(prop, ext)
		assert passed is False
		assert confirm_identity(prop, ext) is False


class TestIdentityAgrees:
	"""identity_agrees — do two identity-bearing objects (the final BookMeta
	vs the online EnrichedMeta) describe the same book? Gates review_writer's
	auto-verified pre-fill: the identity that was confirmed must survive the
	proposal projection."""

	def test_same_identity_agrees(self):
		a = _Proposal("Zastavený příval", ["Eduard Štorch"], isbn="978-80-204-0311-7")
		b = _Proposal("Zastavený příval", ["Eduard Štorch"], isbn="9788020403117")
		assert identity_agrees(a, b) is True

	def test_isbn10_and_isbn13_are_the_same_book(self):
		a = _Proposal(None, [], isbn="8020403116")
		b = _Proposal(None, [], isbn="9788020403117")
		assert identity_agrees(a, b) is True

	def test_different_isbn_disagrees(self):
		a = _Proposal(None, [], isbn="9788020403117")
		b = _Proposal(None, [], isbn="9788020403124")
		assert identity_agrees(a, b) is False

	def test_different_title_disagrees(self):
		a = _Proposal("Zastavený příval", ["Eduard Štorch"])
		b = _Proposal("Jiná kniha o dějinách", ["Eduard Štorch"])
		assert identity_agrees(a, b) is False

	def test_different_author_disagrees(self):
		a = _Proposal("Zastavený příval", ["Eduard Štorch"])
		b = _Proposal("Zastavený příval", ["Karel May"])
		assert identity_agrees(a, b) is False

	def test_one_sided_fields_cannot_contradict(self):
		"""A field present on one side only is skipped — e.g. an additive-only
		enrichment (no title/isbn of its own) vs the full metadata."""
		a = _Proposal(None, [], isbn=None)
		b = _Proposal("Zastavený příval", ["Eduard Štorch"], isbn="9788020403117")
		assert identity_agrees(a, b) is True

	def test_diacritics_and_case_tolerated(self):
		a = _Proposal("Zastaveny Prival", ["Eduard Storch"])
		b = _Proposal("Zastavený příval", ["Eduard Štorch"])
		assert identity_agrees(a, b) is True


class TestTitlePositionalPenalty:
	"""The positional check in _title_in_text: titles near the top of the text
	are trusted at full score; matches only deep in the text get penalised to
	suppress chapter-heading false positives from the LLM."""

	def _early_text(self, title: str) -> str:
		"""Return text where *title* appears in the early window (first ~700 chars)."""
		return title + " " + "x" * 800

	def _deep_text(self, title: str) -> str:
		"""Return text where *title* appears only DEEP (past 700 chars)."""
		return "A" * 800 + " " + title + " " + "x" * 200

	def test_title_in_early_window_passes(self):
		"""Title found near the top of the text: full score, passes verify."""
		from book_meta_fix.verifier import _title_in_text
		text = self._early_text("Zastavený příval")
		score = _title_in_text("Zastavený příval", text)
		assert score >= 0.80

	def test_chapter_heading_only_in_deep_text_is_penalised(self):
		"""A chapter heading ('Prolog') appearing only deep in the text gets a
		score penalty — it should land below the fuzzy_strong threshold."""
		from book_meta_fix.verifier import _title_in_text
		# 'Prolog' appears only at position >800 — typical chapter-heading scenario.
		text = "Tady začíná příběh: " + "a " * 380 + "Prolog " + "b " * 200
		score = _title_in_text("Prolog", text)
		# With penalty (0.80×) a deep exact match yields 0.80; we check it's
		# strictly below the 0.90 title-only-strong threshold so it won't
		# silently confirm identity.
		assert score <= 0.80

	def test_exact_substring_deep_gets_penalized(self):
		"""Exact substring match deep in the text receives the penalty too —
		LLM hallucinating a chapter heading creates an exact substring match,
		so it cannot bypass the positional check."""
		from book_meta_fix.verifier import _title_in_text
		text = "x" * 800 + " Zastavený příval " + "y" * 200
		score = _title_in_text("Zastavený příval", text)
		# 1.0 full score * 0.80 penalty = 0.80
		assert score == 0.80

	def test_chapter_heading_llm_proposal_fails_verify(self):
		"""Integration: LLM proposes a chapter heading found only in the body
		text as the title — verify_proposal should now reject it."""
		# 800 chars of body text, then 'Prolog' as a chapter heading.
		body_prefix = "Byl jednou jeden hrad " * 36  # ~792 chars
		text = body_prefix + "Prolog\n\nZačalo to ráno..." + " text " * 100
		ext = ExtractedMeta(first_page_text=text)
		# LLM hallucinated 'Prolog' as the title (common false positive).
		prop = _Proposal("Prolog", ["Neznámý Autor"])
		passed, fb = verify_proposal(prop, ext)
		assert passed is False
		assert "Prolog" in fb or "title" in fb.lower()
