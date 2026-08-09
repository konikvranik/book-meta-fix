"""Pipeline orchestration: detect -> extract -> verify -> enrich -> review/write.

The pipeline glues together all phases and exposes high-level functions used
by the CLI:
	run_pipeline()   - full scan+detect+extract+verify+enrich, returns list of (meta, diag, verif, enriched)
	generate_review() - emit review.yaml for NEEDS_REVIEW items
	apply_review()   - parse a (human-edited) review.yaml and write changes
"""
from __future__ import annotations

import logging
from pathlib import Path

from .detectors import detect as detect_fn
from .enrichers import Enricher
from .extractors import extract
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
) -> list[tuple[BookMeta, "Diagnosis", "Verification | None", "EnrichedMeta | None"]]:  # noqa: F821
	"""Run the full pipeline over the whole library.

	Returns a list of (meta, diagnosis, verification, enriched) tuples.
	"""
	books = scan_library(library, cache=cache)

	results = []
	for meta in books:
		diag = detect_fn(meta)

		verification = None
		enriched = None

		# Only verify+enrich books that look OK structurally (the rest are
		# already flagged for review by the detector).
		if diag.verdict == Verdict.OK and not skip_verify and meta.primary_file:
			try:
				verification = verify(meta)
			except Exception as e:  # noqa: BLE001
				log.debug("verify failed for %s: %s", meta.path, e)

		# Enrich: try for books that are AUTO_FIXABLE or NEEDS_REVIEW (missing data)
		if enricher is not None and not skip_enrich:
			need_enrich = diag.category in ("MISSING_ISBN", "MISSING_YEAR") or diag.verdict == Verdict.NEEDS_REVIEW
			if need_enrich:
				try:
					# Prefer ISBN from verification (extracted from content) over DB
					isbn_for_lookup = None
					if verification and verification.extracted:
						isbn_for_lookup = verification.extracted.isbn or verification.extracted.isbn_from_text
					isbn_for_lookup = isbn_for_lookup or meta.isbn
					enriched = enricher.lookup(isbn=isbn_for_lookup, title=meta.title, author=meta.authors[0] if meta.authors else None)
				except Exception as e:  # noqa: BLE001
					log.debug("enrich failed for %s: %s", meta.path, e)

		results.append((meta, diag, verification, enriched))

	return results


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
