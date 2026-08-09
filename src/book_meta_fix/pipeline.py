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
from .models import BookMeta, Verdict
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
	llm_categories: tuple[str, ...] = ("C1", "C4"),
	limit: int | None = None,
	workers: int = 10,
	progress_callback: Any = None,
	only_needs_review: bool = True,
) -> list[tuple[BookMeta, "Diagnosis", "Verification | None", "EnrichedMeta | None"]]:  # noqa: F821
	"""Run the full pipeline over the whole library.

	Returns a list of (meta, diagnosis, verification, enriched) tuples.

	*only_needs_review* (default True): skip books that the detector already
	classifies as OK or VERIFIED. This makes the pipeline incremental —
	repeated runs only touch books that still need work. Set to False to
	force a full re-scan (e.g. when detector rules change).

	If *limit* is given, it caps the number of books processed AFTER the
	only_needs_review filter. So `--limit 500` means "process at most 500
	books that still need review", not "the first 500 books in the library".

	*workers* controls parallelism: each book's expensive I/O (content
	extraction, online lookup, LLM call) runs in a ThreadPoolExecutor with
	this many workers. Output order matches input order.
	*progress_callback* (if given) is called with (i, total) after each book.
	"""
	from concurrent.futures import ThreadPoolExecutor

	all_books = scan_library(library, cache=cache)
	# Apply the detector cheaply to filter out already-OK books (incremental).
	# This is fast (no I/O — just regex/heuristics over metadata).
	if only_needs_review:
		books = [b for b in all_books if detect_fn(b).verdict != Verdict.OK]
		log.info(
			"pipeline: %d total books, %d already OK -> %d to process",
			len(all_books), len(all_books) - len(books), len(books),
		)
	else:
		books = list(all_books)
	if limit is not None:
		books = books[:limit]
	total = len(books)
	stats = {
		"ok": 0, "needs_review": 0, "det_fixed": 0, "online_fixed": 0,
		"llm_fixed": 0, "llm_skipped_no_text": 0, "llm_no_result": 0, "llm_error": 0,
		"unfixed": 0,
	}

	# Per-book work closure. The shared enricher/llm are thread-safe:
	# - openai client uses an internal httpx Client (thread-safe)
	# - requests.Session is thread-safe for GETs
	# - SQLite Enricher cache serializes via its connection's check_same_thread=False
	def _process(meta: BookMeta):
		return _process_book(
			meta, enricher=enricher, skip_enrich=skip_enrich, skip_verify=skip_verify,
			llm_provider=llm_provider, llm_categories=llm_categories, stats=stats,
		)

	# No point spawning a pool of 10 if we only have 3 books.
	n_workers = max(1, min(workers, total))
	if n_workers == 1:
		# Serial path — keeps stack traces readable for debugging
		results = []
		for i, meta in enumerate(books):
			results.append(_process(meta))
			if progress_callback is not None:
				progress_callback(i + 1, total)
	else:
		results = []
		with ThreadPoolExecutor(max_workers=n_workers) as pool:
			# submit + as_completed would give fastest-first ordering, but we
			# want input-order output, so we submit all and read futures in order.
			futures = [pool.submit(_process, meta) for meta in books]
			for i, fut in enumerate(futures):
				results.append(fut.result())
				if progress_callback is not None:
					progress_callback(i + 1, total)

	log.info(
		"pipeline: %d ok, %d needs_review (det=%d, online=%d, llm=%d, llm_skipped=%d, llm_no_result=%d, unfixed=%d)",
		stats["ok"], stats["needs_review"], stats["det_fixed"], stats["online_fixed"],
		stats["llm_fixed"], stats["llm_skipped_no_text"], stats["llm_no_result"], stats["unfixed"],
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
) -> tuple[BookMeta, "Diagnosis", "Verification | None", "EnrichedMeta | None"]:  # noqa: F821
	"""Process one book end-to-end. Thread-safe (no shared mutable state except *stats*)."""
	diag = detect_fn(meta)
	verification = None
	enriched = None

	is_needs_review = diag.verdict == Verdict.NEEDS_REVIEW or diag.category in ("MISSING_ISBN", "MISSING_YEAR")

	# --- OK books: just verify against content ---
	if diag.verdict == Verdict.OK and not skip_verify and meta.primary_file:
		try:
			verification = verify(meta)
		except Exception as e:  # noqa: BLE001
			log.debug("verify failed for %s: %s", meta.path, e)
		stats["ok"] += 1

	# --- NEEDS_REVIEW books: try cheap fixes first, LLM last ---
	elif is_needs_review:
		stats["needs_review"] += 1
		# Extract content once (reused by both deterministic fixes and LLM)
		extracted = _safe_extract(meta)

		# Step 2a-2c: deterministic fixes from extracted content + online lookup
		if extracted is not None:
			enriched = _try_deterministic_fix(meta, diag, extracted, enricher, skip_enrich)
			if enriched is not None:
				stats["det_fixed" if enriched.source.startswith("embedded") else "online_fixed"] += 1

		# Step 4: LLM fallback only if deterministic + online failed AND the
		# book has usable first-page text (LLM cannot work without it).
		if enriched is None and llm_provider is not None and diag.category in llm_categories:
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
	"""Extract content metadata, returning None on any failure."""
	if not meta.primary_file:
		return None
	try:
		return extract(meta.primary_file)
	except Exception as e:  # noqa: BLE001
		log.debug("extract failed for %s: %s", meta.path, e)
		return None


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
	"""Try cheap fixes: online lookup by ISBN, then embedded metadata.

	Returns an EnrichedMeta if we found something better than the current
	(broken) metadata, else None.
	"""
	# Prefer ISBN found in the book's text over the (likely missing/wrong) DB ISBN
	content_isbn = extracted.isbn or extracted.isbn_from_text

	# --- 2a. Online lookup by ISBN (cached, ~1s) ---
	if content_isbn and enricher is not None and not skip_enrich:
		online = enricher.lookup(isbn=content_isbn)
		if online is not None:
			# Only accept if it brings something the DB doesn't have
			if _is_better(online.title, meta.title) or _is_better(online.isbn, meta.isbn) or _is_better(online.year, meta.year):
				return online

	# --- 2b/2c. Embedded metadata from content (if cleaner than DB) ---
	# Note: embedded EPUB metadata is often a copy of the broken DB (calibre
	# wrote it back). So we ONLY trust embedded if it's clearly cleaner:
	# title without underscores, author that isn't "Neznamy", etc.
	proposal_title = extracted.title if _is_better(extracted.title, meta.title) else None
	proposal_authors = extracted.authors if any(_is_better(a, m) for a, m in zip(extracted.authors, meta.authors)) else None
	# Drop authors that are obvious garbage even in the extracted data
	if proposal_authors:
		proposal_authors = [a for a in proposal_authors if a and a != "Neznamy" and "_" not in a]

	if proposal_title or proposal_authors or (content_isbn and content_isbn != meta.isbn):
		from .enrichers import EnrichedMeta

		return EnrichedMeta(
			title=proposal_title,
			authors=proposal_authors or [],
			isbn=content_isbn if content_isbn != meta.isbn else None,
			source="embedded",
		)
	return None


def _is_better(candidate: str | None, current: str | None) -> bool:
	"""Is *candidate* a better (cleaner) value than *current*?

	A candidate is "better" if EITHER:
	  - the current is obviously broken (underscores, extensions, mojibake,
	    "Neznamy") and the candidate is not, OR
	  - the candidate has Czech/Slovak diacritics that the current lacks
	    (e.g. "Čas přílivu" beats "Cas prilivu" — same text, but with proper
	    diacritics). This catches the common case where Calibre stripped
	    diacritics but didn't replace it with underscores.
	"""
	if not candidate:
		return False
	if not current:
		return True
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


def _looks_broken(s: str) -> bool:
	"""Does *s* have obvious corruption signals?"""
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
		source=f"llm:{r.confidence}",
	)


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
		description=proposed.get("reasoning"),
		source=proposed.get("source") or "preserved",
	)


def _yaml_safe_load(path: Path):
	"""Load a YAML file, returning None on empty/invalid."""
	import yaml

	try:
		return yaml.safe_load(path.read_text(encoding="utf-8"))
	except yaml.YAMLError:
		return None


def apply_review(review_path: Path, library: Path, *, dry_run: bool = True) -> dict:
	"""Parse a review.yaml and apply approved changes to the library.

	Returns a summary dict: {applied, rejected, errors}.
	"""
	from .models import Diagnosis  # local to avoid cycle

	items = parse_review(review_path)
	summary = {"applied": 0, "rejected": 0, "errors": [], "dry_run": dry_run}

	for item in items:
		if item.action is None:
			# Not yet reviewed — skip silently
			continue
		if item.action == "reject":
			summary["rejected"] += 1
			continue
		if item.action not in ("accept", "swap", "edit"):
			summary["errors"].append(f"id={item.id}: unknown action {item.action!r}")
			continue

		# Reconstruct a BookMeta with the *current* values, then apply the fix
		folder = Path(item.path)
		if not folder.is_absolute():
			folder = library / item.path
		if not folder.is_dir():
			summary["errors"].append(f"id={item.id}: folder not found: {folder}")
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

	return summary


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
