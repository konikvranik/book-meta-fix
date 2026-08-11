"""Diagnose where metadata proposals are lost in the pipeline.

Runs ONLY the extraction stage (no LLM, no network) on a sample of books and
reports, for each detector category, how many books have:

  - empty first_page_text        (extractor found nothing to mine)
  - text but no text_meta signal (text_meta heuristics did not fire)
  - text_meta signal but no embedded ISBN/title
  - embedded metadata present (the cheap, deterministic path)

This isolates whether the parsing reliability problem is in:
  (a) the extractor producing empty text          -> fix extractors.py
  (b) text_meta heuristics missing real signals   -> fix text_meta.py
  (c) the pipeline gating good proposals out      -> fix pipeline/review

Usage:
  python scripts/parse_diagnostic.py [--limit 500] [--category C2]
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

# Allow running from repo root without install
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from book_meta_fix.config import Config
from book_meta_fix.detectors import detect as detect_fn
from book_meta_fix.extractors import extract
from book_meta_fix.library import Cache, scan_library

log = logging.getLogger("parse_diag")


def _book_file(folder: Path) -> Path | None:
    """Return the primary book file in a folder (epub > pdf > txt > other)."""
    prio = [".epub", ".pdf", ".txt", ".pdb", ".mobi", ".azw", ".doc", ".rtf"]
    files = [f for f in folder.iterdir() if f.is_file()]
    for ext in prio:
        for f in files:
            if f.suffix.lower() == ext:
                return f
    # any remaining book-like file
    for f in files:
        if f.suffix.lower() in (".lit", ".djvu"):
            return f
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500, help="max books to scan (0 = all)")
    ap.add_argument("--category", default=None, help="only books with this detector category")
    ap.add_argument("--format", default=None, help="only books with this file extension (e.g. .epub)")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    cfg = Config.from_env()
    cache = None if args.no_cache else Cache(cfg.cache_db)
    books = scan_library(cfg.library, cache=cache, use_cache=not args.no_cache)
    if cache:
        cache.close()

    # Filter to books with problems
    flagged = []
    for b in books:
        diag = detect_fn(b)
        if diag.category == "OK":
            continue
        if args.category and diag.category != args.category:
            continue
        flagged.append((b, diag.category))

    if args.limit and args.limit > 0:
        flagged = flagged[: args.limit]

    print(f"\n=== Sample: {len(flagged)} flagged books ===")
    cat_counter: Counter[str] = Counter()
    for _, cat in flagged:
        cat_counter[cat] += 1
    print("Categories:", dict(cat_counter))

    # Extraction stats
    stats = Counter()
    empty_text_samples: list[str] = []
    text_but_no_signal_samples: list[tuple[str, str]] = []  # (path, text head)
    by_format: Counter[str] = Counter()
    format_empty_text: Counter[str] = Counter()

    for b, _cat in flagged:
        book_file = _book_file(Path(b.path))
        if book_file is None:
            stats["no_book_file"] += 1
            continue
        ext = book_file.suffix.lower()
        if args.format and ext != args.format:
            continue
        by_format[ext] += 1
        ext_meta = extract(book_file)
        if ext_meta.error:
            stats[f"extract_error"] += 1
        if not ext_meta.first_page_text or len(ext_meta.first_page_text.strip()) < 10:
            stats["empty_first_page_text"] += 1
            format_empty_text[ext] += 1
            if len(empty_text_samples) < 10:
                empty_text_samples.append(f"{ext}\t{book_file.name}")
            continue
        # We have text. Did text_meta find anything?
        has_text_signal = any([
            ext_meta.title_from_text,
            ext_meta.authors_from_text,
            ext_meta.isbn_from_text,
            ext_meta.publisher_from_text,
            ext_meta.year_from_text,
        ])
        if not has_text_signal:
            stats["text_but_no_textmeta_signal"] += 1
            if len(text_but_no_signal_samples) < 15:
                head = ext_meta.first_page_text[:200].replace("\n", " ⏎ ")
                text_but_no_signal_samples.append((f"{ext} {book_file.name}", head))
        else:
            stats["text_meta_signal_found"] += 1
        # Embedded (OPF / pdfinfo / ebook-meta)
        if ext_meta.title or ext_meta.authors or ext_meta.isbn:
            stats["embedded_present"] += 1
        else:
            stats["embedded_missing"] += 1

    print("\n=== Extraction stats ===")
    for k, v in stats.most_common():
        print(f"  {k:35s} {v}")

    print("\n=== By format ===")
    for k, v in by_format.most_common():
        empty = format_empty_text.get(k, 0)
        print(f"  {k:8s} total={v:4d}  empty_text={empty:4d} ({empty*100//v if v else 0}%)")

    if empty_text_samples:
        print("\n=== Sample: empty first_page_text (format\\tfilename) ===")
        for s in empty_text_samples:
            print(f"  {s}")

    if text_but_no_signal_samples:
        print("\n=== Sample: text present but text_meta found nothing ===")
        for name, head in text_but_no_signal_samples:
            print(f"  [{name}]")
            print(f"    text: {head}")
            print()


if __name__ == "__main__":
    main()
