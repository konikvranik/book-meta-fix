"""Verifier — compare DB metadata against the book's actual content.

Implements the cascading verification from the plan:
	1. Embedded metadata (EPUB content.opf / pdfinfo / ebook-meta)
	   present and matches DB  -> VERIFIED
	2. ISBN from content matches DB ISBN  -> VERIFIED
	3. Fuzzy title/author match on first-page text  -> VERIFIED or NEEDS_REVIEW

"First match wins" — the cheapest confirming signal stops the cascade.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from rapidfuzz import fuzz

from .extractors import ExtractedMeta, extract
from .isbn import canonicalize
from .models import BookMeta

log = logging.getLogger(__name__)

VerifyResult = Literal["VERIFIED", "MISMATCH", "NO_CONTENT", "UNCERTAIN"]


@dataclass
class Verification:
	"""Result of verifying a book's metadata against its content."""

	result: VerifyResult
	reason: str = ""
	# What the content actually says (for the report / proposed fix)
	extracted: ExtractedMeta | None = None
	# Strength of the match (0.0–1.0) when fuzzy comparison was used
	match_score: float = 0.0


def verify(meta: BookMeta, *, fuzzy_strong: float = 0.8, fuzzy_weak: float = 0.5) -> Verification:
	"""Verify *meta* against the book file's content.

	IMPORTANT: embedded metadata inside EPUB/PDF files is NOT treated as an
	independent confirmation. Calibre wrote the (possibly wrong) DB metadata
	back into the file at import time, so embedded == DB by construction.
	We only trust signals that come from the BOOK'S ACTUAL TEXT:
	  - an ISBN scanned from the first few pages (copyright page), or
	  - the title appearing (fuzzily) in the first-page text.
	Embedded metadata is still collected (as `extracted`) because it may be
	useful to the enricher as a *hint*, but it cannot VERIFIED a record.
	"""
	if not meta.primary_file:
		return Verification(result="NO_CONTENT", reason="no book file in folder")

	try:
		extracted = extract(meta.primary_file)
	except Exception as e:  # noqa: BLE001
		return Verification(result="NO_CONTENT", reason=f"extraction failed: {e}")

	if extracted.error and not extracted.first_page_text and not extracted.isbn and not extracted.isbn_from_text:
		return Verification(result="NO_CONTENT", reason=extracted.error, extracted=extracted)

	# --- Step 1: ISBN from content TEXT (not from embedded metadata) ---
	# isbn_from_text is scanned from the first pages — this is independent of
	# whatever calibre wrote into the file's metadata block.
	if extracted.isbn_from_text:
		content_isbn = extracted.isbn_from_text
		if meta.isbn and canonicalize(meta.isbn) == canonicalize(content_isbn):
			return Verification(
				result="VERIFIED",
				reason=f"ISBN found in content text and matches DB: {content_isbn}",
				extracted=extracted,
			)
		if not meta.isbn:
			return Verification(
				result="VERIFIED",
				reason=f"ISBN found in content text (DB had none): {content_isbn}",
				extracted=extracted,
			)
		# DB has ISBN, content has a different one — strong mismatch signal.
		return Verification(
			result="MISMATCH",
			reason=f"ISBN in content text differs from DB: DB={meta.isbn} content={content_isbn}",
			extracted=extracted,
		)

	# --- Step 2: fuzzy title match against first-page TEXT ---
	# This is the primary independent check. The title page of a book almost
	# always contains the title verbatim.
	if extracted.first_page_text and meta.title:
		score = _title_in_text(meta.title, extracted.first_page_text)
		if score >= fuzzy_strong:
			return Verification(
				result="VERIFIED",
				reason=f"title found in content text (fuzzy {score:.2f})",
				extracted=extracted,
				match_score=score,
			)
		if score >= fuzzy_weak:
			return Verification(
				result="UNCERTAIN",
				reason=f"title partially matches content text (fuzzy {score:.2f})",
				extracted=extracted,
				match_score=score,
			)
		# Below fuzzy_weak — title not found in the book's actual text.
		return Verification(
			result="MISMATCH",
			reason=f"title NOT found in content text (fuzzy {score:.2f})",
			extracted=extracted,
			match_score=score,
		)

	# --- Step 3: no text signal available ---
	# Could not extract any readable text from the file (scanned PDF, empty
	# EPUB, opaque PDB without ebook-meta). Fall back to embedded metadata as
	# a weak signal, but mark it UNCERTAIN because we can't independently
	# confirm it.
	if extracted.title or extracted.authors:
		return Verification(
			result="UNCERTAIN",
			reason="no readable text in content; only embedded metadata available (unreliable)",
			extracted=extracted,
		)

	return Verification(
		result="NO_CONTENT",
		reason="no usable signal in content (no text, no ISBN, no embedded metadata)",
		extracted=extracted,
	)


def _compare_embedded(meta: BookMeta, ext: ExtractedMeta, fuzzy_strong: float) -> tuple[bool, float, str]:
	"""Compare DB title/author against embedded metadata. Returns (match, score, reason)."""
	scores: list[float] = []
	reasons: list[str] = []

	# Title comparison (fuzzy, accent-insensitive)
	if ext.title and meta.title:
		s = _fuzzy_match(meta.title, ext.title)
		scores.append(s)
		reasons.append(f"title {s:.2f}")
		if s < fuzzy_strong:
			reasons.append(f"(DB {meta.title!r} vs embedded {ext.title!r})")

	# Author comparison: check if ANY DB author matches ANY embedded author
	if ext.authors and meta.authors:
		best_author = 0.0
		for db_a in meta.authors:
			for ext_a in ext.authors:
				s = _fuzzy_match(db_a, ext_a)
				best_author = max(best_author, s)
		scores.append(best_author)
		reasons.append(f"author {best_author:.2f}")

	if not scores:
		return False, 0.0, "no comparable fields in embedded metadata"

	avg = sum(scores) / len(scores)
	match = avg >= fuzzy_strong
	return match, avg, "; ".join(reasons)


def _title_in_text(title: str, text: str, window: int | None = 4000) -> float:
	"""Check whether *title* appears (fuzzily) within the first *window* chars of *text*.

	Returns the best fuzzy ratio found when sliding the title over the text.
	Uses partial_ratio which is well-suited for finding a short query string
	inside a longer text. ``window=None`` searches the WHOLE text — reserved
	for the broader content window, whose extra depth past the default cap
	is exactly the copyright/title-page material the deep-penalty logic
	below was built to judge.

	Positional penalty: titles appear at the TOP of a title page. If a match
	is found ONLY deep in the text (past the first _TITLE_EARLY_WINDOW chars),
	it is more likely a chapter heading or body-text fragment than a real
	title. We apply a 20 % penalty to deep-only scores to push typical
	chapter-heading false positives (score ~1.0 anywhere → 0.80 with penalty)
	right to the boundary of fuzzy_strong (0.8 default), where the author
	check usually rejects them as well. Exact substring matches still return
	1.0 regardless of depth — verbatim presence is trusted unconditionally.
	"""
	title_norm = _normalize(title)
	text_norm = _normalize(text if window is None else text[:window])
	if not title_norm or not text_norm:
		return 0.0

	early = text_norm[:_TITLE_EARLY_WINDOW]
	# Quick path: exact substring in the early window is trusted unconditionally.
	if title_norm in early:
		return 1.0

	# Fuzzy early match.
	early_score = fuzz.partial_ratio(title_norm, early) / 100.0 if early else 0.0
	if early_score >= 0.80:
		return early_score

	# Deep-only match: apply penalty to suppress chapter-heading selection.
	# Even an exact substring ('Prolog') gets the penalty if it's only deep.
	if title_norm in text_norm:
		full_score = 1.0
	else:
		full_score = fuzz.partial_ratio(title_norm, text_norm) / 100.0
	return full_score * _TITLE_DEEP_PENALTY


def _author_in_text(author: str, text: str, window: int | None = 4000) -> float:
	"""Check whether *author* appears (fuzzily) within the first *window* chars of *text*.

	Same technique as _title_in_text: partial_ratio of the normalised author
	against the normalised text. On CZ/SK title pages the author name usually
	appears verbatim (often in ALL-CAPS); the normalisation folds case and
	diacritics so 'BOŽENA NĚMCOVÁ' matches 'Božena Němcová'. ``window=None``
	searches the whole text (see _title_in_text — the broader window's depth).

	Before the fuzzy pass, the structural variant matcher
	(_author_variant_in_text) recognises the SAME author under a different
	printed format — initials for given names, dropped middle names, inflected
	or transliterated surnames — which plain partial_ratio scores below 0.8
	(measured: "A. Buškov" vs "Alexandr Buškov", "Brian Aldiss" vs "Brian
	Wilson Aldiss", "Strugačtí" vs "Strugackij").
	"""
	author_norm = _normalize(author)
	text_norm = _normalize(text if window is None else text[:window])
	if not author_norm or not text_norm:
		return 0.0
	if author_norm in text_norm:
		return 1.0
	if _author_variant_in_text(author_norm, text_norm):
		return 1.0
	# For short author names token_sort_ratio is slightly more tolerant of
	# reordered name parts (e.g. "May Karel" vs "Karel May"), but we still
	# anchor on partial_ratio so a name inside a longer line is found.
	return max(
		fuzz.partial_ratio(author_norm, text_norm),
		fuzz.token_sort_ratio(author_norm, text_norm[: len(author_norm) * 4 + 40]),
	) / 100.0


# --- Author-name format variants ---------------------------------------------
#
# The printed author credit in a book's text is the same person as the
# canonical metadata form, only formatted differently. Measured classes in
# the real library (accept-missing books whose identity would not confirm):
#
#   initials for given names   "A. Buškov"        vs "Alexandr Buškov"
#   dropped middle names       "Brian Aldiss"     vs "Brian Wilson Aldiss"
#   dropped middle initials    "Avram Davidson"   vs "Avram J. A. Davidson"
#   inflected surname (CZ)     "Baxterovi"        vs "Baxter"   (+ová/+ovi/+a…)
#   transliteration/typo       "Strugačtí"/"Andersen" vs "Strugackij"/"Anderson"
#   co-author with connective  "Arkadij a Boris Strugačtí" vs authors[0]
#
# The structural rules stay strict where it matters (survey-verified false
# positives): the FIRST given name must align (initial or full) — otherwise a
# surname mentioned in prose confirms anything ("Poe" inside a Feist novel,
# "kosek.cz" in a URL, the homonym Karel vs Josef Čapek), and a bare
# single-letter token counts as an initial only with its dot or right next to
# the surname — Czech prepositions ("s", "v", "u"…) otherwise pose as
# initials (measured: "…dura se s" near "Jamesi Baxterovi" fake-matched
# "Stephen").

# Single letters that are Czech/English function words, never initials.
_NOT_AN_INITIAL = frozenset("aioskvuz")

# Trailing/leading punctuation stripped from a text token before comparing.
_NAME_PUNCT = ".,;:!?()[]{}„\"«»—–-…*"


def _name_tokens(author_norm: str) -> tuple[list[str], str]:
	"""Split a folded author name into (given-name tokens, surname).

	Library convention is "Given Surname"; the comma form ("Bondy, Egon")
	carries the surname first. Middle names/initials are given-name tokens
	too — the alignment decides whether they were printed.
	"""
	if "," in author_norm:
		head, _, tail = author_norm.partition(",")
		givens = [t.strip(_NAME_PUNCT) for t in tail.split()]
		head_toks = [t.strip(_NAME_PUNCT) for t in head.split()]
		return [t for t in givens if t], (head_toks[-1] if head_toks else "")
	toks = [t.strip(_NAME_PUNCT) for t in author_norm.split()]
	toks = [t for t in toks if t]
	return toks[:-1], (toks[-1] if toks else "")


def _surname_hit(tok: str, surname: str) -> bool:
	"""Can text token *tok* stand for the metadata *surname*?

	Accepts the exact folded form, a Czech inflection suffix (Baxter →
	Baxterovi, Christie → Christieové) and — for reasonably long surnames
	only — a transliteration/typo variant (Strugačtí vs Strugackij and
	Andersen vs Anderson both measure ≥ 84). Short surnames skip the fuzzy
	tier: "Poe"→"poemu" scores 75, and a mentioned short surname is prose,
	not an author credit.
	"""
	if not tok or not surname:
		return False
	if tok == surname:
		return True
	if len(tok) > len(surname) and tok.startswith(surname) and len(tok) - len(surname) <= 4:
		return True
	if len(surname) >= 6 and tok[:1] == surname[:1] and fuzz.ratio(tok, surname) >= 74:
		return True
	return False


def _given_hit(tok: str, given: str, *, dotted: bool, adjacent: bool) -> bool:
	"""Can text token *tok* stand for the metadata *given* token?

	Exact, or an initial standing for the full token in either direction
	(metadata "A." vs text "Alexandr", metadata "P. D." vs text "Phyllis
	Dorothy"). A bare single letter (no dot) counts only directly beside the
	surname — elsewhere it is usually a preposition. Given names are NOT
	fuzzy- or inflection-matched: the homonym guard lives exactly here
	("Kevin" must never match "Poul"), and a Czech-inflected given in a
	dedication ("Jamesi" for "James") is a different person, not a format
	variant (measured: "mému synovci Jamesi Baxterovi" in a Stephen Baxter
	book).
	"""
	if not tok or not given:
		return False
	if tok == given:
		return True
	if len(given) == 1:
		# Metadata carries the initial, text the full name.
		return len(tok) >= 3 and tok[0] == given
	if len(tok) == 1 and tok == given[0]:
		return dotted or adjacent
	return False


def _author_variant_in_text(author_norm: str, text_norm: str) -> bool:
	"""Is the author credited in *text_norm* the same person, another format?

	Structural match: some token passes _surname_hit for the author's
	surname, and the given-name tokens align in order over the ±4 tokens
	around it. Returns a plain bool — the caller scores it 1.0 (the printed
	form of the same author is the author).
	"""
	givens, surname = _name_tokens(author_norm)
	if not surname or not givens:
		# A surname-only author gets no structural credit: a lone surname hit
		# is indistinguishable from a prose mention.
		return False
	tokens = []
	for raw in text_norm.split():
		# Split on '.' too: glued initials ("G.J.Arnaud") are one whitespace
		# token. A piece counts as dotted when a dot actually follows it
		# inside the raw token — a standalone "s" stays undotted (preposition,
		# never an initial).
		pieces = raw.split(".")
		for idx, piece in enumerate(pieces):
			core = piece.strip(_NAME_PUNCT)
			if not core:
				continue
			dotted = idx < len(pieces) - 1 or raw.endswith(".")
			tokens.append((core, dotted))
	n = len(tokens)
	for i, (core, _dot) in enumerate(tokens):
		if not _surname_hit(core, surname):
			continue
		lo = max(0, i - 4)
		ctx = []
		for k in list(range(lo, i)) + list(range(i + 1, min(n, i + 5))):
			c, d = tokens[k]
			ctx.append((c, d, k in (i - 1, i + 1)))
		# In-order alignment: every given token may claim a distinct context
		# token (or be skipped — a middle name the print dropped, or an extra
		# text token: a co-author, a dedication). The FIRST given token is
		# mandatory — skipping it would let any same-surname author confirm.
		pos = 0
		first_aligned = False
		for gi, given in enumerate(givens):
			found = -1
			for j in range(pos, len(ctx)):
				c, d, adj = ctx[j]
				if _given_hit(c, given, dotted=d, adjacent=adj):
					found = j
					break
			if found >= 0:
				pos = found + 1
				if gi == 0:
					first_aligned = True
			elif gi == 0:
				break
		if first_aligned:
			return True
	return False


# ---------------------------------------------------------------------------
# Content windows + identity confirmation
#
# The verifier trusts ONLY the book's actual readable text (page text), never
# embedded OPF metadata (which Calibre may have corrupted at import time). We
# search every available content window — first the first-page window, then the
# broader window (~30k chars deeper into the book). A title/author/ISBN printed
# on the title or copyright page a few pages in is just as much evidence as one
# on page 1; searching only the first page was the #1 reason correct LLM/online
# proposals got tagged low-confidence (the author name in particular is rarely
# in the extractable body text of a novel).
# ---------------------------------------------------------------------------


def _content_windows(extracted: ExtractedMeta) -> list[str]:
	"""The book's readable content-text windows, best-first.

	Returns the non-empty ``first_page_text`` then ``broader_text`` (de-duplicated
	by exact value). These are page text only — never embedded OPF metadata.
	"""
	windows: list[str] = []
	for attr in ("first_page_text", "broader_text"):
		text = getattr(extracted, attr, None) or ""
		if text.strip() and text not in windows:
			windows.append(text)
	return windows


# When the author is genuinely absent from the book's text (it is printed on the
# cover/copyright page, which extractors often can't read), a STRONG title match
# alone is still solid evidence we have the right book. Kept a notch above
# fuzzy_strong so a weak/partial title hit can't confirm identity on its own.
# A tiny generic title ('It', '2012') partial_ratio-matches almost any text, so
# _title_only_confirms additionally requires a substantive (>=10 normalised
# chars) title before trusting the title without the author.
_TITLE_ONLY_STRONG = 0.90
_TITLE_ONLY_MIN_LEN = 10

# Positional constants for _title_in_text. The title appears at the TOP of the
# title page, so we grant full score to matches in the first _TITLE_EARLY_WINDOW
# normalised characters. Matches found ONLY deeper in the text get a
# _TITLE_DEEP_PENALTY multiplier — enough to push a "Prolog" chapter heading
# (score 1.0 full-window) to 0.80, right at the edge of the default fuzzy_strong
# threshold, where it will typically also fail the author check.
_TITLE_EARLY_WINDOW: int = 700
_TITLE_DEEP_PENALTY: float = 0.80


def _title_only_confirms(title: str | None, windows: list[str], title_strong: float) -> bool:
	"""Does a STRONG title match alone confirm identity (author absent)?

	True when *title* is substantive (>= _TITLE_ONLY_MIN_LEN normalised chars)
	and fuzzy-matches some content window at >= *title_strong*. Short/generic
	titles are rejected here because partial_ratio matches them almost anywhere.
	"""
	if not title or len(_normalize(title)) < _TITLE_ONLY_MIN_LEN:
		return False
	return max((_title_in_text(title, t, window=None) for t in windows), default=0.0) >= title_strong


def _isbn_in_content(isbn: str, extracted: ExtractedMeta) -> bool:
	"""Is *isbn* corroborated by the book's content?

	True when it equals the ISBN scanned from the page text/embedded OPF, or
	when it appears verbatim (digits-only) anywhere in the book's readable
	text — first-page OR the broader window. A copyright-page ISBN a few pages
	in counts just as much as one on page 1. All signals are independent of
	the (possibly corrupt) library metadata.
	"""
	canon = canonicalize(isbn)
	if not canon:
		return False
	content_isbn = getattr(extracted, "isbn_from_text", None) or getattr(extracted, "isbn", None)
	if content_isbn and canonicalize(content_isbn) == canon:
		return True
	# ISBNs in text may keep hyphens/spaces; compare digits only. Search every
	# content window — the ISBN is often on the copyright page, not page 1.
	for text in _content_windows(extracted):
		if canon in re.sub(r"\D", "", text):
			return True
	return False


def identity_in_text(
	title: str,
	author: str | None,
	text: str | None,
	*,
	fuzzy_strong: float = 0.8,
	window: int | None = 4000,
) -> bool:
	"""Do *title* and *author* both appear (fuzzily) in the book's page text?

	*window* is the per-text search extent (None = whole text); see
	_title_in_text for why the broader window passes None.
	"""
	if not text or not title:
		return False
	if _title_in_text(title, text, window) < fuzzy_strong:
		return False
	if author and _author_in_text(author, text, window) < fuzzy_strong:
		return False
	return True


def confirm_identity(proposal, extracted, *, fuzzy_strong: float = 0.8, title_strong: float = _TITLE_ONLY_STRONG) -> bool:
	"""Is the identity in *proposal* confirmed by the book's *extracted* content?

	A POSITIVE gate: returns True only when content actually corroborates the
	identity. Three independent signals (any one suffices):
	  1. ISBN agreement — proposal.isbn is confirmed anywhere in the book's
	     content (strongest; works even without page text).
	  2. title + author both appear in a content window (first-page or broader).
	  3. the title appears STRONGLY (>= title_strong) on its own — the author is
	     frequently printed only on the cover/copyright page, which the extractor
	     can't read, so a tight title match alone is accepted as confirmation.

	Returns False when there is no content to check against (we cannot confirm
	what we cannot read) and when the content contradicts the proposal.
	"""
	windows = _content_windows(extracted)
	# Signal 1: ISBN agreement (anywhere in the book).
	proposal_isbn = getattr(proposal, "isbn", None)
	if proposal_isbn and _isbn_in_content(proposal_isbn, extracted):
		return True
	title = getattr(proposal, "title", None)
	if not windows or not title:
		return False
	authors = getattr(proposal, "authors", None) or []
	author = authors[0] if authors else None
	# Signal 2: title + author both in a content window. window=None: the
	# broader window is prefix-aligned with the first page, so a capped search
	# would only ever re-see page 1 — its whole value is the depth past the cap.
	for text in windows:
		if identity_in_text(title, author, text, fuzzy_strong=fuzzy_strong, window=None):
			return True
	# Signal 3: title alone, strongly (author genuinely absent from body text).
	if _title_only_confirms(title, windows, title_strong):
		return True
	return False


def identity_agrees(a, b, *, floor: float = 0.8) -> bool:
	"""Do two identity-bearing objects agree on who the book is?

	*a* and *b* may be a BookMeta or an EnrichedMeta (anything with
	``title`` / ``authors`` / ``isbn`` — read via getattr like confirm_identity
	does). Only fields BOTH sides carry are compared: title and first author
	fuzzily (>= *floor*), ISBN by canonical equality. A field present on one
	side only cannot contradict, so it is skipped.

	Used by review_writer's auto-verified gate: the pipeline confirmed the
	identity against the book's content AND an online record matched it, but
	the entry's FINAL identity is the post-proposal state — an extracted or
	C1-swap title may have overridden the online-confirmed one, and then the
	metadata that would be written is not the identity that was confirmed.
	"""
	ta, tb = getattr(a, "title", None), getattr(b, "title", None)
	if ta and tb and _fuzzy_match(ta, tb) < floor:
		return False
	aa = list(getattr(a, "authors", None) or [])
	ab = list(getattr(b, "authors", None) or [])
	if aa and ab and _fuzzy_match(aa[0], ab[0]) < floor:
		return False
	ia, ib = getattr(a, "isbn", None), getattr(b, "isbn", None)
	if ia and ib:
		ca, cb = canonicalize(ia), canonicalize(ib)
		if ca and cb and ca != cb:
			return False
	return True


def verify_proposal(proposal, extracted, *, fuzzy_strong: float = 0.8, title_strong: float = _TITLE_ONLY_STRONG) -> tuple[bool, str]:
	"""Validate a proposed title/author against the book's actual page text.

	*proposal* may be a ReconciledMeta (LLM) or EnrichedMeta (online) — both
	carry ``title`` and ``authors``. *extracted* is the ExtractedMeta produced
	by extract(); its content windows (first-page then broader) are the
	independent signal we trust.

	Returns ``(passed, feedback)``:
	  - (True, "")  — the proposal is corroborated by the content, or there is
                     no text to check against (image-only title page; accept as
                     low-confidence).
	  - (False, msg) — the content contradicts the proposal; *msg* is a short,
                       human/LLM-readable reason fed back into the next attempt.

	A proposal passes when ANY of these holds:
	  - title (>= fuzzy_strong) AND at least one author (>= fuzzy_strong) both
	    appear in a content window;
	  - the title matches STRONGLY (>= title_strong) on its own — the author is
	    often absent from the body text of a novel (printed only on the cover);
	  - an ISBN on the proposal is confirmed anywhere in the book's text.

	The ISBN, when present on both proposal and the text-scanned ISBN, is also
	compared exactly — a mismatch there is an instant fail (different book).
	"""
	windows = _content_windows(extracted)
	if not windows:
		# No readable text (image-only cover, scanned PDF, opaque format): we
		# have nothing to validate against. Accept rather than loop forever.
		return True, ""

	title = getattr(proposal, "title", None)
	authors = getattr(proposal, "authors", None) or []

	# ISBN mismatch check (independent, strong signal): a proposal ISBN that
	# DISAGREES with an ISBN scanned from the book's text is an instant fail.
	# (We only compare against isbn_from_text, not the embedded OPF isbn — that
	# one Calibre wrote and may itself be wrong.)
	proposal_isbn = getattr(proposal, "isbn", None)
	text_isbn = getattr(extracted, "isbn_from_text", None)
	if proposal_isbn and text_isbn:
		from .isbn import canonicalize

		if canonicalize(proposal_isbn) and canonicalize(text_isbn) and canonicalize(proposal_isbn) != canonicalize(text_isbn):
			return False, f"proposed ISBN {proposal_isbn} differs from the ISBN scanned from the book's text ({text_isbn}); the proposal is for a different book."

	# Best fuzzy score per field across EVERY content window (first-page then
	# broader), whole-window: the broader window is prefix-aligned with the
	# first page, so only the depth past the old 4000-char cap is new evidence
	# — a title/author on the title/copyright page counts too (that was the
	# point of adding it; searching page 1 was the #1 reason correct proposals
	# got tagged 'low'). The deep penalty keeps deep-only title hits honest.
	title_score = max((_title_in_text(title, t, window=None) for t in windows), default=0.0) if title else 0.0
	best_author = max((_author_in_text(a, t, window=None) for a in authors if a for t in windows), default=0.0) if authors else 0.0

	title_ok = title_score >= fuzzy_strong
	author_ok = best_author >= fuzzy_strong
	isbn_confirmed = bool(proposal_isbn and _isbn_in_content(proposal_isbn, extracted))

	if (title_ok and author_ok) or _title_only_confirms(title, windows, title_strong) or isbn_confirmed:
		return True, ""

	# Build feedback for the LLM self-correction loop: tell it which field(s)
	# the content contradicts, so the next attempt can correct them.
	reasons: list[str] = []
	if title and not title_ok:
		reasons.append(f"the title {title!r} is not found in the book's text (fuzzy {title_score:.2f}); read the first-page text carefully and use the title that actually appears there")
	if authors and not author_ok:
		reasons.append(f"none of the proposed authors {authors!r} appear in the book's text (best fuzzy {best_author:.2f}); the real author's name is printed on the title page")
	if not reasons:
		# Both fields empty / unusable — nothing to validate, accept.
		return True, ""
	return False, "; ".join(reasons)


def _fuzzy_match(a: str, b: str) -> float:
	"""Fuzzy similarity of two strings (accent-insensitive, case-insensitive)."""
	return fuzz.token_sort_ratio(_normalize(a), _normalize(b)) / 100.0


def _normalize(s: str) -> str:
	"""Lowercase + strip accents + collapse whitespace."""
	import re

	repl = str.maketrans(
		"áčďéěíňóřšťúůýžôäüÁČĎÉĚÍŇÓŘŠŤÚŮÝŽÔÄÜ",
		"acdeeinorstuuyzoauACDEEINORSTUUYZOAU",
	)
	return re.sub(r"\s+", " ", s.translate(repl).lower().strip())


# --- Content extraction helpers ----------------------------------------------
# These are content primitives shared by the pipeline and the unified
# classifier (classify.py): robust extraction with sibling-format fallback,
# and a heuristic for whether extracted text is usable downstream.


def has_usable_text(text: str | None) -> bool:
	"""Does *text* contain enough readable content to reason over?

	Rejects:
	  - None / empty
	  - Pure CSS (e.g. 'Cover @page {padding: 0pt; ...}')
	  - Pure HTML tags / boilerplate with no prose
	  - Very short snippets (< 80 chars of actual prose)

	The check is heuristic and fast — it strips obvious non-prose and measures
	the remaining length. It is the #1 cost saver for the LLM path and the gate
	for extraction fallback.
	"""
	if not text or len(text) < 80:
		return False
	# Strip CSS blocks (common in EPUB cover pages)
	clean = re.sub(r"@page\s*\{[^}]*\}", " ", text)
	clean = re.sub(r"[{};]", " ", clean)
	# Count word-like tokens (sequences of 3+ letters)
	words = re.findall(r"[A-Za-zÁ-ž]{3,}", clean)
	return len(words) >= 8


def safe_extract(meta: BookMeta) -> ExtractedMeta | None:
	"""Extract content from the book, falling back to sibling formats.

	The primary file (highest-preference format) is tried first. If it yields no
	usable page text — a corrupt epub (bad zip), an image-only PDF, a .doc whose
	catdoc output is empty — the book's other formats are tried until one yields
	usable text. This recovers content when the primary format is broken but a
	sibling (often the calibre source format) is fine.

	Multi-format extraction is slower, so siblings are only tried when the
	primary failed — never all formats up front.
	"""
	if not meta.primary_file:
		return None

	def _try(path: str) -> ExtractedMeta | None:
		try:
			return extract(path)
		except Exception as e:  # noqa: BLE001
			log.debug("extract failed for %s: %s", path, e)
			return None

	primary = _try(meta.primary_file)
	if primary is not None and has_usable_text(primary.first_page_text):
		return primary

	# Fallback: try sibling formats for usable page text.
	primary_suffix = Path(meta.primary_file).suffix.lower()
	folder = Path(meta.path)
	if folder.is_dir() and meta.formats:
		for entry in sorted(folder.iterdir(), key=lambda e: e.name):
			if not entry.is_file():
				continue
			suf = entry.suffix.lower()
			if suf == primary_suffix or suf not in meta.formats:
				continue
			other = _try(str(entry))
			if other is not None and has_usable_text(other.first_page_text):
				log.info("extraction fallback %s -> %s for %s", primary_suffix, suf, meta.path)
				return other

	# No sibling yielded usable text either; return whatever the primary gave
	# (it may still carry embedded metadata even without page text).
	return primary


# --- Identity acquisition ----------------------------------------------------
# A content-verified identity (ISBN or title+author) anchors both online lookup
# and the "identified MISSING_* book is acceptable" classification rule.


@dataclass
class IdentityResult:
	"""A book identity confirmed against the book's own content.

	Either an ISBN (strongest) or a title+authors pair, plus the source level
	that established it. Used to anchor online metadata lookup so online sources
	fill data for a KNOWN book rather than guess identity.
	"""

	isbn: str | None = None
	title: str | None = None
	authors: list[str] = field(default_factory=list)
	year: int | None = None
	source: str = ""  # 'content-isbn' | 'metadata' | 'extractor'

	@property
	def has_isbn(self) -> bool:
		return bool(self.isbn)

	@property
	def has_title_author(self) -> bool:
		return bool(self.title) and bool(self.authors)


def acquire_identity(meta: BookMeta, extracted: ExtractedMeta | None) -> IdentityResult | None:
	"""Acquire a content-verified identity for the book (no network).

	Cascade (first verified wins):
	  1. content-ISBN — scanned from the book's text/embedded OPF (strongest;
	     self-grounded, validated by ISBN check digit).
	  2. metadata ISBN — confirmed present in the content.
	  3. metadata title+author — confirmed present in the page text.
	  4. offline extractor (text_meta) title+author — mined from the page text.

	Steps 3-4 search EVERY content window whole-text (first page + broader):
	the broader window is prefix-aligned with the first page, so its only
	added evidence sits past the 4000-char search cap — exactly the
	title/copyright-page material acquire_identity used to miss while
	confirm_identity already accepted it (the two gates must not disagree).

	Returns None when no identity can be confirmed against content (→ LLM).

	Note: per the agreed identification policy, an author+title confirmed
	against the content is sufficient (year is carried as data, never required);
	step 3 is the rule the unified classifier reuses to treat an identified
	MISSING_* book as acceptable.
	"""
	if extracted is None:
		return None

	content_isbn = extracted.isbn_from_text or extracted.isbn

	# 1. Content-ISBN (strongest, content-grounded).
	if content_isbn:
		return IdentityResult(isbn=content_isbn, source="content-isbn")

	# 2. Metadata ISBN, verified against content.
	if meta.isbn and _isbn_in_content(meta.isbn, extracted):
		return IdentityResult(isbn=meta.isbn, source="metadata")

	windows = _content_windows(extracted)

	# 3. Metadata title+author, verified against content.
	if meta.title and meta.authors:
		for text in windows:
			if identity_in_text(meta.title, meta.authors[0], text, window=None):
				return IdentityResult(title=meta.title, authors=list(meta.authors), year=meta.year, source="metadata")

	# 4. Offline extractor (text_meta) — content-grounded.
	ext_title = extracted.title_from_text
	ext_authors = extracted.authors_from_text or []
	if ext_title and ext_authors:
		for text in windows:
			if identity_in_text(ext_title, ext_authors[0], text, window=None):
				return IdentityResult(title=ext_title, authors=list(ext_authors), year=extracted.year_from_text, source="extractor")

	return None
