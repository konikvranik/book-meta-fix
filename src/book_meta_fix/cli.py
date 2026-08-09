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


@main.command()
@click.option("--library", "library", type=click.Path(exists=True, file_okay=False, path_type=Path), help="Library root")
@click.option("--no-cache", is_flag=True, help="Disable SQLite cache (force full re-parse)")
@click.option("--limit", type=int, default=None, help="Process only the first N books (for testing)")
@click.option("--skip-enrich", is_flag=True, default=True, help="Skip online enrichment (offline mode)")
@click.option("--skip-verify", is_flag=True, help="Skip content verification")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None, help="Output review file (default: review.yaml)")
@click.option("--llm", "use_llm", is_flag=True, help="Enable LLM reconciliation (needs ZAI_API_KEY or BMF_LLM_MOCK=1)")
@click.option("--llm-categories", default="C1,C4", help="Comma-separated categories to send to LLM (default: C1,C4)")
@click.option("--workers", "-w", type=int, default=10, help="Parallel workers for I/O (extract/LLM/enrich). Default 10.")
@click.option("--auto-apply", "auto_apply", is_flag=True, help="Auto-apply high-confidence LLM proposals directly (with snapshot + .bak). Lower-confidence go to review.yaml.")
@click.option("--auto-apply-threshold", default="high", help="Confidence threshold for auto-apply: high (default) | medium | low.")
@click.option("--snapshot-dir", default=None, help="Where to write the metadata tar.gz snapshot before auto-apply (default: CWD).")
def report(library: Path | None, no_cache: bool, limit: int | None, skip_enrich: bool, skip_verify: bool, output: Path | None, use_llm: bool, llm_categories: str, workers: int, auto_apply: bool, auto_apply_threshold: str, snapshot_dir: Path | None) -> None:
	"""Run full pipeline and generate a review.yaml for NEEDS_REVIEW books."""
	from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn

	from .enrichers import Enricher
	from .llm import get_provider
	from .pipeline import auto_apply_results, generate_review, run_pipeline

	cfg = Config.from_env()
	if library is not None:
		cfg.library = library
	out = output or cfg.review_file

	if auto_apply and not use_llm:
		console.print("[yellow]--auto-apply requires --llm (nothing to apply without LLM). Enabling --llm.[/yellow]")
		use_llm = True

	console.print(f"[bold]Running pipeline[/bold] on {cfg.library} ({workers} workers)")

	cache: Cache | None = None
	if not no_cache:
		cache = Cache(cfg.cache_db)

	enricher = None
	if not skip_enrich:
		enricher = Enricher(cache_db=cfg.cache_db)

	# LLM provider
	llm_provider = None
	if use_llm:
		llm_provider = get_provider(cfg)
		if llm_provider is None:
			console.print("[yellow]--llm given but no provider available (set ZAI_API_KEY or BMF_LLM_MOCK=1)[/yellow]")
		else:
			cats = tuple(c.strip() for c in llm_categories.split(",") if c.strip())
			console.print(f"  LLM: [cyan]{llm_provider.name}[/cyan] for categories {cats}")

	# Progress bar (updated from worker threads via callback)
	progress = Progress(
		SpinnerColumn(),
		TextColumn("[progress.description]{task.description}"),
		BarColumn(),
		TextColumn("{task.completed}/{task.total}"),
		TimeRemainingColumn(),
		console=console,
		transient=True,
	)
	task_id = progress.add_task("processing", total=None)
	progress.start()
	try:
		def _cb(done: int, total: int) -> None:
			if progress.tasks[0].total is None and total:
				progress.update(task_id, total=total)
			progress.update(task_id, completed=done)

		results = run_pipeline(
			cfg.library, cache=cache, enricher=enricher,
			skip_enrich=skip_enrich, skip_verify=skip_verify,
			llm_provider=llm_provider,
			llm_categories=tuple(c.strip() for c in llm_categories.split(",") if c.strip()) if use_llm else (),
			limit=limit,
			workers=workers,
			progress_callback=_cb,
		)
	finally:
		progress.stop()
		if cache is not None:
			cache.close()
		if enricher is not None:
			enricher.close()

	# Print pipeline summary
	_print_pipeline_summary(results)

	# Auto-apply high-confidence proposals (if --auto-apply)
	if auto_apply:
		from .writers import snapshot_metadata

		# Create safety snapshot before any writes
		snap_path = snapshot_metadata(cfg.library, output=Path(snapshot_dir) / f"metadata_snapshot.tar.gz" if snapshot_dir else None)
		console.print(f"[dim]Snapshot: {snap_path}[/dim]")
		summary = auto_apply_results(results, library_root=cfg.library, threshold=auto_apply_threshold, dry_run=False)
		console.print()
		t = Table(title="Auto-apply results", show_header=True, header_style="bold cyan")
		t.add_column("Metric", style="bold")
		t.add_column("Count", justify="right")
		t.add_row("Threshold", auto_apply_threshold)
		t.add_row("Applied (written)", str(summary["applied"]))
		t.add_row("Skipped (low confidence)", str(summary["skipped_low_conf"]))
		t.add_row("Skipped (no proposal)", str(summary["skipped_no_proposal"]))
		t.add_row("Remaining for review", str(len(summary["remaining"])))
		console.print(t)
		# Use remaining (non-applied) results for the review file
		results = summary["remaining"]

	# Generate the review file
	n = generate_review(results, library_root=cfg.library, output=out)
	console.print()
	console.print(f"[bold green]Wrote {n} review entries to {out}[/bold green]")
	console.print(f"Edit the file, set `action` for each entry, then run: [bold]bmf apply {out}[/bold]")


def _print_pipeline_summary(results) -> None:  # noqa: ANN001
	"""Print a summary table of the pipeline's verdicts."""
	from collections import Counter

	verdict_counter: Counter[str] = Counter()
	verify_counter: Counter[str] = Counter()
	for _meta, diag, verification, _enriched in results:
		verdict_counter[diag.verdict.value] += 1
		if verification is not None:
			verify_counter[verification.result] += 1
		else:
			verify_counter["(skipped)"] += 1

	total = len(results)
	console.print()
	t = Table(title="Pipeline summary", show_header=True, header_style="bold cyan")
	t.add_column("Verdict", style="bold")
	t.add_column("Count", justify="right")
	t.add_column("%", justify="right")
	for v, n in verdict_counter.most_common():
		t.add_row(v, str(n), f"{n / total * 100:.1f}%")
	console.print(t)

	t = Table(title="Verification results", show_header=True, header_style="bold cyan")
	t.add_column("Result")
	t.add_column("Count", justify="right")
	for r, n in verify_counter.most_common():
		t.add_row(r, str(n))
	console.print(t)


@main.command()
@click.argument("review_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--library", "library", type=click.Path(exists=True, file_okay=False, path_type=Path), help="Library root")
@click.option("--apply", "do_apply", is_flag=True, help="Actually write changes (default: dry-run)")
def apply(review_file: Path, library: Path | None, do_apply: bool) -> None:
	"""Apply approved changes from a (human-edited) review.yaml."""
	from .pipeline import apply_review

	cfg = Config.from_env()
	if library is not None:
		cfg.library = library

	console.print(f"[bold]Applying[/bold] {review_file} ({'WRITE' if do_apply else 'DRY-RUN'})")
	summary = apply_review(review_file, cfg.library, dry_run=not do_apply)
	console.print()
	t = Table(title="Apply summary", show_header=True, header_style="bold cyan")
	t.add_column("Metric", style="bold")
	t.add_column("Count", justify="right")
	t.add_row("Mode", "WRITE" if do_apply else "DRY-RUN")
	t.add_row("Applied", str(summary["applied"]))
	t.add_row("Rejected", str(summary["rejected"]))
	t.add_row("Errors", str(len(summary["errors"])))
	console.print(t)
	if summary["errors"]:
		console.print()
		console.print("[red]Errors:[/red]")
		for e in summary["errors"][:20]:
			console.print(f"  {e}")


@main.command()
@click.option("--library", "library", type=click.Path(exists=True, file_okay=False, path_type=Path), help="Library root")
@click.option("--no-cache", is_flag=True, help="Disable SQLite cache")
@click.option("--limit", type=int, default=None, help="Process only the first N books")
@click.option("--pattern", default=None, help="Path pattern for OK books (default: '{author}/{title} ({id})')")
@click.option("--needfix-dir", default=None, help="Folder for broken books (default: 'needfix')")
@click.option("--apply", "do_apply", is_flag=True, help="Actually move (default: dry-run)")
@click.option("--skip-verify", is_flag=True, help="Skip content verification (faster, less reliable)")
def organize(library: Path | None, no_cache: bool, limit: int | None, pattern: str | None, needfix_dir: str | None, do_apply: bool, skip_verify: bool) -> None:
	"""Move OK books to a clean path pattern and broken books to needfix/."""
	from .detectors import detect as detect_fn
	from .mover import DEFAULT_NEEDFIX_DIR, DEFAULT_PATH_PATTERN, organize as organize_fn
	from .verifier import verify

	cfg = Config.from_env()
	if library is not None:
		cfg.library = library
	pat = pattern or DEFAULT_PATH_PATTERN
	needfix = needfix_dir or DEFAULT_NEEDFIX_DIR

	console.print(f"[bold]Organizing[/bold] {cfg.library}")
	console.print(f"  OK pattern:    [cyan]{pat}[/cyan]")
	console.print(f"  needfix dir:   [cyan]{needfix}[/cyan]")
	console.print(f"  mode:          [{'WRITE' if do_apply else 'DRY-RUN'}]")

	cache: Cache | None = None
	if not no_cache:
		cache = Cache(cfg.cache_db)
	books = scan_library(cfg.library, cache=cache, use_cache=not no_cache)
	if cache is not None:
		cache.close()
	if limit is not None:
		books = books[:limit]

	# Determine verdict for each book
	console.print(f"\n[classifying] {len(books)} books...")
	verdicts = []
	for meta in books:
		diag = detect_fn(meta)
		# OK books get verified; if verification MISMATCHES, demote to NEEDS_REVIEW
		v = diag.verdict
		if v.value == "OK" and not skip_verify and meta.primary_file:
			try:
				ver = verify(meta)
				if ver.result == "MISMATCH":
					from .models import Verdict

					v = Verdict.NEEDS_REVIEW
			except Exception:  # noqa: BLE001
				pass
		verdicts.append((meta, v))

	results = organize_fn(
		verdicts, cfg.library,
		path_pattern=pat, needfix_dir=needfix, dry_run=not do_apply,
	)
	_print_organize_summary(results, verdicts)


def _print_organize_summary(results, verdicts) -> None:  # noqa: ANN001
	from collections import Counter

	from .models import Verdict

	# Verdict distribution
	vc: Counter[str] = Counter()
	for _m, v in verdicts:
		vc[v.value] += 1
	console.print()
	t = Table(title="Classification", show_header=True, header_style="bold cyan")
	t.add_column("Verdict")
	t.add_column("Count", justify="right")
	t.add_column("Destination", style="dim")
	for v, n in vc.most_common():
		dest = "OK path" if v in ("OK", "VERIFIED") else "needfix/"
		t.add_row(v, str(n), dest)
	console.print(t)

	# Move results
	rc: Counter[str] = Counter()
	for r in results:
		rc[r.action] += 1
	console.print()
	t = Table(title="Move results", show_header=True, header_style="bold cyan")
	t.add_column("Action")
	t.add_column("Count", justify="right")
	for a, n in rc.most_common():
		t.add_row(a, str(n))
	console.print(t)


@main.command()
@click.option("--library", "library", type=click.Path(exists=True, file_okay=False, path_type=Path), help="Library root")
@click.option("--no-cache", is_flag=True, help="Disable SQLite cache")
@click.option("--limit", type=int, default=None, help="Process only the first N books")
@click.option("--apply", "do_apply", is_flag=True, help="Actually generate EPUBs (default: dry-run)")
@click.option("--skip-verify", is_flag=True, help="Skip content verification (faster)")
def epubgen(library: Path | None, no_cache: bool, limit: int | None, do_apply: bool, skip_verify: bool) -> None:
	"""Generate EPUBs for OK books that lack one, from the best source format."""
	from .detectors import detect as detect_fn
	from .epubgen import generate_epub
	from .verifier import verify

	cfg = Config.from_env()
	if library is not None:
		cfg.library = library

	console.print(f"[bold]Generating EPUBs[/bold] for {cfg.library} [{'WRITE' if do_apply else 'DRY-RUN'}]")

	cache: Cache | None = None
	if not no_cache:
		cache = Cache(cfg.cache_db)
	books = scan_library(cfg.library, cache=cache, use_cache=not no_cache)
	if cache is not None:
		cache.close()
	if limit is not None:
		books = books[:limit]

	results = []
	skipped_not_ok = 0
	skipped_has_epub = 0
	for meta in books:
		# Only OK books (verified clean)
		diag = detect_fn(meta)
		if diag.verdict.value not in ("OK", "VERIFIED"):
			skipped_not_ok += 1
			continue
		if diag.verdict.value == "OK" and not skip_verify and meta.primary_file:
			try:
				ver = verify(meta)
				if ver.result == "MISMATCH":
					skipped_not_ok += 1
					continue
			except Exception:  # noqa: BLE001
				pass
		# Skip if already has epub
		if ".epub" in meta.formats:
			skipped_has_epub += 1
			continue
		result = generate_epub(meta, dry_run=not do_apply)
		results.append(result)

	_print_epubgen_summary(results, skipped_not_ok, skipped_has_epub)


def _print_epubgen_summary(results, skipped_not_ok: int, skipped_has_epub: int) -> None:  # noqa: ANN001
	from collections import Counter

	console.print()
	t = Table(title="EPUB generation summary", show_header=True, header_style="bold cyan")
	t.add_column("Metric", style="bold")
	t.add_column("Count", justify="right")
	t.add_row("Skipped (not OK)", str(skipped_not_ok))
	t.add_row("Skipped (already has epub)", str(skipped_has_epub))
	t.add_row("To generate", str(len(results)))
	console.print(t)

	if not results:
		return

	# Source format breakdown
	src: Counter[str] = Counter(r.source_format for r in results)
	console.print()
	t = Table(title="By source format", show_header=True, header_style="bold cyan")
	t.add_column("Format")
	t.add_column("Count", justify="right")
	for fmt, n in src.most_common():
		t.add_row(fmt, str(n))
	console.print(t)

	# Show sample (first 10)
	console.print()
	t = Table(title="Sample (first 10)", show_header=True, header_style="bold cyan")
	t.add_column("ID", justify="right", style="cyan")
	t.add_column("Source file")
	t.add_column("Tool")
	t.add_column("Output / Error", style="dim")
	for r in results[:10]:
		out = r.output_file or r.error or ""
		t.add_row(str(r.book_id or "?"), Path(r.source_file).name[:40], r.tool, out[:50])
	console.print(t)


# Required imports for the new commands
# (Enricher is imported lazily inside report() to avoid loading requests
#  when the user only runs scan/detect.)


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
