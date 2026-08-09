"""Command-line interface for book-meta-fix.

Subcommands implemented so far:
	bmf scan    — traverse the library, build cache, print summary stats

Upcoming (placeholders will be filled in later phases):
	bmf detect  — run C1–C10 detector rules
	bmf verify  — verify OK books against content
	bmf enrich  — online metadata lookup
	bmf apply   — apply changes from a review.yaml
"""
from __future__ import annotations

import logging
import sys
from collections import Counter
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import Config
from .library import Cache, scan_library

console = Console()
log = logging.getLogger(__name__)


def _setup_logging(verbose: bool) -> None:
	level = logging.DEBUG if verbose else logging.INFO
	logging.basicConfig(
		level=level,
		format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
		datefmt="%H:%M:%S",
	)


@click.group()
@click.version_option(__version__, prog_name="bmf")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
def main(verbose: bool) -> None:
	"""book-meta-fix: detect and fix metadata of ebooks."""
	_setup_logging(verbose)


@main.command()
@click.option("--library", "library", type=click.Path(exists=True, file_okay=False, path_type=Path), help="Library root (default: $BMF_LIBRARY or /mnt/share_nfs/Shared eBooks)")
@click.option("--no-cache", is_flag=True, help="Disable SQLite cache (force full re-parse)")
@click.option("--limit", type=int, default=None, help="Process only the first N books (for testing)")
def scan(library: Path | None, no_cache: bool, limit: int | None) -> None:
	"""Scan the library and print summary statistics."""
	cfg = Config.from_env()
	if library is not None:
		cfg.library = library

	console.print(f"[bold]Scanning[/bold] {cfg.library}")

	cache: Cache | None = None
	if not no_cache:
		cache = Cache(cfg.cache_db)

	books = scan_library(cfg.library, cache=cache, use_cache=not no_cache)

	if limit is not None:
		books = books[:limit]

	if not books:
		console.print("[red]No books found.[/red]")
		sys.exit(1)

	_print_scan_summary(books)

	if cache is not None:
		cache.close()


@main.command()
@click.option("--library", "library", type=click.Path(exists=True, file_okay=False, path_type=Path), help="Library root")
@click.option("--no-cache", is_flag=True, help="Disable SQLite cache (force full re-parse)")
@click.option("--limit", type=int, default=None, help="Process only the first N books (for testing)")
@click.option("--category", default=None, help="Show only books in this category (C1..C10, MISSING_ISBN, ...)")
@click.option("--samples", type=int, default=3, help="Number of sample books to show per category")
def detect(library: Path | None, no_cache: bool, limit: int | None, category: str | None, samples: int) -> None:
	"""Run detector rules and print category counts + samples."""
	from .detectors import detect as detect_fn

	cfg = Config.from_env()
	if library is not None:
		cfg.library = library

	cache: Cache | None = None
	if not no_cache:
		cache = Cache(cfg.cache_db)

	books = scan_library(cfg.library, cache=cache, use_cache=not no_cache)
	if limit is not None:
		books = books[:limit]
	if not books:
		console.print("[red]No books found.[/red]")
		sys.exit(1)

	# Run detectors
	results = [(b, detect_fn(b)) for b in books]
	if cache is not None:
		cache.close()

	_print_detect_summary(results, category, samples)


def _print_scan_summary(books) -> None:  # noqa: ANN001
	"""Print a rich table of scan statistics."""
	console.print()
	console.print(f"[bold green]Found {len(books)} books[/bold green]")

	# Source distribution
	src = Counter(b.source for b in books)
	# Format distribution
	fmt_counter: Counter[str] = Counter()
	for b in books:
		for f in b.formats:
			fmt_counter[f] += 1
	# Has ISBN / year
	has_isbn = sum(1 for b in books if b.isbn)
	has_year = sum(1 for b in books if b.year is not None)
	has_json = sum(1 for b in books if b.source.startswith("json"))
	enc_repaired = sum(1 for b in books if b.encoding_repaired)
	enc_unrepairable = sum(1 for b in books if b.encoding_unrepairable)

	t = Table(title="Scan summary", show_header=True, header_style="bold cyan")
	t.add_column("Metric", style="bold")
	t.add_column("Count", justify="right")
	t.add_column("%", justify="right")
	t.add_row("Total books", str(len(books)), "100%")
	t.add_row("With metadata.json", str(has_json), f"{has_json / len(books) * 100:.0f}%")
	t.add_row("With ISBN (valid)", str(has_isbn), f"{has_isbn / len(books) * 100:.0f}%")
	t.add_row("With publication year", str(has_year), f"{has_year / len(books) * 100:.0f}%")
	t.add_row("Mojibake repaired", str(enc_repaired), f"{enc_repaired / len(books) * 100:.0f}%")
	t.add_row("Mojibake unrepairable", str(enc_unrepairable), f"{enc_unrepairable / len(books) * 100:.0f}%")
	console.print(t)

	# Source table
	t = Table(title="Metadata source", show_header=True, header_style="bold cyan")
	t.add_column("Source")
	t.add_column("Count", justify="right")
	for src_name, n in src.most_common():
		t.add_row(src_name, str(n))
	console.print(t)

	# Format table
	t = Table(title="File formats present", show_header=True, header_style="bold cyan")
	t.add_column("Format")
	t.add_column("Books", justify="right")
	for fmt, n in fmt_counter.most_common():
		t.add_row(fmt, str(n))
	console.print(t)


def _print_detect_summary(results, category_filter: str | None, samples: int) -> None:  # noqa: ANN001
	"""Print detector results: category counts + sample books per category."""
	from collections import defaultdict

	# Aggregate by category
	by_cat: dict[str, list] = defaultdict(list)
	for meta, diag in results:
		by_cat[diag.category].append((meta, diag))

	# Sort: corruption categories first, then OK, then MISSING_*
	cat_order = ["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10", "OK", "MISSING_ISBN", "MISSING_YEAR"]
	all_cats = sorted(by_cat.keys(), key=lambda c: cat_order.index(c) if c in cat_order else 999)

	total = len(results)
	console.print()
	console.print(f"[bold green]Detected {total} books[/bold green]")

	# Summary table
	t = Table(title="Detection summary", show_header=True, header_style="bold cyan")
	t.add_column("Category", style="bold")
	t.add_column("Count", justify="right")
	t.add_column("%", justify="right")
	t.add_column("Verdict", style="magenta")
	for cat in all_cats:
		n = len(by_cat[cat])
		# Pick a representative verdict (the first one in the bucket)
		verdict = by_cat[cat][0][1].verdict.value
		t.add_row(cat, str(n), f"{n / total * 100:.1f}%", verdict)
	console.print(t)

	# Samples per category (filtered if --category given)
	cats_to_show = [category_filter] if category_filter else all_cats
	for cat in cats_to_show:
		if cat not in by_cat:
			console.print(f"[yellow]No books in category {cat}.[/yellow]")
			continue
		console.print()
		console.print(f"[bold]Category {cat}[/bold] — {len(by_cat[cat])} books (showing {min(samples, len(by_cat[cat]))}):")
		t = Table(show_header=True, header_style="bold", show_lines=False)
		t.add_column("ID", justify="right", style="cyan")
		t.add_column("Author folder")
		t.add_column("Title")
		t.add_column("Reason", style="dim")
		for meta, diag in by_cat[cat][:samples]:
			reason = diag.reason if len(diag.reason) <= 70 else diag.reason[:67] + "..."
			t.add_row(
				str(meta.calibre_id or "?"),
				(meta.author_folder or "")[:35],
				(meta.title or "")[:45],
				reason,
			)
		console.print(t)


if __name__ == "__main__":
	main()
