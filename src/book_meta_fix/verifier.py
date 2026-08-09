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
from dataclasses import dataclass
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


def _title_in_text(title: str, text: str, window: int = 4000) -> float:
	"""Check whether *title* appears (fuzzily) within the first *window* chars of *text*.

	Returns the best fuzzy ratio found when sliding the title over the text.
	Uses partial_ratio which is well-suited for finding a short query string
	inside a longer text.
	"""
	title_norm = _normalize(title)
	text_norm = _normalize(text[:window])
	if not title_norm or not text_norm:
		return 0.0
	# Quick path: exact substring (after normalization)
	if title_norm in text_norm:
		return 1.0
	# Fuzzy: use partial_ratio which handles substrings well
	return fuzz.partial_ratio(title_norm, text_norm) / 100.0


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
