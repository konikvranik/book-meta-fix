"""Pipeline orchestration: detect -> extract -> verify -> enrich -> review/write.

The pipeline glues together all phases and exposes high-level functions used
by the CLI:
	run_pipeline()   - full scan+detect+extract+verify+enrich, returns list of (meta, diag, verif, enriched)
	generate_review() - emit review.yaml for NEEDS_REVIEW items
	apply_review()   - parse a (human-edited) review.yaml and write changes

Cost-aware fix strategy for NEEDS_REVIEW books (cheap first, LLM last):
	1. Extract content ONCE per book (cached) -> extracted: {title, authors, isbn, first_page}
	2. Deterministic fixes from extracted:
	   2a. extracted.isbn or isbn_from_text -> online lookup (OpenLibrary/GB)  [cached, ~1s]
	   2b. extracted.title is cleaner than DB title -> use it
	   2c. extracted.authors are cleaner than DB -> use them
	3. If (2) produced a useful proposal -> done (NEEDS_REVIEW with proposed filled)
	4. Else LLM (only for categories in llm_categories) -> proposed
	5. Else proposed=null (human review)
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .detectors import all_diagnoses, detect as detect_fn
from .enrichers import Enricher
from .extractors import ExtractedMeta, extract
from .library import Cache, scan_library
from .models import BookMeta, Confidence, Diagnosis, Verdict
from .review import _COVER_CATEGORIES, build_review, parse_review, prune_review
from .verifier import confirm_identity, identity_in_text, verify
from .writers import write_book_meta

log = logging.getLogger(__name__)


def run_pipeline(
	library: Path,
	cache: Cache | None = None,
	enricher: Enricher | None = None,
	*,
	skip_enrich: bool = False,
	skip_verify: bool = False,
	llm_provider: Any = None,
	llm_categories: tuple[str, ...] = ("ALL",),
	limit: int | None = None,
	workers: int = 10,
	progress_callback: Any = None,
	only_needs_review: bool = True,
	review_writer: Any = None,
	verify_ok: bool = False,
	strict_verify: bool = True,
	llm_loop: bool = True,
	stats: dict | None = None,
) -> list[tuple[BookMeta, "Diagnosis", "Verification | None", "EnrichedMeta | None"]]:  # noqa: F821
	"""Run the full pipeline over the whole library.

	Returns a list of (meta, diagnosis, verification, enriched) tuples.

	*stats* (optional): if a dict is passed in, it is populated with a
	breakdown of how books were processed (offline/online/LLM fix sources,
	skips, errors, cover detections). The caller can then print a summary
	table. When None (default), an internal dict is used and discarded.

	*only_needs_review* (default True): skip books that the detector already
	classifies as OK or VERIFIED. This makes the pipeline incremental —
	repeated runs only touch books that still need work. Set to False to
	force a full re-scan (e.g. when detector rules change). Overridden by
	*verify_ok*: when verify_ok is True, OK books are kept so they can be
	checked against their content (audit mode).

	*verify_ok* (default False): when True, OK books are verified against
	their content via :func:`verify`. A MISMATCH (or an UNCERTAIN, when
	*strict_verify* is True) reclassifies the book to NEEDS_REVIEW and runs
	the same enrichment/LLM fix path as for detector-flagged books. This is
	an audit mode — it reads every OK book's content, so it is much slower
	than the default incremental run.

	*strict_verify* (default True): only meaningful with *verify_ok*. When
	True, UNCERTAIN (fuzzy title match 0.5–0.8) is also treated as a
	mismatch and reclassified; when False only a clear MISMATCH (< 0.5) is.

	If *limit* is given, it caps the number of books processed AFTER the
	only_needs_review filter. So `--limit 500` means "process at most 500
	books that still need review", not "the first 500 books in the library".

	*workers* controls parallelism: each book's expensive I/O (content
	extraction, online lookup, LLM call) runs in a ThreadPoolExecutor with
	this many workers. Output order matches input order.
	*progress_callback* (if given) is called with (i, total) after each book.

	*review_writer* (optional): if a ReviewWriter is supplied, each processed
	result is also streamed to review.yaml via ``review_writer.submit()`` as it
	completes (Unix-pipe style), instead of writing the whole file at the end.
	"""
	all_books = scan_library(library, cache=cache)
	# Apply the detector cheaply to filter out already-OK books (incremental).
	# This is fast (no I/O — just regex/heuristics over metadata).
	# verify_ok overrides this: OK books must be kept so they can be verified
	# against their content (audit mode).
	if only_needs_review and not verify_ok:
		books = [b for b in all_books if detect_fn(b).verdict != Verdict.OK]
		log.info(
			"pipeline: %d total books, %d already OK -> %d to process",
			len(all_books), len(all_books) - len(books), len(books),
		)
	else:
		books = list(all_books)
		if verify_ok:
			log.info(
				"pipeline: %d total books, verify-ok mode (OK books kept for content check)",
				len(all_books),
			)
	if limit is not None:
		books = books[:limit]
	total = len(books)
	# Per-source fix counters, filled as books are processed. When the caller
	# passes a stats dict we merge into it (so the CLI can print a summary
	# table); otherwise we keep a throwaway local dict for the log line.
	_stats = {
		"ok": 0, "needs_review": 0, "det_fixed": 0, "online_fixed": 0,
		"llm_fixed": 0, "llm_flash_fixed": 0, "llm_final_fixed": 0, "llm_low_confidence": 0,
		"llm_skipped_no_text": 0, "llm_no_result": 0, "llm_error": 0,
		"unfixed": 0, "errors": 0, "content_mismatch": 0,
		"covers_generated": 0, "covers_missing": 0,
		# Online fix source breakdown (sub-rows of online_fixed).
		"online_databazeknih": 0, "online_openlibrary": 0, "online_google_books": 0,
		# Offline fix source breakdown (sub-rows of det_fixed).
		"offline_content": 0, "offline_embedded": 0,
		"total": total,
	}
	if stats is not None:
		# Seed any missing keys so the caller's dict always has the full set,
		# while preserving any caller-provided starting values.
		for k, v in _stats.items():
			stats.setdefault(k, v)
		stats_ref = stats
	else:
		stats_ref = _stats

	# Per-book work closure. The shared enricher/llm are thread-safe:
	# - openai client uses an internal httpx Client (thread-safe)
	# - requests.Session is thread-safe for GETs
	# - SQLite Enricher cache serializes via its connection's check_same_thread=False
	def _process(meta: BookMeta):
		return _process_book(
			meta, enricher=enricher, skip_enrich=skip_enrich, skip_verify=skip_verify,
			llm_provider=llm_provider, llm_categories=llm_categories, stats=stats_ref,
			verify_ok=verify_ok, strict_verify=strict_verify, llm_loop=llm_loop,
		)

	def _process_safe(meta: BookMeta):
		"""Wrap _process so one book's failure never aborts the whole run.

		On exception we log, count it, and return a minimal tuple so the book
		still appears in the review report (with NEEDS_REVIEW diagnosis and no
		proposal). This preserves any LLM tokens already spent on other books:
		they are reflected in the report rather than thrown away by a crash.
		"""
		try:
			return _process(meta)
		except Exception as e:  # noqa: BLE001
			stats_ref["errors"] += 1
			log.exception("unhandled error processing %s (calibre_id=%s); recording as NEEDS_REVIEW with no proposal", meta.path, meta.calibre_id)
			# Build a NEEDS_REVIEW diagnosis so the book still lands in the report.
			diag = Diagnosis(category="ERROR", reason=f"processing failed: {e}", verdict=Verdict.NEEDS_REVIEW, confidence=Confidence.HIGH)
			return (meta, diag, None, None)

	def _stream(result: tuple | None) -> tuple | None:
		"""Push a result to the streaming writer (if any), then return it.

		None results (catastrophic worker failure) are not streamed.
		"""
		if review_writer is not None and result is not None:
			try:
				review_writer.submit(result)
			except Exception:  # noqa: BLE001
				log.debug("review writer submit failed (non-fatal)", exc_info=True)
		return result

	# No point spawning a pool of 10 if we only have 3 books.
	n_workers = max(1, min(workers, total))
	interrupted = False
	if n_workers == 1:
		# Serial path — keeps stack traces readable for debugging
		results = []
		for i, meta in enumerate(books):
			try:
				results.append(_stream(_process_safe(meta)))
			except KeyboardInterrupt:
				interrupted = True
				log.warning("interrupted by user (Ctrl-C) after %d/%d books; keeping partial results", i, total)
				break
			if progress_callback is not None:
				progress_callback(i + 1, total)
	else:
		results = []
		with ThreadPoolExecutor(max_workers=n_workers) as pool:
			# Submit all, then read results AS THEY COMPLETE (as_completed).
			# Reading futures in submit order blocked the progress bar on the
			# slowest in-flight book: 9 cheap books would finish while we
			# waited on the 1 slow LLM call, then the bar jumped 1 -> 10 at
			# once. as_completed updates the bar the instant each book is done,
			# which is what the user wants to see. Review.yaml order becomes
			# completion-order (not input-order); that's fine because each
			# entry carries its id and the file is a multi-doc stream.
			futures = {pool.submit(_process_safe, meta): meta for meta in books}
			done = 0
			try:
				for fut in as_completed(futures):
					try:
						results.append(_stream(fut.result()))
					except KeyboardInterrupt:
						interrupted = True
						break
					except Exception as e:  # noqa: BLE001
						stats_ref["errors"] += 1
						log.exception("worker future failed unexpectedly: %s", e)
						results.append(None)
					finally:
						done += 1
						if progress_callback is not None:
							progress_callback(done, total)
			except KeyboardInterrupt:
				interrupted = True
			if interrupted:
				log.warning("interrupted by user (Ctrl-C) after %d/%d books; cancelling pending work, keeping partial results", done, total)
				for f in futures:
					f.cancel()

	# Drop any None placeholders from a catastrophic worker failure (kept above
	# only to preserve order/count); generate_review iterates results as-is.
	results = [r for r in results if r is not None]

	if interrupted:
		log.warning("pipeline interrupted: returning %d partial results (of %d books) for review", len(results), total)
	log.info(
		"pipeline: %d ok, %d needs_review (content_mismatch=%d, det=%d, online=%d, llm_flash=%d, llm_final=%d, llm_low=%d, llm_other=%d, llm_skipped=%d, llm_no_result=%d, unfixed=%d, covers_gen=%d, covers_missing=%d, errors=%d)%s",
		stats_ref["ok"], stats_ref["needs_review"], stats_ref["content_mismatch"], stats_ref["det_fixed"], stats_ref["online_fixed"],
		stats_ref["llm_flash_fixed"], stats_ref["llm_final_fixed"], stats_ref["llm_low_confidence"], stats_ref["llm_fixed"],
		stats_ref["llm_skipped_no_text"], stats_ref["llm_no_result"], stats_ref["unfixed"],
		stats_ref["covers_generated"], stats_ref["covers_missing"], stats_ref["errors"],
		" [INTERRUPTED]" if interrupted else "",
	)
	return results


def _process_book(
	meta: BookMeta,
	*,
	enricher: Enricher | None,
	skip_enrich: bool,
	skip_verify: bool,
	llm_provider: Any,
	llm_categories: tuple[str, ...],
	stats: dict,
	verify_ok: bool = False,
	strict_verify: bool = True,
	llm_loop: bool = True,
) -> tuple[BookMeta, "Diagnosis", "Verification | None", "EnrichedMeta | None"]:  # noqa: F821
	"""Process one book end-to-end. Thread-safe (no shared mutable state except *stats*).

	*verify_ok*: when True, OK books (detectors found nothing wrong) are still
	checked against the book's content via :func:`verify`. A MISMATCH (or, with
	*strict_verify*, also an UNCERTAIN) reclassifies the book to NEEDS_REVIEW
	so the same enrichment/LLM fix path runs as for detector-flagged books.
	"""
	diag = detect_fn(meta)
	verification = None
	enriched = None
	# Count cover-specific diagnoses for the pipeline summary. Iterate all
	# diagnoses (primary + additional) so a book whose primary issue is, say,
	# C2 but that also has a generated cover is still counted here — matching
	# what apply will actually do.
	for d in all_diagnoses(diag):
		if d.category == "C11":
			stats["covers_generated"] += 1
		elif d.category == "MISSING_COVER":
			stats["covers_missing"] += 1
	# Carries an already-extracted ExtractedMeta into the fix path, so a book
	# that was verified (and reclassified) doesn't get extracted a second time.
	preextracted: ExtractedMeta | None = None

	# --- OK books: optionally verify against content, else stay OK ---
	if diag.verdict == Verdict.OK and meta.primary_file:
		if verify_ok and not skip_verify:
			try:
				verification = verify(meta)
			except Exception as e:  # noqa: BLE001
				log.debug("verify failed for %s: %s", meta.path, e)
			if verification is not None:
				mismatch = verification.result == "MISMATCH"
				uncertain = verification.result == "UNCERTAIN" and strict_verify
				if mismatch or uncertain:
					# Reclassify OK -> NEEDS_REVIEW so the fix path below runs.
					# Keep the extracted content from verify() to reuse it.
					# Preserve additional diagnoses (e.g. a generated cover found
					# by detect) so they're still reported alongside the mismatch.
					preextracted = verification.extracted
					saved_additional = list(diag.additional)
					diag = Diagnosis(
						category="CONTENT_MISMATCH",
						reason=f"metadata neodpovídají obsahu ({verification.result}: {verification.reason})",
						confidence=Confidence.HIGH,
						verdict=Verdict.NEEDS_REVIEW,
					)
					diag.additional = saved_additional
					stats["content_mismatch"] += 1
				else:
					# VERIFIED / NO_CONTENT / non-strict UNCERTAIN -> stays OK.
					stats["ok"] += 1
					return (meta, diag, verification, None)
			else:
				# verify() failed — treat as OK (can't say it's broken).
				stats["ok"] += 1
				return (meta, diag, None, None)
		else:
			stats["ok"] += 1
			return (meta, diag, None, None)

	is_needs_review = diag.verdict == Verdict.NEEDS_REVIEW or diag.category in ("MISSING_ISBN", "MISSING_YEAR", "MISSING_COVER")

	# --- NEEDS_REVIEW books: try cheap fixes first, LLM last ---
	if is_needs_review:
		stats["needs_review"] += 1
		# Extract content once (reused by both deterministic fixes and LLM).
		# Prefer content already extracted during verify() to avoid a second
		# read of the book file.
		extracted = preextracted if preextracted is not None else _safe_extract(meta)

		# Step 2a-2c: deterministic fixes from extracted content + online lookup
		if extracted is not None:
			try:
				enriched = _try_deterministic_fix(meta, diag, extracted, enricher, skip_enrich)
			except Exception as e:  # noqa: BLE001
				log.debug("deterministic fix failed for %s: %s", meta.path, e)
				enriched = None
			if enriched is not None:
				# 'embedded' (OPF compare) and 'content' (text_meta from page
				# text) are both offline/deterministic; the rest are online.
				# Bucket by fix source for the post-report stats table.
				if enriched.source in ("embedded", "content"):
					stats["det_fixed"] = stats.get("det_fixed", 0) + 1
					if enriched.source == "content":
						stats["offline_content"] = stats.get("offline_content", 0) + 1
					else:
						stats["offline_embedded"] = stats.get("offline_embedded", 0) + 1
				else:
					stats["online_fixed"] = stats.get("online_fixed", 0) + 1
					key = f"online_{enriched.source}"  # databazeknih/openlibrary/google_books
					stats[key] = stats.get(key, 0) + 1

		# Step 4: LLM fallback only if deterministic + online failed AND the
		# book has usable first-page text (LLM cannot work without it).
		# llm_categories gates WHICH books the LLM is asked about: each book is
		# one request that returns all fields at once (cost is per-book, not
		# per-category). 'ALL' expands to every category except C9 (legitimate
		# anonyms like the Bible — an LLM-invented author there would be wrong).
		if enriched is None and llm_provider is not None and _llm_wants(diag.category, llm_categories):
			# Pre-filter: skip LLM if no first-page text or only CSS noise.
			# This is the #1 cost saver — books with empty/CSS-only content
			# would waste an API call for nothing.
			first_page = extracted.first_page_text if extracted is not None else None
			if not _has_usable_text(first_page):
				stats["llm_skipped_no_text"] += 1
			else:
				def _llm_attempt(ev):
					"""One reconcile_loop attempt. Returns (enriched, llm_src) or (None, None)."""
					reconciled, src = _llm_reconcile_with_loop(llm_provider, ev, extracted, loop=llm_loop)
					if reconciled is None or not _reconciled_is_useful(reconciled, meta):
						return None, None
					em = _reconciled_to_enriched(reconciled, source=src)
					# The LLM result passed verify_proposal inside the loop;
					# confirm_identity is the matching positive gate that sets
					# identity_confirmed (a verified identity change auto-accepts).
					em.identity_confirmed = confirm_identity(em, extracted)
					return em, src

				def _bucket(src):
					if src in ("llm:flash", "llm:loop"):
						stats["llm_flash_fixed"] += 1
					elif src == "llm:high":
						stats["llm_final_fixed"] += 1
					elif src == "llm:low":
						stats["llm_low_confidence"] += 1
					else:
						stats["llm_fixed"] += 1

				try:
					evidence = _build_llm_evidence(meta, diag, extracted)
					# Attempt 1: first-page text.
					enriched, llm_src = _llm_attempt(evidence)
					# Attempt 2 (only if the first page wasn't enough): retry with a
					# broader text window (title/author aren't always on page 1).
					if enriched is None and extracted is not None:
						broader = getattr(extracted, "broader_text", None)
						first = extracted.first_page_text
						if broader and _has_usable_text(broader) and (not first or len(broader) > len(first)):
							enriched, llm_src = _llm_attempt({**evidence, "first_page_text": broader})
							if enriched is not None:
								stats["llm_broader_fixed"] = stats.get("llm_broader_fixed", 0) + 1
					if enriched is not None:
						_bucket(llm_src)
					else:
						stats["llm_no_result"] += 1
				except Exception as e:  # noqa: BLE001
					log.debug("LLM reconcile failed for %s: %s", meta.path, e)
					stats["llm_error"] += 1

		if enriched is None:
			stats["unfixed"] += 1

	return (meta, diag, verification, enriched)


def _safe_extract(meta: BookMeta) -> ExtractedMeta | None:
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
	if primary is not None and _has_usable_text(primary.first_page_text):
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
			if other is not None and _has_usable_text(other.first_page_text):
				log.info("extraction fallback %s -> %s for %s", primary_suffix, suf, meta.path)
				return other

	# No sibling yielded usable text either; return whatever the primary gave
	# (it may still carry embedded metadata even without page text).
	return primary


# Categories that 'ALL' deliberately excludes: a known-good state where sending
# the book to the LLM would risk inventing a wrong author rather than fixing one.
_LLM_ALL_EXCLUDE = frozenset({"C9"})


def _llm_wants(category: str, llm_categories: tuple[str, ...]) -> bool:
	"""Should a book in *category* be sent to the LLM?

	- Explicit category list (e.g. ('C1','C2')): category must be in the tuple.
	- 'ALL': every category EXCEPT those in _LLM_ALL_EXCLUDE (currently C9 —
	  legitimate anonyms like the Bible/Koran; an LLM there would fabricate an
	  author). 'ALL' may be combined with explicit inclusions/exclusions.
	"""
	if "ALL" in llm_categories:
		return category not in _LLM_ALL_EXCLUDE
	return category in llm_categories


def _has_usable_text(text: str | None) -> bool:
	"""Does *text* contain enough readable content for the LLM to work with?

	Rejects:
	  - None / empty
	  - Pure CSS (e.g. 'Cover @page {padding: 0pt; ...}')
	  - Pure HTML tags / boilerplate with no prose
	  - Very short snippets (< 80 chars of actual prose)

	The check is heuristic and fast — it strips obvious non-prose and measures
	the remaining length. This is the #1 LLM cost saver.
	"""
	if not text or len(text) < 80:
		return False
	# Strip CSS blocks (common in EPUB cover pages)
	import re

	clean = re.sub(r"@page\s*\{[^}]*\}", " ", text)
	clean = re.sub(r"[{};]", " ", clean)
	# Count word-like tokens (sequences of 3+ letters)
	words = re.findall(r"[A-Za-zÁ-ž]{3,}", clean)
	return len(words) >= 8


def _try_deterministic_fix(
	meta: BookMeta,
	diag: "Diagnosis",  # noqa: F821
	extracted: ExtractedMeta,
	enricher: Enricher | None,
	skip_enrich: bool,
) -> "EnrichedMeta | None":  # noqa: F821
	"""Resolve a content-verified identity, then fill metadata online.

	Online sources no longer guess identity. We first acquire an identity from
	the book itself (ISBN or title+author), confirmed against its content, then
	anchor the online lookup to that identity. The result is identity_confirmed
	(safe to auto-accept even if it changes title/author — we know the book).

	Returns None if no identity could be verified against content (→ LLM, or
	review when there is no content to reason over).
	"""
	identity = _acquire_identity(meta, extracted)
	if identity is None:
		return None

	# Online fill, anchored to the verified identity (ISBN exact, or title+
	# author with an author-match filter).
	online = _online_fill(identity, enricher, skip_enrich)
	if online is not None:
		online.identity_confirmed = True
		return online

	# No online data — fall back to a content-grounded proposal (offline fix
	# from text_meta + embedded OPF, only fields that improve on the meta).
	return _content_proposal(meta, extracted)


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


def _acquire_identity(meta: BookMeta, extracted: ExtractedMeta | None) -> IdentityResult | None:
	"""Acquire a content-verified identity for the book (no network).

	Cascade (first verified wins):
	  1. content-ISBN — scanned from the book's text/embedded OPF (strongest;
	     self-grounded, validated by ISBN check digit).
	  2. metadata ISBN — confirmed present in the content.
	  3. metadata title+author — confirmed present in the page text.
	  4. offline extractor (text_meta) title+author — mined from the page text.

	Returns None when no identity can be confirmed against content (→ LLM).
	"""
	if extracted is None:
		return None
	from .verifier import _isbn_in_content

	content_isbn = extracted.isbn_from_text or extracted.isbn
	text = extracted.first_page_text

	# 1. Content-ISBN (strongest, content-grounded).
	if content_isbn:
		return IdentityResult(isbn=content_isbn, source="content-isbn")

	# 2. Metadata ISBN, verified against content.
	if meta.isbn and _isbn_in_content(meta.isbn, extracted):
		return IdentityResult(isbn=meta.isbn, source="metadata")

	# 3. Metadata title+author, verified against content.
	if meta.title and meta.authors and identity_in_text(meta.title, meta.authors[0], text):
		return IdentityResult(title=meta.title, authors=list(meta.authors), year=meta.year, source="metadata")

	# 4. Offline extractor (text_meta) — content-grounded.
	ext_title = extracted.title_from_text
	ext_authors = extracted.authors_from_text or []
	if ext_title and ext_authors and identity_in_text(ext_title, ext_authors[0], text):
		return IdentityResult(title=ext_title, authors=list(ext_authors), year=extracted.year_from_text, source="extractor")

	return None


def _online_matches_identity(online: "EnrichedMeta", identity: IdentityResult) -> bool:  # noqa: F821
	"""For title-based online lookups: does the record's author match the
	verified identity? (The title was already gated by the source's search.)
	Different author ⇒ different book ⇒ reject (false-positive prevention)."""
	from rapidfuzz import fuzz

	if online.authors and identity.authors:
		return fuzz.token_sort_ratio(online.authors[0].lower(), identity.authors[0].lower()) >= 80
	return True  # no author to compare — trust the title search


def _online_fill(identity: IdentityResult, enricher: Enricher | None, skip_enrich: bool) -> "EnrichedMeta | None":  # noqa: F821
	"""Fill metadata online, anchored to the verified identity: exact by ISBN,
	or title+author with an author-match filter. Returns None if nothing found
	or the result doesn't match the identity."""
	if enricher is None or skip_enrich:
		return None
	if identity.has_isbn:
		online = enricher.lookup(isbn=identity.isbn)
	elif identity.has_title_author:
		# Pass the identity's year so the databazeknih title search can
		# disambiguate editions (prefer the matching publication year).
		online = enricher.lookup(title=identity.title, author=identity.authors[0], year=identity.year)
	else:
		return None
	if online is None:
		return None
	# ISBN lookups are exact; title lookups need an author-match filter.
	if identity.has_title_author and not _online_matches_identity(online, identity):
		log.debug("online result rejected (author mismatch) for identity %r", identity.title)
		return None
	return online


def _content_proposal(meta: BookMeta, extracted: ExtractedMeta) -> "EnrichedMeta | None":  # noqa: F821
	"""Build an offline proposal from content-grounded fields (text_meta +
	embedded OPF), only fields that improve on the current meta. Used when the
	online lookup found nothing — the identity is still content-confirmed, so
	the proposal carries identity_confirmed=True."""
	from .enrichers import EnrichedMeta

	content_isbn = extracted.isbn_from_text or extracted.isbn
	proposal_title = extracted.title_from_text if _is_better(extracted.title_from_text, meta.title) else None
	proposal_authors = [a for a in (extracted.authors_from_text or []) if _is_better(a, meta.authors[0] if meta.authors else None)] or None
	proposal_isbn = content_isbn if (content_isbn and content_isbn != meta.isbn and _is_better(content_isbn, meta.isbn)) else None
	proposal_publisher = extracted.publisher_from_text if _is_better(extracted.publisher_from_text, meta.publisher) else None
	proposal_year = extracted.year_from_text if _is_better(extracted.year_from_text, meta.year) else None
	# Embedded-OPF fallback (weakest; calibre may have corrupted it).
	if proposal_title is None:
		proposal_title = extracted.title if _is_better(extracted.title, meta.title) else None
	if proposal_authors is None:
		embedded_authors = extracted.authors if any(_is_better(a, m) for a, m in zip(extracted.authors, meta.authors)) else None
		if embedded_authors:
			proposal_authors = [a for a in embedded_authors if a and a != "Neznamy" and "_" not in a] or None

	if proposal_title or proposal_authors or proposal_isbn or proposal_publisher or proposal_year:
		return EnrichedMeta(
			title=proposal_title,
			authors=proposal_authors or [],
			isbn=proposal_isbn,
			publisher=proposal_publisher,
			year=proposal_year,
			source="content",
			identity_confirmed=True,
		)
	return None


def _is_better(candidate: object | None, current: object | None) -> bool:
	"""Is *candidate* a better (cleaner) value than *current*?

	A candidate is "better" if EITHER:
	  - the current is obviously broken (underscores, extensions, mojibake,
	    "Neznamy") and the candidate is not, OR
	  - the candidate has Czech/Slovak diacritics that the current lacks
	    (e.g. "Čas přílivu" beats "Cas prilivu" — same text, but with proper
	    diacritics). This catches the common case where Calibre stripped
	    diacritics but didn't replace it with underscores.

	Both arguments may be str, int (e.g. year), or None. Non-string truthy
	values are treated as always-good (they can't carry textual corruption);
	the diacritics check only applies to strings.
	"""
	if not candidate:
		return False
	if not current:
		return True
	# Non-string values (e.g. year as int) can't be "broken" textually, and
	# any truthy value beats a falsy current (handled above). Treat the
	# candidate as good and the current as not-broken unless it's a string.
	if not isinstance(candidate, str) or not isinstance(current, str):
		return isinstance(current, str) and _looks_broken(current) and not (isinstance(candidate, str) and _looks_broken(candidate))
	candidate_bad = _looks_broken(candidate)
	current_bad = _looks_broken(current)
	if current_bad and not candidate_bad:
		return True
	# Diacritics check: candidate has CZ letters that current lacks at the
	# same positions (after stripping both to ASCII they should be equal).
	if not candidate_bad and not current_bad:
		if _has_cz_diacritics(candidate) and not _has_cz_diacritics(current):
			if _strip_diacritics(candidate).lower() == _strip_diacritics(current).lower():
				return True
		# Title-case check: candidate has each word capitalised (title case)
		# while current is all-lowercase — same text but better casing. Common
		# case where the DB stored the title lowercase but the title page (and
		# thus text_meta) recovered proper title case. Only fires when the two
		# are equal ignoring case, to avoid spurious "improvements".
		if _is_better_title_case(candidate, current):
			return True
	return False


def _is_better_title_case(candidate: str, current: str) -> bool:
	"""True when *candidate* is the same text as *current* but better capitalised.

	Catches the common CZ/SK case where calibre stored a sentence-case title
	(``Čas přílivu`` — only the first word capitalised) but text_meta mined
	proper title case from the title page (``Čas Přílivu``). Treats that as an
	improvement worth proposing. Only fires when the two are equal ignoring
	case, to avoid spurious "improvements".
	"""
	if not candidate or not current:
		return False
	if candidate.lower() != current.lower():
		return False
	cand_words = candidate.split()
	cur_words = current.split()
	if len(cand_words) < 2 or len(cand_words) != len(cur_words):
		return False
	# Count words (beyond the first) that start uppercase in each.
	def _capitalised_beyond_first(words: list[str]) -> int:
		return sum(1 for w in words[1:] if w[:1].isupper())
	return _capitalised_beyond_first(cand_words) > _capitalised_beyond_first(cur_words)


_BROKEN_VALUES = {"Neznamy", "Unknown", "anonym", "Anonymous", "Neznámý", ""}


def _looks_broken(s: str | object) -> bool:
	"""Does *s* have obvious corruption signals?

	Accepts any type and stringifies it, so callers can safely pass ints
	(e.g. year) without a TypeError.
	"""
	if s is None:
		return True
	s = str(s)
	if not s or s in _BROKEN_VALUES:
		return True
	if "_" in s:
		return True
	if any(ext in s.lower() for ext in (".epub", ".pdb", ".pdf", ".doc", ".mobi", ".azw", ".azw3", ".prc")):
		return True
	# Mojibake / control chars
	if any(ord(c) > 0x2000 and c not in "„“”‘’–—…" for c in s):
		return True
	return False


_CZ_DIACRITICS = set("áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽôäÄ")


def _has_cz_diacritics(s: str) -> bool:
	return any(c in _CZ_DIACRITICS for c in s)


def _strip_diacritics(s: str) -> str:
	repl = str.maketrans(
		"áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽôäÄ",
		"acdeeinorstuuyzACDEEINORSTUUYZoaA",
	)
	return s.translate(repl)


def _build_llm_evidence(meta: BookMeta, diag: "Diagnosis", extracted: ExtractedMeta | None) -> dict:  # noqa: F821
	"""Assemble the evidence dict passed to the LLM provider."""
	first_page = extracted.first_page_text if extracted is not None else None
	return {
		"category": diag.category,
		"current": {
			"title": meta.title,
			"authors": meta.authors,
			"isbn": meta.isbn,
			"year": meta.year,
			"publisher": meta.publisher,
		},
		"first_page_text": first_page,
		"file_name": meta.primary_file,
		"author_folder": meta.author_folder,
		"title_folder": meta.title_folder,
	}


def _reconciled_is_useful(r, meta: BookMeta) -> bool:  # noqa: ANN001
	"""Did the LLM produce anything useful?

	Useful = at least ONE of:
	  - a title (the most important field)
	  - an author that's not a placeholder ("Neznamy", "Unknown")
	  - an ISBN
	  - a series (which the DB usually lacks)
	If the LLM returned nothing for any of these, it's not useful.
	"""
	if r.title:
		return True
	if r.authors and any(a not in ("Neznamy", "Unknown", "anonym", "Anonymous", "") for a in r.authors):
		return True
	if r.isbn:
		return True
	if r.series:
		return True
	return False


def _reconciled_to_enriched(r, *, source: str | None = None) -> "EnrichedMeta":  # noqa: F821
	"""Convert a ReconciledMeta into the EnrichedMeta shape used by review.py.

	*source* overrides the default ``llm:<confidence>`` tag — used when the
	result came through reconcile_loop, which already labels it
	``llm:flash``/``llm:loop``/``llm:high``/``llm:low``.
	"""
	from .enrichers import EnrichedMeta

	return EnrichedMeta(
		title=r.title,
		authors=r.authors,
		isbn=r.isbn,
		publisher=r.publisher,
		year=r.year,
		language=r.language,
		description=r.reasoning,  # stash reasoning as description for transparency
		series=r.series,
		series_index=r.series_index,
		genres=r.genres,
		source=source or f"llm:{r.confidence}",
	)


def _llm_reconcile_with_loop(provider: Any, evidence: dict, extracted: Any, *, loop: bool = True) -> tuple[Any, str]:  # noqa: F821
	"""Run the LLM, preferring the self-correction loop when available.

	When *loop* is False (config BMF_LLM_LOOP=0 or --no-llm-loop), a single
	``reconcile`` call is made instead of the Flash+feedback+final flow.
	Providers that don't implement ``reconcile_loop`` always use ``reconcile``.

	Returns (ReconciledMeta | None, source) where source is one of
	llm:flash / llm:loop / llm:high / llm:low / llm:medium / ''.
	"""
	if loop and hasattr(provider, "reconcile_loop"):
		return provider.reconcile_loop(evidence, extracted)
	result = provider.reconcile(evidence)
	return result, ("llm:medium" if result is not None else "")


def generate_review(
	results: list,
	*,
	library_root: Path,
	output: Path,
) -> int:
	"""Write a review.yaml from pipeline results. Incremental: preserves
	existing user edits (action, edited, notes) and prior `proposed` values
	when the new run didn't produce a better proposal.

	Returns the count of entries written.
	"""
	# 1. Load existing review.yaml (if any) so we can preserve user edits.
	existing_by_id: dict[int | str, dict] = {}
	if output.is_file():
		try:
			prev = _yaml_safe_load(output)
			for entry in prev or []:
				eid = entry.get("id")
				if eid is not None:
					existing_by_id[eid] = entry
		except Exception as e:  # noqa: BLE001
			log.warning("could not parse existing %s (%s); starting fresh", output, e)

	# 2. Build items for review: NEEDS_REVIEW + UNFIXABLE + verification=MISMATCH
	items_for_review = []
	preserved = 0
	refreshed = 0
	for meta, diag, verification, enriched in results:
		include = diag.verdict.value in ("NEEDS_REVIEW", "UNFIXABLE")
		if verification and verification.result == "MISMATCH":
			include = True
		if not include:
			continue
		extracted = verification.extracted if verification else None

		# If we have a prior entry for this id, preserve user edits. Two cases:
		# (a) user already set `action` — keep the entry AS-IS (don't overwrite
		#     their decision). They can re-run to refresh `current` if needed.
		# (b) no action yet, but the prior run had a `proposed` and this run
		#     doesn't (e.g. LLM quota ran out) — keep the prior `proposed`.
		prior = existing_by_id.get(meta.calibre_id)
		if prior is not None:
			if prior.get("action") is not None:
				# User already decided. Reuse the entire prior entry, but update
				# `current` so it reflects any metadata changes since.
				prior["current"] = _build_current(meta)
				items_for_review.append(_entry_from_prior(prior, meta, diag, extracted))
				preserved += 1
				continue
			# No user action yet. Use the new run's proposal if present, else
			# carry over the prior proposal (LLM result from a previous run).
			if enriched is None and prior.get("proposed"):
				# Reconstruct an EnrichedMeta-like dict from the prior proposed.
				enriched = _proposed_to_enriched(prior["proposed"])
		items_for_review.append((meta, diag, extracted, enriched))
		refreshed += 1

	yaml_text = build_review(items_for_review, library_root=library_root)
	output.write_text(yaml_text, encoding="utf-8")
	log.info(
		"wrote %d review entries to %s (%d preserved, %d refreshed)",
		len(items_for_review), output, preserved, refreshed,
	)
	return len(items_for_review)


def _build_current(meta: BookMeta) -> dict:
	"""Build the `current` block for a review entry from a BookMeta."""
	current = {
		"author": meta.authors[0] if meta.authors else None,
		"authors": meta.authors if len(meta.authors) > 1 else None,
		"title": meta.title,
		"isbn": meta.isbn,
		"year": meta.year,
		"publisher": meta.publisher,
		"language": meta.language,
	}
	return {k: v for k, v in current.items() if v is not None}


def _entry_from_prior(prior: dict, meta: BookMeta, diag, extracted) -> tuple:  # noqa: ANN001
	"""Wrap a preserved prior entry as a 5-element tuple that build_review
	can re-emit. The enriched value carries the prior proposed, and the 5th
	element carries the full prior dict (so build_review can preserve action/
	edited/notes).
	"""
	proposed = prior.get("proposed")
	enriched = _proposed_to_enriched(proposed) if proposed else None
	return (meta, diag, extracted, enriched, prior)


def _proposed_to_enriched(proposed: dict):  # noqa: ANN202
	"""Reconstruct an EnrichedMeta from a review.yaml `proposed` block."""
	from .enrichers import EnrichedMeta

	authors = []
	if proposed.get("author"):
		authors = [proposed["author"]]
	if proposed.get("authors"):
		authors = proposed["authors"]
	return EnrichedMeta(
		title=proposed.get("title"),
		authors=authors,
		isbn=proposed.get("isbn"),
		publisher=proposed.get("publisher"),
		year=proposed.get("year"),
		language=proposed.get("language"),
		series=proposed.get("series"),
		series_index=proposed.get("series_index"),
		genres=proposed.get("genres") or [],
		description=proposed.get("reasoning"),
		source=proposed.get("source") or "preserved",
	)


def _yaml_safe_load(path: Path):
	"""Load a review YAML file (multi-doc or legacy single-list) into a flat
	list of entry dicts. Returns None on empty/invalid."""
	import yaml

	try:
		docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
	except yaml.YAMLError:
		return None
	entries = []
	for doc in docs:
		if doc is None:
			continue
		if isinstance(doc, list):
			entries.extend(doc)
		elif isinstance(doc, dict):
			entries.append(doc)
	return entries if entries else None


def apply_review(review_path: Path, library: Path, *, dry_run: bool = True, cache: Cache | None = None, progress_callback=None) -> dict:
	"""Parse a review.yaml and apply approved changes to the library.

	Returns a summary dict: {applied, rejected, deleted, pruned, remaining,
	snapshot, errors, dry_run}.

	``action: delete`` removes the whole book folder. Because that is not
	reversible via the per-file ``.bak`` that ``write_book_meta`` keeps, the
	folders slated for deletion are first bundled into a single
	``deletion_snapshot_<stamp>.tar.gz`` (in dry-run mode nothing is archived
	and nothing is removed).

	After a successful WRITE run, successfully-applied entries (accept/swap/
	edit/delete without error) are pruned from review.yaml so the file reflects
	only remaining work; pending (action: null), rejected, and errored entries
	are kept. Dry-run never prunes.
	"""
	from .models import Diagnosis  # local to avoid cycle

	items = parse_review(review_path)
	summary = {"applied": 0, "rejected": 0, "deleted": 0, "pruned": 0, "remaining": None, "snapshot": None, "errors": [], "dry_run": dry_run}
	succeeded_ids: set = set()  # ids of entries committed this run → pruned
	# delete collects (folder, id) so removal success can be tracked per entry.
	deletions: list[tuple[Path, int | None]] = []
	total = len(items)

	for i, item in enumerate(items):
		# Report progress at the start of each item (covers every path, including
		# the `continue`s below): "i items done, now on item i+1". A final
		# callback(total, total) fires after the loop.
		if progress_callback is not None:
			progress_callback(i, total)
		if item.action is None:
			# Not yet reviewed — skip silently
			continue
		if item.action == "reject":
			summary["rejected"] += 1
			continue
		if item.action not in ("accept", "swap", "edit", "delete"):
			summary["errors"].append(f"id={item.id}: unknown action {item.action!r}")
			continue

		# Reconstruct a BookMeta with the *current* values, then apply the fix
		folder = Path(item.path)
		if not folder.is_absolute():
			folder = library / item.path
		if not folder.is_dir():
			summary["errors"].append(f"id={item.id}: folder not found: {folder}")
			continue

		# delete: just collect — actual removal happens after a single tar.gz
		# snapshot is taken, so the whole batch can be rolled back together.
		if item.action == "delete":
			deletions.append((folder, item.id))
			continue

		# Build the desired metadata
		from .readers import read_book_folder

		meta = read_book_folder(folder)
		_apply_action(meta, item)

		try:
			result = write_book_meta(meta, dry_run=dry_run)
			if result.get("error"):
				summary["errors"].append(f"id={item.id}: {result['error']}")
			else:
				summary["applied"] += 1
				if not dry_run:
					if item.id is not None:
						succeeded_ids.add(item.id)
					# The folder's metadata just changed on disk; drop its cached
					# BookMeta so the next scan re-parses it. Especially matters
					# on NFS, where the attribute cache can mask the new mtime.
					if cache is not None:
						cache.invalidate(folder)
		except Exception as e:  # noqa: BLE001
			summary["errors"].append(f"id={item.id}: write failed: {e}")

	# All items processed.
	if progress_callback is not None:
		progress_callback(total, total)

	# Deletion pass: snapshot then remove. Dry-run reports without touching disk.
	if deletions:
		deleted_paths = [p for p, _ in deletions]
		summary["deleted"] = len(deleted_paths)
		if not dry_run:
			snap = _snapshot_deletions(deleted_paths, library)
			summary["snapshot"] = str(snap) if snap else None
			for folder, did in deletions:
					try:
						import shutil

						shutil.rmtree(folder)
						if did is not None:
							succeeded_ids.add(did)
						# Folder is gone — drop its cache entry too.
						if cache is not None:
							cache.invalidate(folder)
					except OSError as e:
						summary["errors"].append(f"delete failed for {folder}: {e}")

	# Pruning: drop successfully-applied entries from review.yaml. Only in WRITE
	# mode — dry-run must leave the file untouched.
	if not dry_run and succeeded_ids:
		summary["remaining"] = prune_review(review_path, succeeded_ids)
		summary["pruned"] = len(succeeded_ids)

	# Commit any cache invalidations from this run (writes + deletes).
	if cache is not None and not dry_run:
		cache.commit()

	return summary


def _snapshot_deletions(folders: list[Path], library: Path) -> Path | None:
	"""Bundle *folders* (whole book dirs) into a tar.gz next to the library.

    Returns the snapshot path, or None if there was nothing to archive or the
    archive could not be written (errors are logged, not raised — the caller
    proceeds so a failed snapshot does not block the whole apply run).
    """
	import tarfile
	from datetime import datetime

	if not folders:
		return None
	stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
	output = Path(f"deletion_snapshot_{stamp}.tar.gz")
	try:
		with tarfile.open(output, "w:gz") as tar:
			for folder in folders:
				tar.add(folder, arcname=str(folder.relative_to(library)) if folder.is_relative_to(library) else folder.name)
		log.info("deletion snapshot: %d folders -> %s", len(folders), output)
		return output
	except OSError as e:
		log.warning("could not write deletion snapshot %s: %s", output, e)
		return None


def _apply_action(meta: BookMeta, item) -> None:  # noqa: ANN001
	"""Mutate *meta* in place according to the review item's action.

	Cover downloads are handled inline (not deferred to write_book_meta) because
	they involve a network fetch — the caller (apply_review) wraps this in a
	try/except and reports download failures via the summary dict.
	"""
	if item.action == "accept" and item.proposed:
		# Apply all proposed fields
		if "title" in item.proposed:
			meta.title = item.proposed["title"]
		if "author" in item.proposed:
			meta.authors = [item.proposed["author"]] + (meta.authors[1:] if len(meta.authors) > 1 else [])
		if "isbn" in item.proposed:
			meta.isbn = item.proposed["isbn"]
		if "year" in item.proposed:
			meta.year = item.proposed["year"]
		if "publisher" in item.proposed:
			meta.publisher = item.proposed["publisher"]
		if "language" in item.proposed:
			meta.language = item.proposed["language"]
		if "genres" in item.proposed:
			genres = item.proposed["genres"]
			meta.genres = genres if isinstance(genres, list) else [genres]
		# Cover replacement: download cover_url to cover.jpg — but only when the
		# book has a cover diagnosis (C11 placeholder / MISSING_COVER) among ANY
		# of its diagnoses, not just the primary. A book whose primary issue is
		# C2 but that also has a generated cover must get its cover replaced in
		# the same pass. cover_url rides along on every enriched book's proposed
		# block in older review.yaml files, so this gate also neutralises those
		# legacy entries. Idempotent: a re-run must not re-download a fixed cover.
		diag_dicts = item.diagnoses or ([item.diagnosis] if item.diagnosis else [])
		cats = {d.get("category") for d in diag_dicts}
		if "cover_url" in item.proposed and cats & set(_COVER_CATEGORIES):
			from .covers import analyze_cover, download_cover

			cover_path = Path(meta.path) / "cover.jpg"
			if "C11" in cats and cover_path.is_file() and not analyze_cover(cover_path).is_generated:
				# Placeholder already replaced with a real cover.
				log.info("cover already replaced, skipping id=%s", item.id)
			elif "MISSING_COVER" in cats and cover_path.is_file():
				# Missing cover already filled.
				log.info("cover already present, skipping id=%s", item.id)
			else:
				ok = download_cover(item.proposed["cover_url"], cover_path)
				if not ok:
					log.warning("cover download failed for id=%s url=%s", item.id, item.proposed["cover_url"])
	elif item.action == "swap":
		# Swap author <-> title
		old_title = meta.title
		meta.title = meta.authors[0] if meta.authors else ""
		if old_title:
			meta.authors = [old_title] + (meta.authors[1:] if len(meta.authors) > 1 else [])
	elif item.action == "edit" and item.edited:
		# Apply human-edited fields (these override everything)
		if "title" in item.edited:
			meta.title = item.edited["title"]
		if "author" in item.edited:
			meta.authors = [item.edited["author"]] + (meta.authors[1:] if len(meta.authors) > 1 else [])
		if "authors" in item.edited:
			meta.authors = item.edited["authors"]
		if "isbn" in item.edited:
			meta.isbn = item.edited["isbn"]
		if "year" in item.edited:
			meta.year = item.edited["year"]
		if "publisher" in item.edited:
			meta.publisher = item.edited["publisher"]
		if "language" in item.edited:
			meta.language = item.edited["language"]
		if "genres" in item.edited:
			meta.genres = item.edited["genres"] if isinstance(item.edited["genres"], list) else [item.edited["genres"]]
