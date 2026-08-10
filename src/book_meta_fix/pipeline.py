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
from pathlib import Path
from typing import Any

from .detectors import detect as detect_fn
from .enrichers import Enricher
from .extractors import ExtractedMeta, extract
from .library import Cache, scan_library
from .models import BookMeta, Confidence, Diagnosis, Verdict
from .review import build_review, parse_review
from .verifier import verify
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
) -> list[tuple[BookMeta, "Diagnosis", "Verification | None", "EnrichedMeta | None"]]:  # noqa: F821
	"""Run the full pipeline over the whole library.

	Returns a list of (meta, diagnosis, verification, enriched) tuples.

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
	from concurrent.futures import ThreadPoolExecutor

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
	stats = {
		"ok": 0, "needs_review": 0, "det_fixed": 0, "online_fixed": 0,
		"llm_fixed": 0, "llm_skipped_no_text": 0, "llm_no_result": 0, "llm_error": 0,
		"unfixed": 0, "errors": 0, "content_mismatch": 0,
	}

	# Per-book work closure. The shared enricher/llm are thread-safe:
	# - openai client uses an internal httpx Client (thread-safe)
	# - requests.Session is thread-safe for GETs
	# - SQLite Enricher cache serializes via its connection's check_same_thread=False
	def _process(meta: BookMeta):
		return _process_book(
			meta, enricher=enricher, skip_enrich=skip_enrich, skip_verify=skip_verify,
			llm_provider=llm_provider, llm_categories=llm_categories, stats=stats,
			verify_ok=verify_ok, strict_verify=strict_verify,
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
			stats["errors"] += 1
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
			# submit + as_completed would give fastest-first ordering, but we
			# want input-order output, so we submit all and read futures in order.
			# _process_safe already swallows exceptions, so fut.result() won't
			# raise — but we guard anyway in case the pool itself fails.
			futures = [pool.submit(_process_safe, meta) for meta in books]
			for i, fut in enumerate(futures):
				if interrupted:
					# We've already stopped reading new results; cancel pending
					# futures so the pool can wind down without waiting on them.
					fut.cancel()
					continue
				try:
					# fut.result() blocks until this book finishes. On Ctrl-C we
					# cancel everything still pending and break, keeping the
					# results collected so far.
					results.append(_stream(fut.result()))
				except KeyboardInterrupt:
					interrupted = True
					log.warning("interrupted by user (Ctrl-C) after %d/%d books; cancelling pending work, keeping partial results", i, total)
					# Cancel futures not yet started; in-flight LLM/HTTP calls
					# will finish on their own (we can't safely kill a thread),
					# but we won't wait for or count their results.
					for f in futures[i + 1 :]:
						f.cancel()
				except Exception as e:  # noqa: BLE001
					stats["errors"] += 1
					log.exception("worker future failed unexpectedly: %s", e)
					results.append(None)
				if not interrupted and progress_callback is not None:
					progress_callback(i + 1, total)

	# Drop any None placeholders from a catastrophic worker failure (kept above
	# only to preserve order/count); generate_review iterates results as-is.
	results = [r for r in results if r is not None]

	if interrupted:
		log.warning("pipeline interrupted: returning %d partial results (of %d books) for review", len(results), total)
	log.info(
		"pipeline: %d ok, %d needs_review (content_mismatch=%d, det=%d, online=%d, llm=%d, llm_skipped=%d, llm_no_result=%d, unfixed=%d, errors=%d)%s",
		stats["ok"], stats["needs_review"], stats["content_mismatch"], stats["det_fixed"], stats["online_fixed"],
		stats["llm_fixed"], stats["llm_skipped_no_text"], stats["llm_no_result"], stats["unfixed"], stats["errors"],
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
					preextracted = verification.extracted
					diag = Diagnosis(
						category="CONTENT_MISMATCH",
						reason=f"metadata neodpovídají obsahu ({verification.result}: {verification.reason})",
						confidence=Confidence.HIGH,
						verdict=Verdict.NEEDS_REVIEW,
					)
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

	is_needs_review = diag.verdict == Verdict.NEEDS_REVIEW or diag.category in ("MISSING_ISBN", "MISSING_YEAR")

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
				stats["det_fixed" if enriched.source in ("embedded", "content") else "online_fixed"] += 1

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
				try:
					evidence = _build_llm_evidence(meta, diag, extracted)
					reconciled = llm_provider.reconcile(evidence)
					if reconciled is not None and _reconciled_is_useful(reconciled, meta):
						enriched = _reconciled_to_enriched(reconciled)
						stats["llm_fixed"] += 1
					else:
						stats["llm_no_result"] += 1
				except Exception as e:  # noqa: BLE001
					log.debug("LLM reconcile failed for %s: %s", meta.path, e)
					stats["llm_error"] += 1

		if enriched is None:
			stats["unfixed"] += 1

	return (meta, diag, verification, enriched)


def _safe_extract(meta: BookMeta) -> ExtractedMeta | None:
	"""Extract content metadata from the primary book file.

	We use only the primary file (highest-priority format present) rather than
	trying all formats, because empirical testing showed multi-format extraction
	is 6.5x slower and produces a higher-scored result in 0% of cases — calibre
	writes identical metadata to all formats on import.
	"""
	if not meta.primary_file:
		return None
	try:
		return extract(meta.primary_file)
	except Exception as e:  # noqa: BLE001
		log.debug("extract failed for %s: %s", meta.path, e)
		return None


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
	"""Try cheap fixes before falling back to the LLM. Cheap-first order:

	  A. Offline metadata mined from the book's page text (text_meta) — no I/O.
	  B. Online lookup by ISBN (text-scan ISBN > embedded ISBN > DB ISBN).
	  C. Online lookup by title + author (text-mined > embedded > DB).
	  D. Embedded-OPF compare (only if cleaner than DB; calibre may have
	     corrupted it, so this is the weakest signal and runs last).

	Returns an EnrichedMeta if we found something better than the current
	(broken) metadata, else None (caller falls back to the LLM).
	"""
	from .enrichers import EnrichedMeta

	# Best available ISBN independent of the (possibly calibre-corrupted) DB:
	# prefer the one scanned from the page text, then the embedded-OPF one.
	content_isbn = extracted.isbn_from_text or extracted.isbn

	# Best available title/author for an online title-search: prefer text-mined
	# (independent of OPF), then embedded, then DB.
	best_title = extracted.title_from_text or extracted.title or meta.title
	best_authors = extracted.authors_from_text or extracted.authors or meta.authors
	best_author = best_authors[0] if best_authors else None

	# --- Phase B: online lookup by ISBN (cached, ~1s) ---
	# databazeknih is title-only, so this hits OpenLibrary / Google Books.
	if content_isbn and enricher is not None and not skip_enrich:
		online = enricher.lookup(isbn=content_isbn)
		if online is not None and _online_is_useful(online, meta):
			return online

	# --- Phase C: online lookup by title + author ---
	# This is the path that reaches databazeknih.cz (the strongest CZ/SK
	# source) as well as OpenLibrary's title search. Skipped when we have no
	# title to search on.
	if best_title and enricher is not None and not skip_enrich:
		online = enricher.lookup(title=best_title, author=best_author)
		if online is not None and _online_is_useful(online, meta):
			return online

	# --- Phase A: offline metadata mined from the book's page text ---
	# Build a proposal from the text-mined fields when they are cleaner than
	# the DB. These come from extractors.ExtractedMeta.*_from_text, populated
	# by text_meta.extract_metadata_from_text over first_page_text.
	proposal_title = extracted.title_from_text if _is_better(extracted.title_from_text, meta.title) else None
	proposal_authors = [a for a in extracted.authors_from_text if _is_better(a, meta.authors[0] if meta.authors else None)] or None
	proposal_isbn = content_isbn if (content_isbn and content_isbn != meta.isbn and _is_better(content_isbn, meta.isbn)) else None
	proposal_publisher = extracted.publisher_from_text if _is_better(extracted.publisher_from_text, meta.publisher) else None
	proposal_year = extracted.year_from_text if _is_better(extracted.year_from_text, meta.year) else None

	# --- Phase D: embedded-OPF compare (weakest; calibre may have corrupted it) ---
	# Only fill fields that Phase A did not already fill.
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
		)
	return None


def _online_is_useful(online: "EnrichedMeta", meta: BookMeta) -> bool:  # noqa: F821
	"""Does an online result bring something the DB doesn't already have?

	Accepts the online record if ANY of its fields is better than the DB's
	(title/author/isbn/year/publisher/genres). This is the gate that keeps us
	from overwriting good metadata with a no-op online hit.
	"""
	return any(
		_is_better(getattr(online, f, None), getattr(meta, f, None))
		for f in ("title", "isbn", "year", "publisher")
	) or bool(getattr(online, "genres", None))


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
	return False


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
	if any(ext in s.lower() for ext in (".epub", ".pdb", ".pdf", ".doc", ".mobi")):
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


def _reconciled_to_enriched(r) -> "EnrichedMeta":  # noqa: F821
	"""Convert a ReconciledMeta into the EnrichedMeta shape used by review.py."""
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
		source=f"llm:{r.confidence}",
	)


def auto_apply_results(
	results: list,
	*,
	library_root: Path,
	threshold: str = "high",
	dry_run: bool = True,
) -> dict:
	"""Auto-apply LLM proposals at or above *threshold* confidence.

	Writes metadata.json + metadata.opf directly (with .bak backup) for books
	where the enriched proposal has source like 'llm:high' (or higher).
	Returns a summary dict and the list of remaining (non-applied) results
	for the caller to put into review.yaml.

	Confidence order: high > medium > low. With threshold='high' (default),
	only high-confidence proposals are auto-applied; medium/low go to review.
	"""
	confidence_rank = {"high": 3, "medium": 2, "low": 1}
	min_rank = confidence_rank.get(threshold, 3)

	applied = 0
	skipped_low_conf = 0
	skipped_no_proposal = 0
	skipped_user_decided = 0
	remaining: list = []

	# Load existing review.yaml to respect user's prior decisions (action set).
	prior_actions: dict[int | str, str | None] = {}
	# (best-effort: caller passes results, not the review path, so we skip
	#  prior-action checking here — it's handled in generate_review for the
	#  remaining books.)

	for item in results:
		meta, diag, verification, enriched = item[0], item[1], item[2], item[3]
		# Only consider NEEDS_REVIEW books with a proposal
		if diag.verdict.value not in ("NEEDS_REVIEW", "UNFIXABLE"):
			if not (verification and verification.result == "MISMATCH"):
				continue
		if enriched is None:
			skipped_no_proposal += 1
			remaining.append(item)
			continue
		# Check confidence
		conf = "low"
		if enriched.source.startswith("llm:"):
			conf = enriched.source.split(":", 1)[1] if ":" in enriched.source else "low"
		elif enriched.source.startswith("embedded"):
			# Deterministic fixes are trustworthy — treat as high.
			conf = "high"
		if confidence_rank.get(conf, 0) < min_rank:
			skipped_low_conf += 1
			remaining.append(item)
			continue
		# Apply: build a BookMeta with the proposed values and write it.
		updated = _apply_enriched_to_meta(meta, enriched)
		if not dry_run:
			try:
				from .writers import write_book_meta

				write_book_meta(updated, dry_run=False, backup=True)
			except Exception as e:  # noqa: BLE001
				log.warning("auto-apply write failed for %s: %s", meta.path, e)
				remaining.append(item)
				continue
		applied += 1

	summary = {
		"applied": applied,
		"skipped_low_conf": skipped_low_conf,
		"skipped_no_proposal": skipped_no_proposal,
		"dry_run": dry_run,
		"threshold": threshold,
		"remaining": remaining,
	}
	log.info(
		"auto_apply: %d applied (%s), %d low-conf, %d no-proposal -> %d remaining for review",
		applied, "dry-run" if dry_run else "WRITTEN", skipped_low_conf, skipped_no_proposal, len(remaining),
	)
	return summary


def _apply_enriched_to_meta(meta: BookMeta, enriched) -> BookMeta:  # noqa: ANN001
	"""Return a copy of *meta* with the enriched proposal applied.

	Used by auto_apply_results to build the BookMeta that will be written.
	"""
	import copy

	updated = copy.deepcopy(meta)
	if enriched.title:
		updated.title = enriched.title
	if enriched.authors:
		# Drop placeholder authors, keep real ones
		real = [a for a in enriched.authors if a and a not in ("Neznamy", "Unknown", "Neznámý", "anonym", "Anonymous")]
		if real:
			updated.authors = real
	if enriched.isbn:
		updated.isbn = enriched.isbn
	if enriched.year:
		updated.year = enriched.year
	if enriched.publisher:
		updated.publisher = enriched.publisher
	if enriched.language:
		updated.language = enriched.language
	if enriched.series:
		# Add/replace series metadata
		series_entry = {"name": enriched.series}
		if enriched.series_index:
			series_entry["index"] = enriched.series_index
		updated.series = [series_entry]
	if enriched.genres:
		updated.genres = enriched.genres
	return updated


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


def apply_review(review_path: Path, library: Path, *, dry_run: bool = True) -> dict:
	"""Parse a review.yaml and apply approved changes to the library.

	Returns a summary dict: {applied, rejected, deleted, snapshot, errors}.

	``action: delete`` removes the whole book folder. Because that is not
	reversible via the per-file ``.bak`` that ``write_book_meta`` keeps, the
	folders slated for deletion are first bundled into a single
	``deletion_snapshot_<stamp>.tar.gz`` (in dry-run mode nothing is archived
	and nothing is removed).
	"""
	from .models import Diagnosis  # local to avoid cycle

	items = parse_review(review_path)
	summary = {"applied": 0, "rejected": 0, "deleted": 0, "snapshot": None, "errors": [], "dry_run": dry_run}
	deleted_paths: list[Path] = []

	for item in items:
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
			deleted_paths.append(folder)
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
		except Exception as e:  # noqa: BLE001
			summary["errors"].append(f"id={item.id}: write failed: {e}")

	# Deletion pass: snapshot then remove. Dry-run reports without touching disk.
	if deleted_paths:
		summary["deleted"] = len(deleted_paths)
		if not dry_run:
			snap = _snapshot_deletions(deleted_paths, library)
			summary["snapshot"] = str(snap) if snap else None
			for folder in deleted_paths:
				try:
					import shutil

					shutil.rmtree(folder)
				except OSError as e:
					summary["errors"].append(f"delete failed for {folder}: {e}")

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
	"""Mutate *meta* in place according to the review item's action."""
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
