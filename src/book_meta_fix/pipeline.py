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
) -> list[tuple[BookMeta, "Diagnosis", "Verification | None", "EnrichedMeta | None"]]:  # noqa: F821
	"""Run the full pipeline over the whole library.

	Returns a list of (meta, diagnosis, verification, enriched) tuples.

	If *limit* is given, only the first N books (in scan order) are processed.
	This is applied at the scan stage, so the expensive extraction/LLM work
	isn't wasted on books the caller doesn't want.
	"""
	books = scan_library(library, cache=cache)
	if limit is not None:
		books = books[:limit]
	stats = {"ok": 0, "needs_review": 0, "det_fixed": 0, "online_fixed": 0, "llm_fixed": 0, "unfixed": 0}

	results = []
	for meta in books:
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

			# Step 4: LLM fallback only if deterministic + online failed
			if enriched is None and llm_provider is not None and diag.category in llm_categories:
				try:
					evidence = _build_llm_evidence(meta, diag, extracted)
					reconciled = llm_provider.reconcile(evidence)
					if reconciled is not None and _reconciled_is_useful(reconciled, meta):
						enriched = _reconciled_to_enriched(reconciled)
						stats["llm_fixed"] += 1
				except Exception as e:  # noqa: BLE001
					log.debug("LLM reconcile failed for %s: %s", meta.path, e)

			if enriched is None:
				stats["unfixed"] += 1

		results.append((meta, diag, verification, enriched))

	log.info(
		"pipeline: %d ok, %d needs_review (det=%d, online=%d, llm=%d, unfixed=%d)",
		stats["ok"], stats["needs_review"], stats["det_fixed"], stats["online_fixed"], stats["llm_fixed"], stats["unfixed"],
	)
	return results


def _safe_extract(meta: BookMeta) -> ExtractedMeta | None:
	"""Extract content metadata, returning None on any failure."""
	if not meta.primary_file:
		return None
	try:
		return extract(meta.primary_file)
	except Exception as e:  # noqa: BLE001
		log.debug("extract failed for %s: %s", meta.path, e)
		return None


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
	"""Did the LLM produce anything better than what we already had?"""
	# Useful if it proposed a title different from current, OR added ISBN/year,
	# OR changed authors. If it just echoed current values, skip.
	if r.title and r.title != meta.title:
		return True
	if r.isbn and r.isbn != meta.isbn:
		return True
	if r.year and r.year != meta.year:
		return True
	if r.authors and r.authors != meta.authors:
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
		source=f"llm:{r.confidence}",
	)


def generate_review(
	results: list,
	*,
	library_root: Path,
	output: Path,
) -> int:
	"""Write a review.yaml from pipeline results. Returns the count of entries."""
	# Build items for review: NEEDS_REVIEW + UNFIXABLE + verification=MISMATCH
	items_for_review = []
	for meta, diag, verification, enriched in results:
		include = diag.verdict.value in ("NEEDS_REVIEW", "UNFIXABLE")
		if verification and verification.result == "MISMATCH":
			include = True
		if include:
			extracted = verification.extracted if verification else None
			items_for_review.append((meta, diag, extracted, enriched))

	yaml_text = build_review(items_for_review, library_root=library_root)
	output.write_text(yaml_text, encoding="utf-8")
	log.info("wrote %d review entries to %s", len(items_for_review), output)
	return len(items_for_review)


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
