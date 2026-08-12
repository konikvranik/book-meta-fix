"""Command-line interface for book-meta-fix.

Subcommands:
	bmf scan       — traverse the library, build cache, print summary stats
	bmf report     — run C1–C10 detector rules, show category counts + samples
	bmf analyze    — full pipeline (detect + extract + verify + enrich + LLM)
	                 and generate a review.yaml for NEEDS_REVIEW books
	bmf apply      — apply approved changes from a review.yaml
	bmf organize   — move OK books to a clean path, broken books to needfix/
	bmf epubgen    — generate missing .epub files from other formats
	bmf crosscheck — verify all formats in a folder are the same book;
	                 quarantine format files whose content differs from metadata
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
	# Silence chatty third-party HTTP/SDK loggers unless --verbose. These log
	# every request at INFO (httpx) and every retry at INFO (openai), which
	# drowns the progress bar and our own logs during LLM runs.
	if not verbose:
		for name in ("httpx", "openai", "openai._base_client", "urllib3", "httpcore"):
			logging.getLogger(name).setLevel(logging.WARNING)


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
	from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn

	cfg = Config.from_env()
	if library is not None:
		cfg.library = library

	console.print(f"[bold]Scanning[/bold] [cyan]{cfg.library}[/cyan]", highlight=False)

	cache: Cache | None = None
	if not no_cache:
		cache = Cache(cfg.cache_db)

	# scan_library walks the tree once to learn the folder count, then reports
	# (done, total) per folder — so the bar starts indeterminate and gains a
	# total + ETA on the first callback (no separate pre-count walk needed).
	with Progress(
		SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
		BarColumn(complete_style="cyan", finished_style="cyan", pulse_style="cyan"), TextColumn("{task.completed}/{task.total}"),
		TimeRemainingColumn(), console=console, transient=True,
	) as progress:
		task_id = progress.add_task("Scanning", total=None)

		def _cb(done: int, total: int) -> None:
			if progress.tasks[0].total is None and total:
				progress.update(task_id, total=total)
			progress.update(task_id, completed=done)

		books = scan_library(cfg.library, cache=cache, use_cache=not no_cache, progress_callback=_cb)

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
def report(library: Path | None, no_cache: bool, limit: int | None, category: str | None, samples: int) -> None:
	"""Run detector rules and print category counts + samples."""
	from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn

	from .detectors import detect as detect_fn

	cfg = Config.from_env()
	if library is not None:
		cfg.library = library

	cache: Cache | None = None
	if not no_cache:
		cache = Cache(cfg.cache_db)

	# Reading metadata for the whole library is I/O-heavy (especially on NFS);
	# wrap it so there's no silent gap before the detect pass.
	with Progress(
		SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
		BarColumn(complete_style="magenta", finished_style="magenta", pulse_style="magenta"), TextColumn("{task.completed}/{task.total}"),
		TimeRemainingColumn(), console=console, transient=True,
	) as progress:
		task_id = progress.add_task("Reading library", total=None)

		def _scan_cb(done: int, total: int) -> None:
			if progress.tasks[0].total is None and total:
				progress.update(task_id, total=total)
			progress.update(task_id, completed=done)

		books = scan_library(cfg.library, cache=cache, use_cache=not no_cache, progress_callback=_scan_cb)
	if limit is not None:
		books = books[:limit]
	if not books:
		console.print("[red]No books found.[/red]")
		sys.exit(1)

	# Run detectors (the per-book classify pass — wrap with a progress bar/ETA).
	results = []
	with Progress(
		SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
		BarColumn(complete_style="magenta", finished_style="magenta", pulse_style="magenta"), TextColumn("{task.completed}/{task.total}"),
		TimeRemainingColumn(), console=console, transient=True,
	) as progress:
		task_id = progress.add_task("Detecting", total=len(books))
		for i, b in enumerate(books, start=1):
			results.append((b, detect_fn(b)))
			progress.update(task_id, completed=i)
	if cache is not None:
		cache.close()

	_print_detect_summary(results, category, samples)


@main.command()
@click.option("--library", "library", type=click.Path(exists=True, file_okay=False, path_type=Path), help="Library root")
@click.option("--no-cache", is_flag=True, help="Disable SQLite cache (force full re-parse)")
@click.option("--limit", type=int, default=None, help="Process only the first N books (for testing)")
@click.option("--skip-enrich", is_flag=True, default=True, help="Skip online enrichment (offline mode)")
@click.option("--databazeknih", "use_databazeknih", is_flag=True, help="Enable databazeknih.cz lookup (CZ/SK genres + metadata). Implies --no-skip-enrich.")
@click.option("--skip-verify", is_flag=True, help="Skip content verification")
@click.option("--verify-ok", "verify_ok", is_flag=True, help="Audit: also verify books the detectors marked OK against their content. Reads every OK book's file (slower). A MISMATCH reclassifies it to NEEDS_REVIEW and seeks a fix (enrichment + LLM). Use periodically to catch corruption the structural detectors miss.")
@click.option("--no-strict-verify", "no_strict_verify", is_flag=True, help="With --verify-ok: only reclassify a clear MISMATCH (fuzzy title < 0.5). By default (without this flag) UNCERTAIN (0.5–0.8) is also reclassified.")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None, help="Output review file (default: review.yaml)")
@click.option("--llm", "use_llm", is_flag=True, help="Enable LLM reconciliation (needs ZAI_API_KEY or BMF_LLM_MOCK=1)")
@click.option("--llm-categories", default="ALL", help="Comma-separated categories to send to LLM, or 'ALL' (default). ALL = every category except C9 (legitimate anonyms like the Bible, where an LLM-invented author would be wrong). Each book is one LLM request that returns all fields at once, so the cost is per-book, not per-category.")
@click.option("--workers", "-w", type=int, default=10, help="Parallel workers for I/O (extract/LLM/enrich). Default 10.")
@click.option("--llm-min-interval", "llm_min_interval", type=float, default=None, help="Minimum seconds between LLM requests (RPM throttle, default 2.0 = ~30 RPM). Decoupled from --workers: cheap I/O still runs at full worker count. Lower (e.g. 1.0 = 60 RPM) on a higher Z.AI tier; raise (e.g. 4.0 = 15 RPM) if you still hit 429.")
@click.option("--llm-model", "llm_model", default=None, help="Z.AI model for LLM fallback (default glm-5.2). Alternatives: glm-4.6, glm-4.5-air, glm-4.5-flash, glm-4.7-flash. See README 'LLM model choice' for the token/quality tradeoffs measured by scripts/llm_experiment.py.")
@click.option("--llm-reasoning-effort", "llm_reasoning_effort", default=None, help="reasoning_effort for GLM-5.x models: low (default) | medium | max. Lower cuts reasoning tokens ~60%% vs max. Ignored by GLM-4.x (use --llm-thinking).")
@click.option("--llm-thinking", "llm_thinking", default=None, help="thinking toggle for GLM-4.x models: disabled (default) | enabled. 'disabled' turns off chain-of-thought (3-4x fewer output tokens). Ignored by GLM-5.x (use --llm-reasoning-effort).")
@click.option("--no-llm-loop", "no_llm_loop", is_flag=True, help="Disable the self-correction loop. Default: loop on — try the free Flash model first (with verify feedback), then the paid final model as fallback. With this flag, a single LLM call (the configured --llm-model) is used as before.")
@click.option("--llm-flash-model", "llm_flash_model", default=None, help="Free first-attempt model for the loop (default glm-4.7-flash — best CZ/SK quality among free models). Alternatives: glm-4.5-flash, glm-4.5-air.")
@click.option("--llm-final-model", "llm_final_model", default=None, help="Paid high-quality fallback for the loop (default glm-5.2). Used when Flash fails verify or is rate-limited.")
@click.option("--llm-burst", "llm_burst", type=float, default=None, help="Leaky-bucket burst capacity: how many LLM calls may start inside one interval (default 1 = pure even drip, no bunching — one call every --llm-min-interval seconds). This is a count-per-time limiter, not a concurrency cap. Raise only with confirmed rate headroom; a burst >1 fires multiple calls in the same second and trips Z.AI's dynamic RPM limit (429).")
@click.option("--llm-rate-limit-base", "llm_rate_limit_base", type=float, default=None, help="Base seconds of the global cooldown applied when a 429 is seen (default 5). When ANY worker hits a 429, ALL workers pause this long; the cooldown escalates 5/10/20/... with consecutive 429s, honours the server Retry-After when longer, and is capped by --llm-rate-limit-max. Higher = safer but slower; lower = more 429 risk.")
@click.option("--llm-rate-limit-max", "llm_rate_limit_max", type=float, default=None, help="Cap (seconds) on the escalating 429 cooldown (default 60). Prevents a sustained outage from parking workers indefinitely.")
def analyze(library: Path | None, no_cache: bool, limit: int | None, skip_enrich: bool, use_databazeknih: bool, skip_verify: bool, verify_ok: bool, no_strict_verify: bool, output: Path | None, use_llm: bool, llm_categories: str, workers: int, llm_min_interval: float | None, llm_model: str | None, llm_reasoning_effort: str | None, llm_thinking: str | None, no_llm_loop: bool, llm_flash_model: str | None, llm_final_model: str | None, llm_burst: float | None, llm_rate_limit_base: float | None, llm_rate_limit_max: float | None) -> None:
	"""Run full pipeline and generate a review.yaml for NEEDS_REVIEW books."""
	from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn

	from .enrichers import Enricher
	from .llm import get_provider
	from .pipeline import run_pipeline

	cfg = Config.from_env()
	if library is not None:
		cfg.library = library
	out = output or cfg.review_file

	# --databazeknih turns enrichment on (and opts the CZ/SK scraper in).
	if use_databazeknih:
		skip_enrich = False
		cfg.databazeknih_enabled = True

	console.print(f"[bold]Running pipeline[/bold] on [cyan]{cfg.library}[/cyan] ({workers} workers)", highlight=False)
	if cfg.databazeknih_enabled:
		console.print("  [cyan]databazeknih.cz[/cyan] lookup enabled (genres + metadata)")
		console.print("  [cyan]cover replacement[/cyan] enabled (C11 generated / MISSING_COVER → databazeknih cover_url)")
	if verify_ok:
		strict = not no_strict_verify
		console.print(f"  [cyan]--verify-ok[/cyan] audit: OK books checked against content (strict={strict})")

	cache: Cache | None = None
	if not no_cache:
		cache = Cache(cfg.cache_db)

	enricher = None
	if not skip_enrich:
		enricher = Enricher(
			cache_db=cfg.cache_db,
			databazeknih_enabled=cfg.databazeknih_enabled,
			openlibrary_enabled=cfg.openlibrary_enabled,
			google_books_enabled=cfg.google_books_enabled,
			negative_ttl_sec=cfg.enrich_negative_ttl_sec,
		)

	# LLM provider
	llm_provider = None
	if use_llm:
		if llm_min_interval is not None:
			cfg.llm_min_interval = llm_min_interval
		if llm_model is not None:
			cfg.zai_model = llm_model
		if llm_reasoning_effort is not None:
			cfg.zai_reasoning_effort = llm_reasoning_effort
		if llm_thinking is not None:
			cfg.zai_thinking = llm_thinking
		if no_llm_loop:
			cfg.llm_loop = False
		if llm_flash_model is not None:
			cfg.zai_flash_model = llm_flash_model
		if llm_final_model is not None:
			cfg.zai_final_model = llm_final_model
		if llm_burst is not None:
			cfg.llm_burst = llm_burst
		if llm_rate_limit_base is not None:
			cfg.llm_rate_limit_base = llm_rate_limit_base
		if llm_rate_limit_max is not None:
			cfg.llm_rate_limit_max = llm_rate_limit_max
		llm_provider = get_provider(cfg)
		if llm_provider is None:
			console.print("[yellow]--llm given but no provider available (set ZAI_API_KEY or BMF_LLM_MOCK=1)[/yellow]")
		else:
			cats = tuple(c.strip() for c in llm_categories.split(",") if c.strip())
			rpm = round(60.0 / cfg.llm_min_interval) if cfg.llm_min_interval > 0 else float("inf")
			if cfg.llm_loop:
				# Loop mode (default): free Flash first, paid final as fallback.
				console.print(
					f"  LLM: [cyan]{llm_provider.name}[/cyan] "
					f"primary={cfg.zai_flash_model} → fallback={cfg.zai_final_model} "
					f"(reasoning_effort={cfg.zai_reasoning_effort}) "
					f"for categories {cats} (≤{rpm} RPM, min {cfg.llm_min_interval}s between calls)"
				)
			else:
				# Single-call mode (--no-llm-loop): one model, no fallback.
				is_glm5 = cfg.zai_model.lower().startswith("glm-5")
				reason = f"reasoning_effort={cfg.zai_reasoning_effort}" if is_glm5 else f"thinking={cfg.zai_thinking}"
				console.print(f"  LLM: [cyan]{llm_provider.name}[/cyan] model={cfg.zai_model} ({reason}) for categories {cats} (≤{rpm} RPM, min {cfg.llm_min_interval}s between calls)")

	# Streaming review writer: appends each processed book to review.yaml as it
	# completes (Unix-pipe style). The original is moved to .bak on
	# construction so user decisions are preserved; finish() carries over any
	# unprocessed prior entries and deletes .bak on success.
	from .review_writer import ReviewWriter

	review_writer = ReviewWriter(out, library_root=cfg.library)

	# Progress bar (updated from worker threads via callback)
	progress = Progress(
		SpinnerColumn(),
		TextColumn("[progress.description]{task.description}"),
		BarColumn(complete_style="blue", finished_style="blue", pulse_style="blue"),
		TextColumn("{task.completed}/{task.total}"),
		TimeRemainingColumn(),
		console=console,
		transient=True,
	)
	task_id = progress.add_task("processing", total=None)
	progress.start()
	results: list = []
	interrupted = False
	# Populated by run_pipeline (passed in) so we can print a fix-source
	# breakdown after the run. The dict is seeded with all keys inside
	# run_pipeline, so it's safe to read here even on early failure.
	pipe_stats: dict = {}
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
			review_writer=review_writer,
			verify_ok=verify_ok,
			strict_verify=not no_strict_verify,
			llm_loop=cfg.llm_loop,
			stats=pipe_stats,
		)
	except KeyboardInterrupt:
		# A second Ctrl-C (or one that escaped run_pipeline's internal handler).
		# The streaming writer has already flushed everything up to the point of
		# interruption; finish() below carries over prior unprocessed entries.
		interrupted = True
		console.print("\n[yellow]Interrupted (Ctrl-C). Finalizing review file with partial results…[/yellow]")
	finally:
		progress.stop()
		if cache is not None:
			cache.close()
		if enricher is not None:
			enricher.close()
		# Always finalize the writer — even on Ctrl-C/error — so the review file
		# is consistent and prior decisions are carried over. keep_backup when
		# interrupted, so the user can recover the pre-run state if needed.
		try:
			summary = review_writer.finish(keep_backup=interrupted)
		except Exception as e:  # noqa: BLE001
			console.print(f"[red]review writer finalize failed: {e}[/red]")
			summary = {"written": 0, "skipped_user_decided": 0, "remaining_count": 0, "backup_path": None}

	# Print pipeline summary (from the results list — still populated for stats)
	_print_pipeline_summary(results, pipe_stats)

	# Streaming review summary
	console.print()
	t = Table(title="Review results", show_header=True, header_style="bold cyan")
	t.add_column("Metric", style="bold")
	t.add_column("Count", justify="right")
	t.add_row("Skipped (user already decided)", str(summary["skipped_user_decided"]))
	t.add_row("Written to review", str(summary["written"]))
	console.print(t)
	if summary.get("backup_path"):
		console.print(f"[yellow]Backup kept at {summary['backup_path']} (run did not finish cleanly)[/yellow]", highlight=False)
	console.print()
	console.print(f"[bold green]Wrote {summary['written']} review entries to {out}[/bold green]", highlight=False)
	console.print(f"Edit the file, set `action` for each entry, then run: [bold]bmf apply {out}[/bold]", highlight=False)


def _print_pipeline_summary(results, stats: dict | None = None) -> None:  # noqa: ANN001
	"""Print a summary table of the pipeline's verdicts and fix sources.

	*stats* (from run_pipeline) drives the fix-source breakdown table; when
	None (e.g. when run_pipeline wasn't used), only the verdict/verification
	tables are printed.
	"""
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

	if stats:
		_print_fix_source_summary(stats)


def _print_fix_source_summary(stats: dict) -> None:
	"""Print a breakdown of how NEEDS_REVIEW books were fixed (offline / online
	/ LLM / unfixed). Driven by the stats dict filled by run_pipeline.

	Each row is a fix source; the right column shows how many books that source
	fixed. Rows with zero counts are hidden to keep the table readable.
	"""
	# Parent rows first, then their breakdown (indented with └). Zero-count
	# sub-rows are dropped below to keep the table tight.
	offline_total = stats.get("det_fixed", 0)
	online_total = stats.get("online_fixed", 0)
	llm_total = stats.get("llm_flash_fixed", 0) + stats.get("llm_final_fixed", 0) + stats.get("llm_low_confidence", 0)

	ordered: list[tuple[str, int, bool]] = [
		("Offline fixes", offline_total, False),
		("  └ text-mined (content)", stats.get("offline_content", 0), True),
		("  └ embedded OPF", stats.get("offline_embedded", 0), True),
		("Online fixes", online_total, False),
		("  └ databazeknih.cz", stats.get("online_databazeknih", 0), True),
		("  └ openlibrary.org", stats.get("online_openlibrary", 0), True),
		("  └ Google Books", stats.get("online_google_books", 0), True),
		("LLM fixes", llm_total, False),
		("  └ fast model (flash)", stats.get("llm_flash_fixed", 0), True),
		("  └ fallback model", stats.get("llm_final_fixed", 0), True),
		("  └ low confidence", stats.get("llm_low_confidence", 0), True),
	]
	# Drop all-zero sub-rows to keep the table tight, but always show parents.
	ordered = [
		(label, n, sub) for (label, n, sub) in ordered
		if not sub or n > 0
	]

	unfixed = stats.get("unfixed", 0)
	llm_skipped = stats.get("llm_skipped_no_text", 0)
	llm_no_result = stats.get("llm_no_result", 0)
	llm_error = stats.get("llm_error", 0)
	proposed_total = offline_total + online_total + llm_total

	console.print()
	t = Table(title="Fix sources (how NEEDS_REVIEW books were resolved)", show_header=True, header_style="bold cyan")
	t.add_column("Source", style="bold")
	t.add_column("Count", justify="right")
	for label, n, _sub in ordered:
		t.add_row(label, str(n))
	t.add_section()
	t.add_row("Proposed (any source)", str(proposed_total), style="bold")
	t.add_row("Unfixed (no proposal found)", str(unfixed), style="yellow")
	console.print(t)

	# LLM cost detail: how many books the LLM was asked about vs. how many it
	# actually fixed. Only shown when an LLM provider was in play.
	if any(stats.get(k) for k in ("llm_flash_fixed", "llm_final_fixed", "llm_low_confidence", "llm_skipped_no_text", "llm_no_result", "llm_error")):
		llm_asked = llm_total + llm_no_result + llm_error
		console.print()
		t = Table(title="LLM usage", show_header=True, header_style="bold cyan")
		t.add_column("Metric", style="bold")
		t.add_column("Count", justify="right")
		t.add_row("LLM calls made", str(llm_asked))
		t.add_row("Skipped (no usable text)", str(llm_skipped))
		t.add_row("No useful result", str(llm_no_result))
		if llm_error:
			t.add_row("LLM errors", str(llm_error), style="red")
		console.print(t)

	# Covers: only show when cover detection ran (counts > 0).
	covers_gen = stats.get("covers_generated", 0)
	covers_missing = stats.get("covers_missing", 0)
	if covers_gen or covers_missing:
		console.print()
		t = Table(title="Covers", show_header=True, header_style="bold cyan")
		t.add_column("Category", style="bold")
		t.add_column("Count", justify="right")
		t.add_row("Generated (calibre placeholder)", str(covers_gen), style="yellow")
		t.add_row("Missing cover", str(covers_missing))
		console.print(t)

	if stats.get("errors"):
		console.print(f"\n[red]Processing errors: {stats['errors']}[/red]")


@main.command()
@click.argument("review_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--library", "library", type=click.Path(exists=True, file_okay=False, path_type=Path), help="Library root")
@click.option("--apply", "do_apply", is_flag=True, help="Actually write changes (default: dry-run)")
def apply(review_file: Path, library: Path | None, do_apply: bool) -> None:
	"""Apply approved changes from a (human-edited) review.yaml."""
	from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn

	from .pipeline import apply_review

	cfg = Config.from_env()
	if library is not None:
		cfg.library = library

	console.print(f"[bold]Applying[/bold] {review_file} ({'WRITE' if do_apply else 'DRY-RUN'})", highlight=False)
	# Open the books cache (if one exists) so we can invalidate the folders we
	# rewrite — otherwise the next run may serve the pre-apply BookMeta, most
	# painfully on NFS where the attribute cache masks the new mtime.
	cache: Cache | None = Cache(cfg.cache_db) if cfg.cache_db.is_file() else None
	progress = Progress(
		SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
		BarColumn(complete_style="green", finished_style="green", pulse_style="green"), TextColumn("{task.completed}/{task.total}"),
		TimeRemainingColumn(), console=console, transient=True,
	)
	task_id = progress.add_task("Applying", total=None)
	progress.start()
	try:
		def _cb(done: int, total: int) -> None:
			if progress.tasks[0].total is None and total:
				progress.update(task_id, total=total)
			progress.update(task_id, completed=done)

		summary = apply_review(review_file, cfg.library, dry_run=not do_apply, cache=cache, progress_callback=_cb)
	finally:
		progress.stop()
		if cache is not None:
			cache.close()
	console.print()
	t = Table(title="Apply summary", show_header=True, header_style="bold cyan")
	t.add_column("Metric", style="bold")
	t.add_column("Count", justify="right")
	t.add_row("Mode", "WRITE" if do_apply else "DRY-RUN")
	t.add_row("Applied", str(summary["applied"]))
	t.add_row("Rejected", str(summary["rejected"]))
	t.add_row("Deleted", str(summary.get("deleted", 0)))
	if summary.get("snapshot"):
		t.add_row("Deletion snapshot", summary["snapshot"])
	if summary.get("pruned"):
		t.add_row("Pruned from review", str(summary["pruned"]))
		t.add_row("Remaining in review", str(summary["remaining"]))
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
	from .mover import DEFAULT_NEEDFIX_DIR, DEFAULT_PATH_PATTERN
	from .mover import organize as organize_fn
	from .verifier import verify

	cfg = Config.from_env()
	if library is not None:
		cfg.library = library
	pat = pattern or DEFAULT_PATH_PATTERN
	needfix = needfix_dir or DEFAULT_NEEDFIX_DIR

	console.print(f"[bold]Organizing[/bold] [cyan]{cfg.library}[/cyan]", highlight=False)
	console.print(f"  OK pattern:    [cyan]{pat}[/cyan]")
	console.print(f"  needfix dir:   [cyan]{needfix}[/cyan]")
	console.print(f"  mode:          [{'WRITE' if do_apply else 'DRY-RUN'}]")

	cache: Cache | None = None
	if not no_cache:
		cache = Cache(cfg.cache_db)
	try:
		from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn

		# Reading metadata for the whole library is the slow, silent gap before
		# classify — wrap it in a bar so it isn't a dead spot.
		with Progress(
			SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
			BarColumn(complete_style="yellow", finished_style="yellow", pulse_style="yellow"), TextColumn("{task.completed}/{task.total}"),
			TimeRemainingColumn(), console=console, transient=True,
		) as progress:
			task_id = progress.add_task("Reading library", total=None)

			def _scan_cb(done: int, total: int) -> None:
				if progress.tasks[0].total is None and total:
					progress.update(task_id, total=total)
				progress.update(task_id, completed=done)

			books = scan_library(cfg.library, cache=cache, use_cache=not no_cache, progress_callback=_scan_cb)
		if limit is not None:
			books = books[:limit]

		# Classify each book: detector rules + content verify for OK books (a
		# MISMATCH demotes to NEEDS_REVIEW). verify() reads the book file, so on
		# NFS this phase can take a while — wrap it in a progress bar.
		verdicts = []
		with Progress(
			SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
			BarColumn(complete_style="yellow", finished_style="yellow", pulse_style="yellow"), TextColumn("{task.completed}/{task.total}"),
			TimeRemainingColumn(), console=console, transient=True,
		) as progress:
			task_id = progress.add_task("Classifying", total=len(books))
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
				progress.update(task_id, advance=1)

		# Move books (verdict-driven) with a progress bar + ETA. cache is passed
		# down so organize can invalidate the folders it moves.
		with Progress(
			SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
			BarColumn(complete_style="yellow", finished_style="yellow", pulse_style="yellow"), TextColumn("{task.completed}/{task.total}"),
			TimeRemainingColumn(), console=console, transient=True,
		) as progress:
			move_task = progress.add_task("Moving books", total=len(verdicts))

			def _move_cb(done: int, total: int) -> None:
				progress.update(move_task, completed=done)

			results = organize_fn(
				verdicts, cfg.library,
				path_pattern=pat, needfix_dir=needfix, dry_run=not do_apply, cache=cache,
				progress_callback=_move_cb,
			)
		_print_organize_summary(results, verdicts)
	finally:
		if cache is not None:
			cache.close()


def _print_organize_summary(results, verdicts) -> None:  # noqa: ANN001
	from collections import Counter


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

	# Collisions: same-book duplicates that were merged (one folder per work,
	# all formats combined). Show a sample so the user can audit what merged
	# into what. Disambiguated/dup-N moves show up as plain 'moved'/
	# 'collision_renamed' above (their destination name carries the suffix).
	merges = [r for r in results if r.action == "merged"]
	if merges:
		console.print()
		t = Table(title=f"Merges ({len(merges)} folder{'s' if len(merges) != 1 else ''} merged away)", show_header=True, header_style="bold cyan")
		t.add_column("Loser (merged in)")
		t.add_column("→ Winner")
		t.add_column("Formats moved", style="dim")
		for r in merges[:25]:
			moved = r.details or []
			n_moved = sum(1 for _, out in moved if not out.startswith("<skipped"))
			t.add_row(Path(r.source).name[:40], Path(r.destination).name[:40], f"{n_moved} file(s)")
		if len(merges) > 25:
			t.add_row(f"… ({len(merges) - 25} more)", "", "")
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

	console.print(f"[bold]Generating EPUBs[/bold] for [cyan]{cfg.library}[/cyan] [{'WRITE' if do_apply else 'DRY-RUN'}]", highlight=False)

	cache: Cache | None = None
	if not no_cache:
		cache = Cache(cfg.cache_db)
	from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn

	# Reading metadata for the whole library is the slow, silent gap before
	# EPUB generation — wrap it in a bar so it isn't a dead spot.
	with Progress(
		SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
		BarColumn(complete_style="bright_cyan", finished_style="bright_cyan", pulse_style="bright_cyan"), TextColumn("{task.completed}/{task.total}"),
		TimeRemainingColumn(), console=console, transient=True,
	) as progress:
		task_id = progress.add_task("Reading library", total=None)

		def _scan_cb(done: int, total: int) -> None:
			if progress.tasks[0].total is None and total:
				progress.update(task_id, total=total)
			progress.update(task_id, completed=done)

		books = scan_library(cfg.library, cache=cache, use_cache=not no_cache, progress_callback=_scan_cb)
	if cache is not None:
		cache.close()
	if limit is not None:
		books = books[:limit]

	results = []
	skipped_not_ok = 0
	skipped_has_epub = 0

	with Progress(
		SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
		BarColumn(complete_style="bright_cyan", finished_style="bright_cyan", pulse_style="bright_cyan"), TextColumn("{task.completed}/{task.total}"),
		TimeRemainingColumn(), console=console, transient=True,
	) as progress:
		task_id = progress.add_task("Generating EPUBs", total=len(books))
		for i, meta in enumerate(books, start=1):
			# Only OK books (verified clean)
			diag = detect_fn(meta)
			if diag.verdict.value not in ("OK", "VERIFIED"):
				skipped_not_ok += 1
				progress.update(task_id, completed=i)
				continue
			if diag.verdict.value == "OK" and not skip_verify and meta.primary_file:
				try:
					ver = verify(meta)
					if ver.result == "MISMATCH":
						skipped_not_ok += 1
						progress.update(task_id, completed=i)
						continue
				except Exception:  # noqa: BLE001
					pass
			# Skip if already has epub
			if ".epub" in meta.formats:
				skipped_has_epub += 1
				progress.update(task_id, completed=i)
				continue
			result = generate_epub(meta, dry_run=not do_apply)
			results.append(result)
			progress.update(task_id, completed=i)

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


@main.command()
@click.option("--library", "library", type=click.Path(exists=True, file_okay=False, path_type=Path), help="Library root")
@click.option("--no-cache", is_flag=True, help="Disable SQLite cache")
@click.option("--limit", type=int, default=None, help="Process only the first N books")
@click.option("--needfix-dir", default=None, help="Folder for quarantined rogues (default: 'needfix')")
@click.option("--apply", "do_apply", is_flag=True, help="Actually move rogues (default: dry-run)")
@click.option("--threshold", "strong", type=float, default=0.8, help="Fuzzy title ratio at/above which a format AGREES (default 0.8)")
@click.option("--weak-threshold", "weak", type=float, default=0.5, help="Fuzzy title ratio below which a format DISAGREES (default 0.5). Between weak and strong = UNCERTAIN (never moved).")
def crosscheck(library: Path | None, no_cache: bool, limit: int | None, needfix_dir: str | None, do_apply: bool, strong: float, weak: float) -> None:
	"""Check all formats in each book folder are the same book; quarantine rogues.

	For every folder with >=2 ebook formats, each format's content is compared
	against the folder's metadata (title/ISBN). Files whose content is a
	different book are moved into their own isolated folder under
	<needfix>/crosscheck/<Author> - <Title> (<id>) - <filename>/. Dry-run by
	default; pass --apply to move. Metadata is the anchor; only text-mined
	signals (ISBN/title from the actual page text) decide, never embedded
	metadata (which Calibre may have overwritten).
	"""
	from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn

	from .crosscheck import CROSSCHECK_SUBDIR, crosscheck_book
	from .crosscheck import quarantine as quarantine_fn
	from .mover import DEFAULT_NEEDFIX_DIR

	cfg = Config.from_env()
	if library is not None:
		cfg.library = library
	needfix = needfix_dir or DEFAULT_NEEDFIX_DIR

	console.print(f"[bold]Cross-checking formats[/bold] on [cyan]{cfg.library}[/cyan] [{'WRITE' if do_apply else 'DRY-RUN'}]", highlight=False)
	console.print(f"  rogues go to:  [cyan]{needfix}/{CROSSCHECK_SUBDIR}/<origin> - <file>/[/cyan]")

	cache: Cache | None = None
	if not no_cache:
		cache = Cache(cfg.cache_db)
	try:
		# Reading metadata for the whole library is the slow, silent gap before
		# cross-check — wrap it in a bar so it isn't a dead spot.
		with Progress(
			SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
			BarColumn(complete_style="bright_magenta", finished_style="bright_magenta", pulse_style="bright_magenta"), TextColumn("{task.completed}/{task.total}"),
			TimeRemainingColumn(), console=console, transient=True,
		) as progress:
			task_id = progress.add_task("Reading library", total=None)

			def _scan_cb(done: int, total: int) -> None:
				if progress.tasks[0].total is None and total:
					progress.update(task_id, total=total)
				progress.update(task_id, completed=done)

			books = scan_library(cfg.library, cache=cache, use_cache=not no_cache, progress_callback=_scan_cb)
		if limit is not None:
			books = books[:limit]

		results: list = []
		with Progress(
			SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
			BarColumn(complete_style="bright_magenta", finished_style="bright_magenta", pulse_style="bright_magenta"), TextColumn("{task.completed}/{task.total}"),
			TimeRemainingColumn(), console=console, transient=True,
		) as progress:
			task_id = progress.add_task("Cross-checking formats", total=len(books))
			for meta in books:
				try:
					results.append(crosscheck_book(meta, strong=strong, weak=weak))
				except Exception as e:  # noqa: BLE001
					log.warning("crosscheck failed for %s: %s", meta.path, e)
				progress.update(task_id, advance=1)

		# Quarantine rogues (second progress bar, sized by the rogue-file count).
		to_quarantine = [r for r in results if r.decision == "quarantine"]
		move_results: list = []
		if to_quarantine:
			rogue_count = sum(len(r.rogues) for r in to_quarantine)
			with Progress(
				SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
				BarColumn(complete_style="bright_magenta", finished_style="bright_magenta", pulse_style="bright_magenta"), TextColumn("{task.completed}/{task.total}"),
				TimeRemainingColumn(), console=console, transient=True,
			) as progress:
				qtask = progress.add_task("Quarantining rogues", total=rogue_count)

				def _cb(done: int, total: int) -> None:
					progress.update(qtask, completed=done)

				move_results = quarantine_fn(
					to_quarantine, cfg.library,
					needfix_dir=needfix, dry_run=not do_apply, cache=cache,
					progress_callback=_cb,
				)
	finally:
		if cache is not None:
			cache.close()

	_print_crosscheck_summary(results, move_results, do_apply)


def _print_crosscheck_summary(results, move_results, do_apply: bool) -> None:  # noqa: ANN001
	from collections import Counter

	console.print()
	t = Table(title="Crosscheck summary", show_header=True, header_style="bold cyan")
	t.add_column("Decision", style="bold")
	t.add_column("Count", justify="right")
	decisions: Counter[str] = Counter(r.decision for r in results)
	checked = len(results) - decisions.get("skipped", 0)
	t.add_row("checked (≥2 formats)", str(checked), style="dim")
	t.add_row("clean", str(decisions.get("clean", 0)))
	t.add_row("quarantine", str(decisions.get("quarantine", 0)), style="yellow")
	t.add_row("ambiguous", str(decisions.get("ambiguous", 0)), style="magenta")
	t.add_row("skipped (<2 formats)", str(decisions.get("skipped", 0)), style="dim")
	console.print(t)

	# Rogues sample (book × rogue-file), capped. Only 'quarantine' decisions
	# carry rogues (the files that will move); 'ambiguous' books are reported
	# separately below.
	rogues = [(r, rv) for r in results for rv in r.rogues]
	if rogues:
		console.print()
		t = Table(title=f"Rogues ({len(rogues)} file{'s' if len(rogues) != 1 else ''})", show_header=True, header_style="bold cyan")
		t.add_column("ID", justify="right", style="cyan")
		t.add_column("Book folder")
		t.add_column("File")
		t.add_column("Reason", style="dim")
		for r, rv in rogues[:25]:
			t.add_row(
				str(r.book_id or "?"),
				Path(r.path).name[:45],
				Path(rv.file).name[:35],
				rv.reason[:55],
			)
		if len(rogues) > 25:
			t.add_row("…", f"({len(rogues) - 25} more)", "", "")
		console.print(t)

	# Ambiguous books: formats disagree with metadata but nothing corroborates
	# the metadata, so nothing was moved. Surface them so a human can inspect.
	ambiguous = [r for r in results if r.decision == "ambiguous"]
	if ambiguous:
		console.print()
		console.print(f"[magenta]{len(ambiguous)} ambiguous book(s)[/magenta] — formats disagree with metadata but no format corroborates it; not moved (review manually).")
		t = Table(title="Ambiguous (not moved)", show_header=True, header_style="bold cyan")
		t.add_column("ID", justify="right", style="cyan")
		t.add_column("Book folder")
		t.add_column("Disagreeing files", style="dim")
		for r in ambiguous[:25]:
			bad = ", ".join(Path(rv.file).name for rv in r.verdicts if rv.verdict == "DISAGREES")
			t.add_row(str(r.book_id or "?"), Path(r.path).name[:45], bad[:60])
		console.print(t)

	if move_results:
		rc: Counter[str] = Counter(mr.action for mr in move_results)
		console.print()
		t = Table(title=f"Move results ({'WRITE' if do_apply else 'DRY-RUN'})", show_header=True, header_style="bold cyan")
		t.add_column("Action")
		t.add_column("Count", justify="right")
		for action, n in rc.most_common():
			t.add_row(action, str(n))
		console.print(t)
		if not do_apply:
			console.print("[dim]Dry-run: nothing moved. Re-run with --apply to quarantine the rogues.[/dim]")


# Required imports for the new commands
# (Enricher is imported lazily inside analyze() to avoid loading requests
#  when the user only runs scan/report.)


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
	"""Print detector results: category counts + sample books per category.

	A book with several diagnoses appears once per matching category (counts
	do not sum to the book total) — this surfaces every problem rather than only
	the highest-priority one.
	"""
	from collections import defaultdict

	from .detectors import all_diagnoses

	# Aggregate by category — a book lands in every category it matches.
	by_cat: dict[str, list] = defaultdict(list)
	for meta, diag in results:
		for d in all_diagnoses(diag):
			by_cat[d.category].append((meta, d))

	# Sort: corruption categories first, then OK, then MISSING_*
	cat_order = ["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10", "C12", "C11", "MISSING_COVER", "OK", "MISSING_ISBN", "MISSING_YEAR"]
	all_cats = sorted(by_cat.keys(), key=lambda c: cat_order.index(c) if c in cat_order else 999)

	total = len(results)
	console.print()
	console.print(f"[bold green]Detected {total} books[/bold green]  [dim](a book may appear in multiple categories)[/dim]")

	# Summary table
	t = Table(title="Detection summary", show_header=True, header_style="bold cyan")
	t.add_column("Category", style="bold")
	t.add_column("Count", justify="right")
	t.add_column("Verdict", style="magenta")
	for cat in all_cats:
		n = len(by_cat[cat])
		# Pick a representative verdict (the first one in the bucket)
		verdict = by_cat[cat][0][1].verdict.value
		t.add_row(cat, str(n), verdict)
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


# Shell-completion script generation.
#
# Click 8+ ships completion backends for bash, zsh, and fish that respond to a
# magic env var (_BMF_COMPLETE=<shell>_complete) at runtime. The classes below
# render the *installer* script each shell needs sourced once so that the
# runtime completion mechanism is wired into the user's shell.
#
# Usage:
#   bmf install-completion bash        # prints the script (eval it, or >> ~/.bashrc)
#   eval "$(bmf install-completion zsh)"
_PROG = "bmf"
_COMPLETE_VAR = f"_{_PROG.upper()}_COMPLETE"


@main.command("install-completion")
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None, help="Write the script to a file instead of stdout.")
def install_completion(shell: str, output: Path | None) -> None:
	"""Print a shell-completion script for tab-completion of bmf commands and options.

	\b
	Install it (pick the line for your shell):

	  bash:
	    eval "$(bmf install-completion bash)"

	  zsh:
	    eval "$(bmf install-completion zsh)"

	  fish:
	    bmf install-completion fish > ~/.config/fish/completions/bmf.fish

	With -o the script is written to a file instead of stdout, e.g.:

	  bmf install-completion bash -o ~/.local/share/bash-completion/completions/bmf.sh

	After sourcing, type 'bmf <Tab>' to complete subcommands and flags.
	"""
	from click.shell_completion import BashComplete, FishComplete, ZshComplete

	classes = {"bash": BashComplete, "zsh": ZshComplete, "fish": FishComplete}
	cls = classes[shell]
	script = cls(
		cli=main,
		ctx_args={},
		prog_name=_PROG,
		complete_var=_COMPLETE_VAR,
	).source()

	if output is not None:
		output.write_text(script, encoding="utf-8")
		console.print(f"[green]Completion script written to[/green] {output}")
		# Shell-specific activation hint.
		if shell == "bash":
			console.print(f"[dim]Run: source {output}[/dim]")
		elif shell == "zsh":
			console.print(f"[dim]Run: source {output} (or add to your ~/.zshrc)[/dim]")
		elif shell == "fish":
			console.print("[dim]Fish loads it automatically from ~/.config/fish/completions/[/dim]")
	else:
		# Print raw script to stdout (not via rich) so 'eval "$(bmf ...)"' works.
		click.echo(script)


if __name__ == "__main__":
	main()
