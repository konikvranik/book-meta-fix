"""Unified book classification — the single source of truth for "is this book
OK or does it need attention?".

Every command that decides a book's disposition (``report``, ``organize``,
``epubgen``) and the pipeline's identity gate call the functions in this module,
so the classification rules cannot drift between call sites. The rule that
mattered most historically — an identified MISSING_* book (author+title verified
against the content) is acceptable, not broken — lives here exactly once and is
applied everywhere.

The classifier layers two content-aware reclassifications on top of the raw
structural detector verdict:

  1. *OK audit* (opt-in via ``verify_ok``): an OK book whose content
     contradicts its metadata is demoted to NEEDS_REVIEW. Off by default, like
     ``analyze``'s ``--verify-ok``.
  2. *Identity gate* (on by default via ``accept_missing``): a MISSING_ISBN /
     MISSING_YEAR / MISSING_COVER book whose identity is confirmable from its
     content — and which carries no co-occurring NEEDS_REVIEW diagnosis — is
     promoted to OK. Per the agreed identification policy, an author+title
     confirmed against the content is sufficient; the year is never required.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .detectors import detect
from .extractors import ExtractedMeta
from .models import BookMeta, Diagnosis, Verdict
from .verifier import acquire_identity, safe_extract, verify

log = logging.getLogger(__name__)

# Primary categories that are auto-acceptable once the book is identified.
# (MISSING_COVER is AUTO_FIXABLE, so a co-occurring one does NOT block the gate;
# only a co-occurring NEEDS_REVIEW diagnosis does — see is_acceptable_missing.)
_MISSING_CATEGORIES: tuple[str, ...] = ("MISSING_ISBN", "MISSING_YEAR", "MISSING_COVER")


@dataclass
class Classification:
	"""The result of classifying one book.

	``diag`` is the raw structural diagnosis (its ``category`` is preserved even
	when the effective ``verdict`` is promoted, so e.g. a MISSING_ISBN book that
	was identified still reports as MISSING_ISBN — it just routes to the OK
	path). ``identified`` records whether the identity gate fired, so summaries
	can show "identified (accepted)" separately from a clean OK.
	"""

	diag: Diagnosis
	verdict: Verdict
	identified: bool = False
	extracted: ExtractedMeta | None = None


def is_acceptable_missing(diag: Diagnosis) -> bool:
	"""Is *diag* a MISSING_* primary with no co-occurring NEEDS_REVIEW diagnosis?

	This is the shared structural condition for the identity gate — the same
	rule the pipeline applies at its accept-missing gate. A book that is also
	flagged NEEDS_REVIEW (e.g. a generated cover, C11) is NOT auto-acceptable:
	that co-occurring problem blocks acceptance and keeps the book in review.
	"""
	return (
		diag.category in _MISSING_CATEGORIES
		and not any(d.verdict == Verdict.NEEDS_REVIEW for d in diag.additional)
	)


def classify(
	meta: BookMeta,
	*,
	accept_missing: bool = True,
	verify_ok: bool = False,
	strict_verify: bool = True,
) -> Classification:
	"""Classify *meta* into a routing disposition.

	Parameters mirror the knobs the CLI commands expose, with defaults that
	match ``report`` (the canonical baseline): detector verdict + identity gate,
	no OK-audit unless ``verify_ok`` is set.

	- ``accept_missing``: apply the identity gate (identified MISSING_* → OK).
	  When False, the classifier is pure detector verdict (the historic fast
	  ``report`` behaviour) and does NO content reads.
	- ``verify_ok``: audit OK books against their content, demoting a MISMATCH
	  (or, with ``strict_verify``, an UNCERTAIN) to NEEDS_REVIEW.
	"""
	diag = detect(meta)
	verdict = diag.verdict
	extracted: ExtractedMeta | None = None

	# 1. OK audit (opt-in). verify() reads the book file; keep its extracted
	#    content so the identity gate below can reuse it without a second read.
	if verdict == Verdict.OK and verify_ok and meta.primary_file:
		try:
			verification = verify(meta)
		except Exception as e:  # noqa: BLE001
			log.debug("verify failed for %s: %s", meta.path, e)
			verification = None
		if verification is not None:
			extracted = verification.extracted
			mismatch = verification.result == "MISMATCH"
			uncertain = verification.result == "UNCERTAIN" and strict_verify
			if mismatch or uncertain:
				verdict = Verdict.NEEDS_REVIEW

	# 2. Identity gate. An identified MISSING_* book is acceptable (routes to
	#    the OK path) — author+title confirmed against the content is sufficient
	#    identification by policy. Only MISSING_* primaries are extracted for
	#    this; other broken books keep their detector verdict and need no read.
	identified = False
	if accept_missing and is_acceptable_missing(diag):
		if extracted is None and meta.primary_file:
			extracted = safe_extract(meta)
		if extracted is not None and acquire_identity(meta, extracted) is not None:
			identified = True
			verdict = Verdict.OK

	return Classification(diag=diag, verdict=verdict, identified=identified, extracted=extracted)
