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
	bmf strip-covers — remove generated covers (cover.jpg sidecar renamed to
	                 .bak + embedded EPUB covers stripped); dry-run by default
	bmf abs-rescan  — tell Audiobookshelf to re-read the metadata of recently
	                 changed books (per-item API rescan); dry-run by default
"""
from __future__ import annotations

import logging
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import Config
from .i18n import SUPPORTED_LANGUAGES, _, init_language
from .library import Cache, CacheError, scan_library

if TYPE_CHECKING:
	from .models import BookMeta

console = Console()
log = logging.getLogger(__name__)


def _validate_library(library: Path) -> None:
	"""Verify that library exists and is an accessible directory."""
	try:
		if not library.is_dir():
			raise FileNotFoundError
	except OSError as e:
		msg = (
			f"[bold red]{_('Error:')}[/bold red] "
			f"{_('Library directory does not exist or is not accessible:')} [cyan]{library}[/cyan]\n"
			f"[dim]{_('If this is a network share (NFS/SMB), please ensure it is mounted.')}[/dim]"
		)
		if str(e):
			msg += f"\n[dim]{e}[/dim]"
		console.print(msg)
		sys.exit(1)


def _open_cache(db_path: Path, no_cache: bool = False) -> Cache | None:
	"""Safely instantiate Cache, handling unmounted/inaccessible paths gracefully."""
	if no_cache:
		# Without the SQLite cache there is no persistent cover-verdict store
		# either — detach any previously attached one (tests reuse the module).
		from .covers import set_cover_cache

		set_cover_cache(None)
		return None
	try:
		cache = Cache(db_path)
	except CacheError as e:
		console.print(
			f"[bold red]{_('Error:')}[/bold red] "
			f"{_('Cannot open cache database:')} [cyan]{db_path}[/cyan]\n"
			f"[dim]{_('Ensure the path exists, the network share is mounted, and you have write permissions.')}[/dim]\n"
			f"[dim]{e}[/dim]"
		)
		sys.exit(1)
	# Share the cache with the cover analyzer: unchanged cover.jpg files then
	# skip the (expensive) Pillow decode in every command that detects.
	from .covers import set_cover_cache

	set_cover_cache(cache)
	return cache

# Initialize the translation catalog from the environment BEFORE the click
# decorators below run: their `help=` texts are evaluated at import time, so
# BMF_LANGUAGE / the user's locale must already be known here. A later
# `--lang` flag (see main) re-initializes for runtime messages; help texts
# keep the import-time language (documented limitation).
init_language()


def _set_language(ctx: click.Context, param: click.Parameter, value: str | None) -> None:
	"""--lang callback: re-init the catalog early (before the subcommand runs)."""
	if value:
		init_language(value)


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


def _scan_library_with_progress(
	cfg: Config,
	cache: Cache | None,
	*,
	no_cache: bool = False,
	description: str | None = None,
	bar_style: str = "bright_yellow",
) -> list[BookMeta]:
	"""Scan library folders and return parsed BookMeta records with a unified yellow progress bar."""
	from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn

	desc = description or _("Reading library")
	with Progress(
		SpinnerColumn(),
		TextColumn("[progress.description]{task.description}"),
		BarColumn(complete_style=bar_style, finished_style=bar_style, pulse_style=bar_style),
		TextColumn("{task.completed}/{task.total}"),
		TimeRemainingColumn(),
		console=console,
		transient=True,
	) as progress:
		task_id = progress.add_task(desc, total=None)

		def _scan_cb(done: int, total: int) -> None:
			if progress.tasks[0].total is None and total:
				progress.update(task_id, total=total)
			progress.update(task_id, completed=done)

		return scan_library(
			cfg.library,
			cache=cache,
			use_cache=not no_cache,
			progress_callback=_scan_cb,
			workers=cfg.scan_workers,
		)


@click.group()
@click.version_option(__version__, prog_name="bmf")
@click.option("-v", "--verbose", is_flag=True, help=_("Enable debug logging"))
@click.option(
	"--lang", "-l", "language", type=click.Choice(SUPPORTED_LANGUAGES), default=None,
	is_eager=True, expose_value=False, callback=_set_language,
	help=_("Interface language (default: auto-detect from your locale; also BMF_LANGUAGE)"),
)
def main(verbose: bool) -> None:
	"""book-meta-fix: detect and fix metadata of ebooks."""
	_setup_logging(verbose)


@main.command()
@click.option("--library", "library", type=click.Path(file_okay=False, path_type=Path), help=_("Library root (default: $BMF_LIBRARY or ~/Books)"))
@click.option("--no-cache", is_flag=True, help=_("Disable SQLite cache (force full re-parse)"))
@click.option("--limit", type=int, default=None, help=_("Process only the first N books (for testing)"))
@click.option("--scan-workers", "scan_workers", type=int, default=None, help=_("Parallel threads for the library scan (tree walk + metadata reads; NFS latency-bound). Default 8, or BMF_SCAN_WORKERS. 1 = serial scan."))
def scan(library: Path | None, no_cache: bool, limit: int | None, scan_workers: int | None) -> None:
	"""Scan the library and print summary statistics."""
	cfg = Config.from_env()
	if library is not None:
		cfg.library = library
	if scan_workers is not None:
		cfg.scan_workers = max(1, scan_workers)

	_validate_library(cfg.library)
	cache = _open_cache(cfg.cache_db, no_cache=no_cache)
	try:
		books = _scan_library_with_progress(cfg, cache, no_cache=no_cache)

		if limit is not None:
			books = books[:limit]

		if not books:
			console.print("[red]" + _("No books found.") + "[/red]")
			sys.exit(1)

		_print_scan_summary(books)
	finally:
		if cache is not None:
			cache.close()


@main.command()
@click.option("--library", "library", type=click.Path(file_okay=False, path_type=Path), help=_("Library root"))
@click.option("--no-cache", is_flag=True, help=_("Disable SQLite cache (force full re-parse)"))
@click.option("--limit", type=int, default=None, help=_("Process only the first N books (for testing)"))
@click.option("--category", default=None, help=_("Show only books in this category (C1..C10, MISSING_ISBN, ...)"))
@click.option("--samples", type=int, default=3, help=_("Number of sample books to show per category"))
@click.option("--accept-missing/--no-accept-missing", "accept_missing", default=True, help=_("Apply the identity gate: a MISSING_ISBN/YEAR/COVER book whose author+title are confirmed against its content counts as OK (not broken). Default on, consistent with organize/analyze. --no-accept-missing keeps the pure detector verdict (no content reads; the historic fast report)."))
@click.option("--verify-ok", "verify_ok", is_flag=True, help=_("Audit: also verify books the detectors marked OK against their content. Reads every OK book's file (slower). A MISMATCH (or UNCERTAIN, see --no-strict-verify) is then counted as broken."))
@click.option("--no-strict-verify", "no_strict_verify", is_flag=True, help=_("With --verify-ok: only treat a clear MISMATCH as broken. By default UNCERTAIN is too."))
def report(library: Path | None, no_cache: bool, limit: int | None, category: str | None, samples: int, accept_missing: bool, verify_ok: bool, no_strict_verify: bool) -> None:
	"""Run detector rules and print category counts + samples.

	Classification is unified with organize/epubgen: an identified MISSING_*
	book (author+title confirmed against the content) is counted as OK, so the
	reported broken tally matches what `bmf organize` would route to needfix/.
	"""
	from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn

	from .classify import classify as classify_fn

	cfg = Config.from_env()
	if library is not None:
		cfg.library = library

	_validate_library(cfg.library)
	cache = _open_cache(cfg.cache_db, no_cache=no_cache)

	books = _scan_library_with_progress(cfg, cache, no_cache=no_cache)
	if limit is not None:
		books = books[:limit]
	if not books:
		console.print("[red]" + _("No books found.") + "[/red]")
		sys.exit(1)

	# Classify each book via the shared classifier (same rules as organize/analyze).
	results = []
	identified = 0
	with Progress(
		SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
		BarColumn(complete_style="magenta", finished_style="magenta", pulse_style="magenta"), TextColumn("{task.completed}/{task.total}"),
		TimeRemainingColumn(), console=console, transient=True,
	) as progress:
		task_id = progress.add_task(_("Classifying"), total=len(books))
		for i, b in enumerate(books, start=1):
			c = classify_fn(b, accept_missing=accept_missing, verify_ok=verify_ok, strict_verify=not no_strict_verify)
			if c.identified:
				identified += 1
			results.append((b, c.diag))
			progress.update(task_id, completed=i)
	if cache is not None:
		cache.close()

	_print_detect_summary(results, category, samples)
	if accept_missing and identified:
		identified_note = _(
			"{identified} book(s) identified (accepted): MISSING_* with author+title "
			"confirmed against content — these route to OK, not needfix."
		).format(identified=identified)
		console.print(f"[dim]  {identified_note}[/dim]")


@main.command()
@click.option("--library", "library", type=click.Path(file_okay=False, path_type=Path), help=_("Library root"))
@click.option("--no-cache", is_flag=True, help=_("Disable SQLite cache (force full re-parse)"))
@click.option("--limit", type=int, default=None, help=_("Process only the first N books (for testing)"))
@click.option("--skip-enrich", is_flag=True, default=False, help=_("Skip online enrichment (offline mode)"))
@click.option("--databazeknih/--no-databazeknih", "use_databazeknih", default=True, help=_("Enable/disable databazeknih.cz lookup (default: enabled)"))
@click.option("--legie/--no-legie", "use_legie", default=True, help=_("Enable/disable legie.info lookup (default: enabled)"))
@click.option("--abs-czech", "abs_czech_url", default=None, help=_("Use a self-hosted audiobookshelf_czech_metadata instance at this base URL (aggregates ~17 CZ audiobook storefronts — audio-edition metadata). Also settable via BMF_ABS_CZECH_URL."))
@click.option("--skip-verify", is_flag=True, help=_("Skip content verification"))
@click.option("--verify-ok", "verify_ok", is_flag=True, help=_("Audit: also verify books the detectors marked OK against their content. Reads every OK book's file (slower). A MISMATCH reclassifies it to NEEDS_REVIEW and seeks a fix (enrichment + LLM). Use periodically to catch corruption the structural detectors miss."))
@click.option("--no-strict-verify", "no_strict_verify", is_flag=True, help=_("With --verify-ok: only reclassify a clear MISMATCH (fuzzy title < 0.5). By default (without this flag) UNCERTAIN (0.5–0.8) is also reclassified."))
@click.option("--accept-missing/--no-accept-missing", "accept_missing", default=True, help=_("Auto-accept MISSING_ISBN/YEAR/COVER books whose author+title were confirmed against the book's content (pre-filled action: accept in review.yaml; pruned by `bmf apply`, safe no-op). Default on. Use --no-accept-missing to keep them for manual review."))
@click.option("--pattern", "pattern", default=None, help=_("Target path pattern for OK books (default: '{author}/{title} ({id})'). Drives the C13 location detector: a book not sitting at its pattern path enters review with a move proposal that `bmf apply` executes."))
@click.option("--no-check-location", "no_check_location", is_flag=True, help=_("Skip the C13 location check (analyze metadata only, no placement proposals)."))
@click.option("--recheck-ok", "recheck_ok", is_flag=True, help=_("Clear the `verified` flag (see review.yaml / the GUI checkbox) from every book, returning user-confirmed books to normal detection. Undo of a too-hasty OK."))
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None, help=_("Output review file (default: review.yaml)"))
@click.option("--llm/--no-llm", "use_llm", default=True, help=_("Enable/disable LLM reconciliation (default: enabled if provider configured)"))
@click.option("--llm-provider", "llm_provider", default=None, type=click.Choice(["antigravity", "acp", "agy", "zai", "mock", "off"], case_sensitive=False), help=_("Force the LLM provider branch (default: auto — Antigravity ACP when an agent is configured or cached, else Z.AI when ZAI_API_KEY is set). 'antigravity' = the ACP agent is the fast tier and Z.AI (if a key exists) only the paid fallback; 'zai' never uses ACP."))
@click.option("--antigravity-cmd", "antigravity_cmd", default=None, help=_("Command that launches an Agent Client Protocol agent process (ACP v1 over stdio) — a Google Antigravity subscription as the FAST LLM tier. The official agent is `agy_acp_server.par` from the ACP Registry (release zips need chmod +x); any ACP agent works, e.g. `gemini --acp`. 'auto' (or empty) = bmf's SELF-MANAGED agent: the run checks the registry, downloads (~700 MB / 1.9 GB unpacked, into ~/.cache/book-meta-fix/acp) and upgrades it itself; an explicit path manages it manually. Same as BMF_ANTIGRAVITY_CMD."))
@click.option("--antigravity-model", "antigravity_model", default=None, help=_("Model the ACP agent should serve the fast tier with (matched against the agent's session config options by exact value/name or token family, so 'gemini-flash' picks 'gemini-3.8-flash-high'; empty = the agent's default pick). Default gemini-flash-low — the newest flash at low effort (the quick check wants latency, not deliberation). Same as BMF_ANTIGRAVITY_MODEL."))
@click.option("--antigravity-fallback", "antigravity_fallback", default=None, type=click.Choice(["agy", "acp", "antigravity", "glm", "zai"], case_sensitive=False), help=_("Who serves the loop's QUALITY stage when the ACP agent is the fast tier: 'agy' (default — a second ACP pool on the fallback model, the whole loop stays on the subscription) or 'glm' (the Z.AI flash+paid loop; needs ZAI_API_KEY). Same as BMF_ANTIGRAVITY_FALLBACK."))
@click.option("--antigravity-fallback-model", "antigravity_fallback_model", default=None, help=_("Model for the agy quality stage, family-matched like --antigravity-model (empty = the agent's default pick). Default gemini-flash-high. Ignored with --antigravity-fallback glm. Same as BMF_ANTIGRAVITY_FALLBACK_MODEL."))
@click.option("--llm-categories", default="ALL", help=_("Comma-separated categories to send to LLM, or 'ALL' (default). ALL = every category except C9 (legitimate anonyms like the Bible, where an LLM-invented author would be wrong). Each book is one LLM request that returns all fields at once, so the cost is per-book, not per-category."))
@click.option("--workers", "-w", type=int, default=10, help=_("Parallel workers for I/O (extract/LLM/enrich). Default 10."))
@click.option("--scan-workers", "scan_workers", type=int, default=None, help=_("Parallel threads for the library scan (tree walk + metadata reads; NFS latency-bound). Default 8, or BMF_SCAN_WORKERS. 1 = serial scan."))
@click.option("--llm-min-interval", "llm_min_interval", type=float, default=None, help=_("Minimum seconds between LLM requests (RPM throttle, default 2.0 = ~30 RPM). Decoupled from --workers: cheap I/O still runs at full worker count. Lower (e.g. 1.0 = 60 RPM) on a higher Z.AI tier; raise (e.g. 4.0 = 15 RPM) if you still hit 429."))
@click.option("--llm-model", "llm_model", default=None, help=_("Primary model for the LLM loop (default glm-4.7-flash, the free first attempt; with --no-llm-loop the fallback-quality model instead). Alternatives: glm-4.6, glm-4.5-air, glm-4.5-flash. See README 'LLM model choice' for the token/quality tradeoffs measured by scripts/llm_experiment.py."))
@click.option("--llm-reasoning-effort", "llm_reasoning_effort", default=None, help=_("reasoning_effort for GLM-5.x models: low (default) | medium | max. Lower cuts reasoning tokens ~60%% vs max. Ignored by GLM-4.x (use --llm-thinking)."))
@click.option("--llm-thinking", "llm_thinking", default=None, help=_("thinking toggle for GLM-4.x models: disabled (default) | enabled. 'disabled' turns off chain-of-thought (3-4x fewer output tokens). Ignored by GLM-5.x (use --llm-reasoning-effort)."))
@click.option("--no-llm-loop", "no_llm_loop", is_flag=True, help=_("Disable the self-correction loop. Default: loop on — try the free loop model first (with verify feedback), then the paid fallback model. With this flag, a single LLM call is used instead (the --llm-model, default the fallback-quality one)."))
@click.option("--llm-fallback-model", "llm_fallback_model", default=None, help=_("Paid high-quality fallback model (default glm-5.3). Used when the loop model fails verify or is rate-limited; also the default single-call model when the loop is off."))
@click.option("--llm-burst", "llm_burst", type=float, default=None, help=_("Leaky-bucket burst capacity: how many LLM calls may start inside one interval (default 1 = pure even drip, no bunching — one call every --llm-min-interval seconds). This is a count-per-time limiter, not a concurrency cap. Raise only with confirmed rate headroom; a burst >1 fires multiple calls in the same second and trips Z.AI's dynamic RPM limit (429)."))
@click.option("--llm-rate-limit-base", "llm_rate_limit_base", type=float, default=None, help=_("Base seconds of the global cooldown applied when a 429 is seen (default 5). When ANY worker hits a 429, ALL workers pause this long; the cooldown escalates 5/10/20/... with consecutive 429s, honours the server Retry-After when longer, and is capped by --llm-rate-limit-max. Higher = safer but slower; lower = more 429 risk."))
@click.option("--llm-rate-limit-max", "llm_rate_limit_max", type=float, default=None, help=_("Cap (seconds) on the escalating 429 cooldown (default 60). Prevents a sustained outage from parking workers indefinitely."))
@click.option("--llm-max-inflight", "llm_max_inflight", type=int, default=None, help=_("Hard cap on LLM requests running at the same instant (default 3). The Z.AI coding plan admits only ~5 concurrent requests per account (interactive clients draw from the same ceiling), so a deep fallback herd gets 429/1302 storms — and false 1113 'insufficient balance' — no matter how slow the drip is. Workers queue on this instead of being rejected. Flash-family models get a stricter sub-cap of min(2, this value)."))
def analyze(library: Path | None, no_cache: bool, limit: int | None, skip_enrich: bool, use_databazeknih: bool, use_legie: bool, abs_czech_url: str | None, skip_verify: bool, verify_ok: bool, no_strict_verify: bool, accept_missing: bool, pattern: str | None, no_check_location: bool, recheck_ok: bool, output: Path | None, use_llm: bool, llm_provider: str | None, antigravity_cmd: str | None, antigravity_model: str | None, antigravity_fallback: str | None, antigravity_fallback_model: str | None, llm_categories: str, workers: int, scan_workers: int | None, llm_min_interval: float | None, llm_model: str | None, llm_reasoning_effort: str | None, llm_thinking: str | None, no_llm_loop: bool, llm_fallback_model: str | None, llm_burst: float | None, llm_rate_limit_base: float | None, llm_rate_limit_max: float | None, llm_max_inflight: int | None) -> None:
	"""Run full pipeline and generate a review.yaml for NEEDS_REVIEW books."""
	from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TextColumn, TimeRemainingColumn

	from .enrichers import Enricher
	from .llm import get_provider
	from .pipeline import run_pipeline

	cfg = Config.from_env()
	if library is not None:
		cfg.library = library
	if scan_workers is not None:
		cfg.scan_workers = max(1, scan_workers)
	_validate_library(cfg.library)
	cache = _open_cache(cfg.cache_db, no_cache=no_cache)
	out = output or cfg.review_file

	if not use_databazeknih:
		cfg.databazeknih_enabled = False
	if not use_legie:
		cfg.legie_enabled = False
	if abs_czech_url:
		cfg.abs_czech_url = abs_czech_url

	console.print(f"[bold]{_('Running pipeline')}[/bold] {cfg.library} ({_('workers')}: {workers})", highlight=False)
	if not skip_enrich:
		if cfg.databazeknih_enabled:
			console.print("  [cyan]databazeknih.cz[/cyan] " + _("lookup enabled (genres + metadata)"))
			console.print("  [cyan]" + _("cover replacement") + "[/cyan] " + _("enabled (C11 generated / MISSING_COVER → databazeknih cover_url)"))
		if cfg.legie_enabled:
			console.print("  [cyan]legie.info[/cyan] " + _("lookup enabled (sci-fi/fantasy — short stories & series)"))
		if cfg.abs_czech_url:
			console.print(f"  [cyan]{cfg.abs_czech_url}[/cyan] " + _("CZ audiobook provider lookup enabled (storefront aggregator)"))
	else:
		console.print("  [dim]" + _("online enrichment disabled (--skip-enrich)") + "[/dim]")
	if verify_ok:
		strict = not no_strict_verify
		console.print(f"  [cyan]--verify-ok[/cyan] {_('--verify-ok audit: OK books checked against content (strict={strict})').format(strict=strict)}")

	# Two-phase progress under one transient bar: the library scan (minutes on
	# NFS) gets its own labelled task fed by scan_library's callbacks, and the
	# per-book processing task appears only when processing actually starts —
	# run_pipeline announces (0, total) before the first book, so the bar shows
	# its total and ETA immediately instead of pulsing at 0/None until the
	# first (LLM-bound) book completes.
	progress = Progress(
		SpinnerColumn(),
		TextColumn("[progress.description]{task.description}"),
		BarColumn(complete_style="bright_yellow", finished_style="bright_yellow", pulse_style="bright_yellow"),
		TextColumn("{task.completed}/{task.total}"),
		TimeRemainingColumn(),
		console=console,
		transient=True,
	)
	scan_task = progress.add_task(_("Reading library"), total=None)
	proc_task: TaskID | None = None

	def _scan_cb(done: int, total: int) -> None:
		# total is re-set on every call — the --recheck-ok pre-scan below and
		# run_pipeline's own scan both feed this task.
		progress.update(scan_task, total=total, completed=done)

	def _proc_cb(done: int, total: int) -> None:
		nonlocal proc_task
		if proc_task is None:
			# First processing callback = the scan phase is over; swap the
			# tasks so the bar always names what is actually running.
			progress.remove_task(scan_task)
			proc_task = progress.add_task(_("processing"), total=total)
		progress.update(proc_task, completed=done)

	# The ACP agent self-install (first agy run / outdated cache) streams its
	# ~700 MB download through this task inside the SAME transient bar.
	acp_task: TaskID | None = None

	def _acp_dl_cb(done: int, total: int) -> None:
		nonlocal acp_task
		if acp_task is None:
			acp_task = progress.add_task(_("Downloading the ACP agent"), total=total or None)
		progress.update(acp_task, total=total or None, completed=done)

	enricher = None
	llm_provider = None
	review_writer = None
	results: list = []
	interrupted = False
	# Populated by run_pipeline (passed in) so we can print a fix-source
	# breakdown after the run. The dict is seeded with all keys inside
	# run_pipeline, so it's safe to read here even on early failure.
	pipe_stats: dict = {}
	try:
		progress.start()

		# --recheck-ok: wipe the persistent `verified` flag off every book that
		# carries it, so those user-confirmed books re-enter normal detection.
		# Must run BEFORE run_pipeline (which skips verified books right after its
		# scan) and must invalidate the cache rows, or the scan would keep serving
		# the pre-clear BookMeta (esp. on NFS).
		if recheck_ok:
			from .writers import clear_verified

			books = scan_library(cfg.library, cache=cache, use_cache=not no_cache, progress_callback=_scan_cb, workers=cfg.scan_workers)
			cleared = 0
			for b in books:
				if b.verified:
					if clear_verified(Path(b.path)):
						cleared += 1
					if cache is not None:
						cache.invalidate(b.path)
			if cache is not None:
				cache.commit()
			console.print("[cyan]--recheck-ok[/cyan]: " + _("cleared the verified flag on {count} book(s)").format(count=cleared))

		if not skip_enrich:
			enricher = Enricher(
				cache_db=cfg.cache_db,
				databazeknih_enabled=cfg.databazeknih_enabled,
				legie_enabled=cfg.legie_enabled,
				abs_czech_url=cfg.abs_czech_url or None,
				abs_czech_token=cfg.abs_czech_token,
				openlibrary_enabled=cfg.openlibrary_enabled,
				google_books_enabled=cfg.google_books_enabled,
				negative_ttl_sec=cfg.enrich_negative_ttl_sec,
			)

		# LLM provider
		if use_llm:
			if llm_min_interval is not None:
				cfg.llm_min_interval = llm_min_interval
			if llm_max_inflight is not None:
				cfg.llm_max_inflight = max(1, llm_max_inflight)
			if llm_model is not None:
				cfg.llm_model = llm_model
			if llm_reasoning_effort is not None:
				cfg.zai_reasoning_effort = llm_reasoning_effort
			if llm_thinking is not None:
				cfg.zai_thinking = llm_thinking
			if no_llm_loop:
				cfg.llm_loop = False
			if llm_fallback_model is not None:
				cfg.llm_fallback_model = llm_fallback_model
			if llm_burst is not None:
				cfg.llm_burst = llm_burst
			if llm_rate_limit_base is not None:
				cfg.llm_rate_limit_base = llm_rate_limit_base
			if llm_rate_limit_max is not None:
				cfg.llm_rate_limit_max = llm_rate_limit_max
			if llm_provider:
				cfg.llm_provider = llm_provider.lower()
			if antigravity_cmd is not None:
				cfg.acp_command = antigravity_cmd
			if antigravity_model is not None:
				cfg.acp_model = antigravity_model
			if antigravity_fallback is not None:
				cfg.acp_fallback_provider = antigravity_fallback.lower()
			if antigravity_fallback_model is not None:
				cfg.acp_fallback_model = antigravity_fallback_model
			llm_provider = get_provider(cfg, progress_cb=_acp_dl_cb)
			# The download task (if any fired) is done — take it out of the bar
			# before the scan/processing phases take it over.
			if acp_task is not None:
				progress.remove_task(acp_task)
				acp_task = None
			if llm_provider is None:
				log.debug("No LLM provider available (set ZAI_API_KEY, BMF_ANTIGRAVITY_CMD, or BMF_LLM_MOCK=1)")
			elif llm_provider.name == "antigravity-acp":
				# ACP fast tier: the informative knobs are the agent command,
				# the model pick, and who serves the quality stage.
				cats = tuple(c.strip() for c in llm_categories.split(",") if c.strip())
				model_s = llm_provider.model or _("agent default")
				fb_model = llm_provider.fallback_model or _("agent default")
				# Hoisted OUT of the f-string below: babel on py3.10 cannot
				# extract _() calls from f-string holes (the abs-rescan gotcha).
				fast_tier = _("fast tier")
				no_fb = _("no fallback configured")
				fallback_s = {
					"agy": f" → fallback agy:{fb_model}",
					"glm": f" → fallback glm:{fb_model}",
				}.get(llm_provider.fallback_kind, f" ({no_fb})")
				console.print(
					f"  LLM: [cyan]{llm_provider.name}[/cyan] {fast_tier} model={model_s}{fallback_s} "
					f"({' '.join(llm_provider.command)}) "
					f"for categories {cats} (≤{cfg.acp_max_inflight} agents in flight)"
				)
			else:
				cats = tuple(c.strip() for c in llm_categories.split(",") if c.strip())
				rpm = round(60.0 / cfg.llm_min_interval) if cfg.llm_min_interval > 0 else float("inf")
				flash_via = f", flash via {llm_provider.flash_base_url}" if getattr(llm_provider, "flash_base_url", None) else ""
				if cfg.llm_loop:
					# Loop mode (default): free loop model first, paid fallback second.
					console.print(
						f"  LLM: [cyan]{llm_provider.name}[/cyan] "
						f"primary={llm_provider.model} → fallback={llm_provider.fallback_model} "
						f"(reasoning_effort={cfg.zai_reasoning_effort}) "
						f"for categories {cats} (≤{rpm} RPM, min {cfg.llm_min_interval}s between calls, max {cfg.llm_max_inflight} in flight, adaptive{flash_via})"
					)
				else:
					# Single-call mode (--no-llm-loop): one model, no fallback.
					is_glm5 = llm_provider.model.lower().startswith("glm-5")
					reason = f"reasoning_effort={cfg.zai_reasoning_effort}" if is_glm5 else f"thinking={cfg.zai_thinking}"
					console.print(f"  LLM: [cyan]{llm_provider.name}[/cyan] model={llm_provider.model} ({reason}) for categories {cats} (≤{rpm} RPM, min {cfg.llm_min_interval}s between calls, max {cfg.llm_max_inflight} in flight, adaptive{flash_via})")

		# Streaming review writer: appends each processed book to review.yaml as it
		# completes (Unix-pipe style). The original is moved to .bak on
		# construction so user decisions are preserved; finish() carries over any
		# unprocessed prior entries and deletes .bak on success.
		from .review_writer import ReviewWriter

		review_writer = ReviewWriter(out, library_root=cfg.library)

		results = run_pipeline(
			cfg.library, cache=cache, enricher=enricher,
			skip_enrich=skip_enrich, skip_verify=skip_verify,
			llm_provider=llm_provider,
			llm_categories=tuple(c.strip() for c in llm_categories.split(",") if c.strip()) if use_llm else (),
			limit=limit,
			workers=workers,
			scan_workers=cfg.scan_workers,
			progress_callback=_proc_cb,
			scan_progress_callback=_scan_cb,
			review_writer=review_writer,
			location_root=None if no_check_location else cfg.library,
			location_pattern=pattern or cfg.path_pattern,
			verify_ok=verify_ok,
			strict_verify=not no_strict_verify,
			llm_loop=cfg.llm_loop,
			accept_missing_if_identified=accept_missing,
			stats=pipe_stats,
		)
	except KeyboardInterrupt:
		# A second Ctrl-C, or one that escaped run_pipeline's internal handler
		# (or landed during the scan/setup phase before the writer existed).
		# The streaming writer has already flushed everything up to the point of
		# interruption; finish() below carries over prior unprocessed entries.
		interrupted = True
		console.print("\n[yellow]" + _("Interrupted (Ctrl-C). Finalizing review file with partial results…") + "[/yellow]")
	finally:
		progress.stop()
		if cache is not None:
			cache.close()
		if enricher is not None:
			enricher.close()
		# Shut down pooled ACP agent subprocesses (the Antigravity fast tier
		# keeps up to --acp workers alive between books; Z.AI/Mock providers
		# have nothing to close).
		if llm_provider is not None and hasattr(llm_provider, "close"):
			llm_provider.close()
		# Always finalize the writer — even on Ctrl-C/error — so the review file
		# is consistent and prior decisions are carried over. keep_backup when
		# interrupted, so the user can recover the pre-run state if needed.
		# A run interrupted before the writer was created (scan/setup phase) has
		# nothing to finalize — review.yaml was never moved to .bak.
		summary = {"written": 0, "skipped_user_decided": 0, "remaining_count": 0, "backup_path": None, "action_accept": 0, "action_null": 0, "action_other": 0, "verified_prefilled": 0}
		if review_writer is not None:
			try:
				summary = review_writer.finish(keep_backup=interrupted)
			except Exception as e:  # noqa: BLE001
				console.print(f"[red]{_('review writer finalize failed: {error}').format(error=e)}[/red]")

	# Print pipeline summary (action breakdown leads; results list still drives stats).
	_print_pipeline_summary(results, pipe_stats, review_summary=summary)
	if summary.get("backup_path"):
		console.print(f"[yellow]{_('Backup kept at {path} (run did not finish cleanly)').format(path=summary['backup_path'])}[/yellow]", highlight=False)
	console.print()
	console.print(f"[bold green]{_('Wrote {count} review entries to {file}').format(count=summary['written'], file=out)}[/bold green]", highlight=False)
	console.print(_('Edit the file, set `action` for each entry, then run:') + f" [bold]bmf apply {out}[/bold]", highlight=False)


def _print_pipeline_summary(results, stats: dict | None = None, review_summary: dict | None = None) -> None:  # noqa: ANN001
	"""Print a summary of what the pipeline produced.

	Leads with the **actionable** breakdown the workflow cares about —
	OK (done) vs auto-fix (pre-filled ``accept`` → ``bmf apply``) vs manual
	review (``action: null``) — rather than the detector's pre-fix verdicts.
	The verdict (OK / AUTO_FIXABLE / NEEDS_REVIEW) is decided BEFORE the
	pipeline tries to fix anything, so it does not predict what will actually
	be applied: a structurally-broken book can still be auto-accepted once a
	confident, content-verified fix is found, and a merely-incomplete book can
	stall at ``null`` when no source has the missing data. The ``action``
	field is the post-fix truth, so that is what we surface.

	*stats* (from run_pipeline) drives the fix-source breakdown table; when
	None (e.g. when run_pipeline wasn't used), only the action/verification
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
	ok = verdict_counter.get("OK", 0) + verdict_counter.get("VERIFIED", 0)
	# Books the detector flagged but the pipeline is confident about: written
	# to review with action: accept (apply will fix them). And the opposite —
	# written with action: null (a human must decide).
	acc = (review_summary or {}).get("action_accept", 0)
	nul = (review_summary or {}).get("action_null", 0)
	oth = (review_summary or {}).get("action_other", 0)
	skipped = (review_summary or {}).get("skipped_user_decided", 0)
	written = (review_summary or {}).get("written", 0)
	ver = (review_summary or {}).get("verified_prefilled", 0)

	console.print()
	t = Table(title=_("Pipeline summary"), show_header=True, header_style="bold cyan")
	t.add_column(_("Bucket"), style="bold")
	t.add_column(_("Count"), justify="right")
	t.add_row(_("OK (already correct — nothing to do)"), str(ok), style="green")
	t.add_row(_("Auto-fix (action: accept → `bmf apply`)"), str(acc), style="bold green")
	if ver:
		t.add_row(_("Auto-verified (identity confirmed vs content + online source)"), str(ver), style="green")
	t.add_row(_("Manual review (action: null)"), str(nul), style="yellow")
	if oth:
		t.add_row(_("Other action (delete/keep)"), str(oth))
	if skipped:
		t.add_row(_("Skipped (already decided earlier)"), str(skipped), style="dim")
	t.add_section()
	t.add_row(_("Written to review.yaml"), str(written))
	t.add_row(_("Total books"), str(total), style="bold")
	console.print(t)

	t = Table(title=_("Verification results"), show_header=True, header_style="bold cyan")
	t.add_column(_("Result"))
	t.add_column(_("Count"), justify="right")
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
		(_("Offline fixes"), offline_total, False),
		(_("  └ text-mined (content)"), stats.get("offline_content", 0), True),
		(_("  └ embedded OPF"), stats.get("offline_embedded", 0), True),
		(_("Online fixes"), online_total, False),
		(_("  └ databazeknih.cz"), stats.get("online_databazeknih", 0), True),
		(_("  └ legie.info"), stats.get("online_legie", 0), True),
		(_("  └ CZ audiobook provider"), stats.get("online_abs_czech", 0), True),
		(_("  └ openlibrary.org"), stats.get("online_openlibrary", 0), True),
		(_("  └ Google Books"), stats.get("online_google_books", 0), True),
		(_("LLM fixes"), llm_total, False),
		(_("  └ fast model (flash)"), stats.get("llm_flash_fixed", 0), True),
		(_("  └ fallback model"), stats.get("llm_final_fixed", 0), True),
		(_("  └ low confidence"), stats.get("llm_low_confidence", 0), True),
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
	t = Table(title=_("Fix sources (how NEEDS_REVIEW books were resolved)"), show_header=True, header_style="bold cyan")
	t.add_column(_("Source"), style="bold")
	t.add_column(_("Count"), justify="right")
	for label, n, _sub in ordered:
		t.add_row(label, str(n))
	t.add_section()
	t.add_row(_("Proposed (any source)"), str(proposed_total), style="bold")
	t.add_row(_("Unfixed (no proposal found)"), str(unfixed), style="yellow")
	t.add_row(_("Accepted (identity OK, field missing)"), str(stats.get("accepted_missing", 0)), style="green")
	console.print(t)

	# LLM cost detail: how many books the LLM was asked about vs. how many it
	# actually fixed. Only shown when an LLM provider was in play.
	if any(stats.get(k) for k in ("llm_flash_fixed", "llm_final_fixed", "llm_low_confidence", "llm_skipped_no_text", "llm_no_result", "llm_error")):
		llm_asked = llm_total + llm_no_result + llm_error
		console.print()
		t = Table(title=_("LLM usage"), show_header=True, header_style="bold cyan")
		t.add_column(_("Metric"), style="bold")
		t.add_column(_("Count"), justify="right")
		t.add_row(_("LLM calls made"), str(llm_asked))
		t.add_row(_("Skipped (no usable text)"), str(llm_skipped))
		t.add_row(_("No useful result"), str(llm_no_result))
		if llm_error:
			t.add_row(_("LLM errors"), str(llm_error), style="red")
		console.print(t)

	# Covers: only show when cover detection ran (counts > 0).
	covers_gen = stats.get("covers_generated", 0)
	covers_missing = stats.get("covers_missing", 0)
	if covers_gen or covers_missing:
		console.print()
		t = Table(title=_("Covers"), show_header=True, header_style="bold cyan")
		t.add_column(_("Category"), style="bold")
		t.add_column(_("Count"), justify="right")
		t.add_row(_("Generated (calibre placeholder)"), str(covers_gen), style="yellow")
		t.add_row(_("Missing cover"), str(covers_missing))
		console.print(t)

	if stats.get("errors"):
		console.print(f"\n[red]{_('Processing errors: {count}').format(count=stats['errors'])}[/red]")


@main.command()
@click.argument("review_file", required=False, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--library", "library", type=click.Path(file_okay=False, path_type=Path), help=_("Library root"))
@click.option("--apply", "do_apply", is_flag=True, help=_("Actually write changes (default: dry-run)"))
@click.option("--pattern", "pattern", default=None, help=_("Target path pattern for OK books (default: '{author}/{title} ({id})'). Applied books whose metadata is clean (or `verified`) move there; books with unresolved problems move to --needfix-dir."))
@click.option("--needfix-dir", "needfix_dir", default=None, help=_("Folder for books that still have unresolved problems after apply (default: 'needfix'). A resolved book moves back out of it on the next apply."))
@click.option("--no-place", "no_place", is_flag=True, help=_("Write metadata only; do not move folders (placement off)."))
def apply(review_file: Path | None, library: Path | None, do_apply: bool, pattern: str | None, needfix_dir: str | None, no_place: bool) -> None:
	"""Apply approved changes from a (human-edited) review.yaml.

	After writing an entry's metadata, apply also PLACES the book (the former
	`bmf organize`, folded in here): clean / verified books move to the target
	pattern path, unresolved ones to needfix/. No content reads — the decision
	is re-derived from the final metadata only.
	"""
	from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn

	from .pipeline import apply_review

	cfg = Config.from_env()
	if library is not None:
		cfg.library = library
	if review_file is None:
		# No positional review file: fall back to $BMF_REVIEW / .env, else the
		# CWD default — same resolution as `analyze` and `gui`.
		review_file = cfg.review_file

	if not no_place:
		_validate_library(cfg.library)

	console.print(f"[bold]{_('Applying')}[/bold] {review_file} ({'WRITE' if do_apply else 'DRY-RUN'})", highlight=False)
	# Open the books cache (if one exists) so we can invalidate the folders we
	# rewrite — otherwise the next run may serve the pre-apply BookMeta, most
	# painfully on NFS where the attribute cache masks the new mtime.
	cache: Cache | None = None
	if cfg.cache_db.is_file():
		try:
			cache = Cache(cfg.cache_db)
		except CacheError:
			cache = None
	progress = Progress(
		SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
		BarColumn(complete_style="green", finished_style="green", pulse_style="green"), TextColumn("{task.completed}/{task.total}"),
		TimeRemainingColumn(), console=console, transient=True,
	)
	task_id = progress.add_task(_("Applying"), total=None)
	progress.start()
	try:
		def _cb(done: int, total: int) -> None:
			if progress.tasks[0].total is None and total:
				progress.update(task_id, total=total)
			progress.update(task_id, completed=done)

		summary = apply_review(
			review_file, cfg.library, dry_run=not do_apply, cache=cache,
			progress_callback=_cb,
			pattern=pattern if pattern is not None else cfg.path_pattern,
			needfix_dir=needfix_dir if needfix_dir is not None else cfg.needfix_dir,
			place=not no_place,
		)
	finally:
		progress.stop()
		if cache is not None:
			cache.close()
	console.print()
	t = Table(title=_("Apply summary"), show_header=True, header_style="bold cyan")
	t.add_column(_("Metric"), style="bold")
	t.add_column(_("Count"), justify="right")
	t.add_row(_("Mode"), "WRITE" if do_apply else "DRY-RUN")
	t.add_row(_("Applied"), str(summary["applied"]))
	t.add_row(_("Kept"), str(summary.get("kept", 0)))
	t.add_row(_("Deleted"), str(summary.get("deleted", 0)))
	if summary.get("snapshot"):
		t.add_row(_("Deletion snapshot"), summary["snapshot"])
	if summary.get("moved_to_root") or summary.get("merged") or summary.get("already_placed"):
		t.add_row(_("Placed on target path"), str(summary.get("moved_to_root", 0)))
		if summary.get("merged"):
			t.add_row(_("Merged into same-work folder"), str(summary["merged"]))
		t.add_row(_("Already on target path"), str(summary.get("already_placed", 0)))
	if summary.get("moved_to_needfix"):
		t.add_row(_("Moved to needfix"), str(summary["moved_to_needfix"]))
	if summary.get("pruned"):
		t.add_row(_("Pruned from review"), str(summary["pruned"]))
		t.add_row(_("Remaining in review"), str(summary["remaining"]))
	t.add_row(_("Errors"), str(len(summary["errors"])))
	console.print(t)
	if summary["errors"]:
		console.print()
		console.print("[red]" + _("Errors:") + "[/red]")
		for e in summary["errors"][:20]:
			console.print(f"  {e}")


@main.command()
@click.option("--library", "library", type=click.Path(file_okay=False, path_type=Path), help=_("Library root"))
@click.option("--review", "review_file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None, help=_("review.yaml to edit (default: $BMF_REVIEW or review.yaml)"))
def gui(library: Path | None, review_file: Path | None) -> None:
	"""Launch the interactive Tkinter editor for review.yaml.

	Keyboard-driven: PgUp/PgDn move between books, Tab between fields, every
	action has a Ctrl+letter shortcut. Edits are written back to review.yaml;
	metadata is then committed by `bmf apply`. Requires the optional Tk
	bindings (python3-tk on Debian/Ubuntu).
	"""
	from .gui import run_gui

	cfg = Config.from_env()
	if library is not None:
		cfg.library = library
	if review_file is not None:
		cfg.review_file = review_file
	# GUI strings are built lazily (widgets created after this call), so the
	# config/env language applies fully here — unlike the CLI help texts.
	init_language(cfg.language or None)
	run_gui(cfg)


@main.command()
@click.pass_context
def organize(ctx: click.Context) -> None:
	"""(Deprecated) Moved into `bmf apply` — kept as a signpost stub.

	The old command re-classified the WHOLE library on every run (detectors +
	content reads for the identity gate), which made it slow and blind to your
	review decisions. Placement now lives in the review flow:

	  1. bmf analyze     — detects everything ONCE, including location
	                       mismatches (C13); misplaced-but-healthy books come
	                       with a pre-filled action: accept
	  2. bmf gui         — adjust proposals / mark books `verified`
	  3. bmf apply --apply — writes metadata AND moves each applied book:
	                       clean/verified → target path, unresolved → needfix/
	"""
	console.print("[yellow]" + _(
		"`bmf organize` was merged into `bmf apply`. Run `bmf analyze` "
		"(it flags misplaced books with a move proposal), review in "
		"`bmf gui` if needed, then `bmf apply --apply` — applied books are "
		"placed on their target path (unresolved ones into needfix/)."
	) + "[/yellow]")


@main.command()
@click.option("--library", "library", type=click.Path(file_okay=False, path_type=Path), help=_("Library root"))
@click.option("--no-cache", is_flag=True, help=_("Disable SQLite cache"))
@click.option("--limit", type=int, default=None, help=_("Process only the first N books"))
@click.option("--apply", "do_apply", is_flag=True, help=_("Actually generate EPUBs (default: dry-run)"))
@click.option("--accept-missing/--no-accept-missing", "accept_missing", default=True, help=_("Apply the identity gate (same as organize/report): an identified MISSING_* book (author+title confirmed against content) is treated as OK and gets an EPUB. Default on."))
@click.option("--verify-ok", "verify_ok", is_flag=True, help=_("Audit: verify OK books against their content before generating. Off by default (consistent with organize/report). A MISMATCH (or UNCERTAIN, see --no-strict-verify) skips generation."))
@click.option("--no-strict-verify", "no_strict_verify", is_flag=True, help=_("With --verify-ok: only a clear MISMATCH skips generation. By default UNCERTAIN does too."))
def epubgen(library: Path | None, no_cache: bool, limit: int | None, do_apply: bool, accept_missing: bool, verify_ok: bool, no_strict_verify: bool) -> None:
	"""Generate EPUBs for OK books that lack one, from the best source format.

	Classification is unified with organize/report: identified MISSING_* books
	route as OK and get an EPUB. OK books are not content-verified unless
	--verify-ok is given.
	"""
	from .classify import classify as classify_fn
	from .epubgen import generate_epub

	cfg = Config.from_env()
	if library is not None:
		cfg.library = library

	_validate_library(cfg.library)
	console.print(f"[bold]{_('Generating EPUBs')}[/bold] {cfg.library} [{'WRITE' if do_apply else 'DRY-RUN'}]", highlight=False)

	cache = _open_cache(cfg.cache_db, no_cache=no_cache)
	from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn

	books = _scan_library_with_progress(cfg, cache, no_cache=no_cache)
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
		task_id = progress.add_task(_("Generating EPUBs"), total=len(books))
		for i, meta in enumerate(books, start=1):
			# Only OK books (verified clean) — identified MISSING_* route as OK too.
			c = classify_fn(meta, accept_missing=accept_missing, verify_ok=verify_ok, strict_verify=not no_strict_verify)
			if c.verdict.value not in ("OK", "VERIFIED"):
				skipped_not_ok += 1
				progress.update(task_id, completed=i)
				continue
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
	t = Table(title=_("EPUB generation summary"), show_header=True, header_style="bold cyan")
	t.add_column(_("Metric"), style="bold")
	t.add_column(_("Count"), justify="right")
	t.add_row(_("Skipped (not OK)"), str(skipped_not_ok))
	t.add_row(_("Skipped (already has epub)"), str(skipped_has_epub))
	t.add_row(_("To generate"), str(len(results)))
	console.print(t)

	if not results:
		return

	# Source format breakdown
	src: Counter[str] = Counter(r.source_format for r in results)
	console.print()
	t = Table(title=_("By source format"), show_header=True, header_style="bold cyan")
	t.add_column(_("Format"))
	t.add_column(_("Count"), justify="right")
	for fmt, n in src.most_common():
		t.add_row(fmt, str(n))
	console.print(t)

	# Show sample (first 10)
	console.print()
	t = Table(title=_("Sample (first 10)"), show_header=True, header_style="bold cyan")
	t.add_column(_("ID"), justify="right", style="cyan")
	t.add_column(_("Source file"))
	t.add_column(_("Tool"))
	t.add_column(_("Output / Error"), style="dim")
	for r in results[:10]:
		out = r.output_file or r.error or ""
		t.add_row(str(r.book_id or "?"), Path(r.source_file).name[:40], r.tool, out[:50])
	console.print(t)


@main.command()
@click.option("--library", "library", type=click.Path(file_okay=False, path_type=Path), help=_("Library root"))
@click.option("--no-cache", is_flag=True, help=_("Disable SQLite cache"))
@click.option("--limit", type=int, default=None, help=_("Process only the first N books"))
@click.option("--needfix-dir", default=None, help=_("Folder for quarantined rogues (default: 'needfix')"))
@click.option("--apply", "do_apply", is_flag=True, help=_("Actually move rogues (default: dry-run)"))
@click.option("--threshold", "strong", type=float, default=0.8, help=_("Fuzzy title ratio at/above which a format AGREES (default 0.8)"))
@click.option("--weak-threshold", "weak", type=float, default=0.5, help=_("Fuzzy title ratio below which a format DISAGREES (default 0.5). Between weak and strong = UNCERTAIN (never moved)."))
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

	_validate_library(cfg.library)
	needfix = needfix_dir or DEFAULT_NEEDFIX_DIR

	console.print(f"[bold]{_('Cross-checking formats')}[/bold] [cyan]{cfg.library}[/cyan] [{'WRITE' if do_apply else 'DRY-RUN'}]", highlight=False)
	console.print(f"  {_('rogues go to')}:  [cyan]{needfix}/{CROSSCHECK_SUBDIR}/<origin> - <file>/[/cyan]")

	cache = _open_cache(cfg.cache_db, no_cache=no_cache)
	try:
		books = _scan_library_with_progress(cfg, cache, no_cache=no_cache)
		if limit is not None:
			books = books[:limit]

		results: list = []
		with Progress(
			SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
			BarColumn(complete_style="bright_magenta", finished_style="bright_magenta", pulse_style="bright_magenta"), TextColumn("{task.completed}/{task.total}"),
			TimeRemainingColumn(), console=console, transient=True,
		) as progress:
			task_id = progress.add_task(_("Cross-checking formats"), total=len(books))
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
				qtask = progress.add_task(_("Quarantining rogues"), total=rogue_count)

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
	t = Table(title=_("Crosscheck summary"), show_header=True, header_style="bold cyan")
	t.add_column(_("Decision"), style="bold")
	t.add_column(_("Count"), justify="right")
	decisions: Counter[str] = Counter(r.decision for r in results)
	checked = len(results) - decisions.get("skipped", 0)
	t.add_row(_("checked (≥2 formats)"), str(checked), style="dim")
	t.add_row(_("clean"), str(decisions.get("clean", 0)))
	t.add_row(_("quarantine"), str(decisions.get("quarantine", 0)), style="yellow")
	t.add_row(_("ambiguous"), str(decisions.get("ambiguous", 0)), style="magenta")
	t.add_row(_("skipped (<2 formats)"), str(decisions.get("skipped", 0)), style="dim")
	console.print(t)

	# Rogues sample (book × rogue-file), capped. Only 'quarantine' decisions
	# carry rogues (the files that will move); 'ambiguous' books are reported
	# separately below.
	rogues = [(r, rv) for r in results for rv in r.rogues]
	if rogues:
		console.print()
		t = Table(title=f"Rogues ({len(rogues)} file{'s' if len(rogues) != 1 else ''})", show_header=True, header_style="bold cyan")
		t.add_column(_("ID"), justify="right", style="cyan")
		t.add_column(_("Book folder"))
		t.add_column(_("File"))
		t.add_column(_("Reason"), style="dim")
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
		console.print(f"[magenta]{_('{count} ambiguous book(s)').format(count=len(ambiguous))}[/magenta] " + _("— formats disagree with metadata but no format corroborates it; not moved (review manually)."))
		t = Table(title=_("Ambiguous (not moved)"), show_header=True, header_style="bold cyan")
		t.add_column(_("ID"), justify="right", style="cyan")
		t.add_column(_("Book folder"))
		t.add_column(_("Disagreeing files"), style="dim")
		for r in ambiguous[:25]:
			bad = ", ".join(Path(rv.file).name for rv in r.verdicts if rv.verdict == "DISAGREES")
			t.add_row(str(r.book_id or "?"), Path(r.path).name[:45], bad[:60])
		console.print(t)

	if move_results:
		rc: Counter[str] = Counter(mr.action for mr in move_results)
		console.print()
		t = Table(title=f"Move results ({'WRITE' if do_apply else 'DRY-RUN'})", show_header=True, header_style="bold cyan")
		t.add_column(_("Action"))
		t.add_column(_("Count"), justify="right")
		for action, n in rc.most_common():
			t.add_row(action, str(n))
		console.print(t)
		if not do_apply:
			console.print("[dim]" + _("Dry-run: nothing moved. Re-run with --apply to quarantine the rogues.") + "[/dim]")


_STRIP_SCOPES = ("external", "embedded", "both")


@main.command()
@click.option("--library", "library", type=click.Path(file_okay=False, path_type=Path), help=_("Library root"))
@click.option("--no-cache", is_flag=True, help=_("Disable SQLite cache"))
@click.option("--limit", type=int, default=None, help=_("Process only the first N books"))
@click.option("--generated", "generated", type=click.Choice(_STRIP_SCOPES), is_flag=False, flag_value="both", default=None, help=_("Remove GENERATED covers (Calibre placeholders). Optional scope: external (sidecar files), embedded (inside EPUBs) or both (default)"))
@click.option("--invalid", "invalid", type=click.Choice(_STRIP_SCOPES), is_flag=False, flag_value="both", default=None, help=_("Remove INVALID covers — image-extension or cover.* files no decoder can read (they trigger ABS ffmpeg 'Invalid data found' errors). Optional scope: external, embedded or both (default)"))
@click.option("--apply", "do_apply", is_flag=True, help=_("Actually remove the covers (default: dry-run)"))
def strip_covers(library: Path | None, no_cache: bool, limit: int | None, do_apply: bool,
		generated: str | None, invalid: str | None) -> None:
	"""Remove auto-generated (Calibre placeholder) and/or invalid covers.

	Two selectors, each an optional-scope flag: `--generated` (C11 pixel
	analysis: cover.jpg sidecar renamed to cover.jpg.bak — never
	hard-deleted — and embedded EPUB covers stripped surgically) and
	`--invalid` (cover files no image decoder can read: an HTML page saved
	as .jpg, cover.html — Audiobookshelf still picks those as item covers
	by extension, which is what its ffmpeg "Invalid data found" resize
	errors come from; renamed to .bak as well). Each flag takes an optional
	value external/embedded/both; bare = both. Without either flag the
	command keeps its original behaviour (generated, both scopes).
	Non-EPUB format files are untouched — their covers live in binary EXTH
	headers with no safe removal path. After a write run the next
	`bmf analyze` sees MISSING_COVER and refetches a real cover.
	"""
	from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn

	from .covers import strip_generated_covers

	cfg = Config.from_env()
	if library is not None:
		cfg.library = library

	# Bare `bmf strip-covers` keeps the original behaviour: generated only,
	# both scopes. Passing either selector limits the run to what was asked.
	if generated is None and invalid is None:
		generated = "both"

	_validate_library(cfg.library)
	console.print(f"[bold]{_('Stripping covers')}[/bold] [cyan]{cfg.library}[/cyan] [{'WRITE' if do_apply else 'DRY-RUN'}]", highlight=False)
	console.print("[dim]" + _("generated: {generated}, invalid: {invalid}").format(
		generated=generated or _("off"), invalid=invalid or _("off"),
	) + "[/dim]")

	cache = _open_cache(cfg.cache_db, no_cache=no_cache)
	try:
		books = _scan_library_with_progress(cfg, cache, no_cache=no_cache)
		if limit is not None:
			books = books[:limit]

		results: list = []
		with Progress(
			SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
			BarColumn(complete_style="bright_yellow", finished_style="bright_yellow", pulse_style="bright_yellow"), TextColumn("{task.completed}/{task.total}"),
			TimeRemainingColumn(), console=console, transient=True,
		) as progress:
			task_id = progress.add_task(_("Stripping covers"), total=len(books))
			for meta in books:
				try:
					results.append(strip_generated_covers(
						meta.path, dry_run=not do_apply, generated=generated, invalid=invalid,
					))
				except Exception as e:  # noqa: BLE001
					log.warning("cover strip failed for %s: %s", meta.path, e)
				progress.update(task_id, advance=1)

		# Only a WRITE run changes folders (a dry-run leaves mtimes alone, so
		# the rows stay fresh); drop the touched ones so the next scan re-parses.
		if do_apply:
			touched = [r.path for r in results if r.touched]
			if touched and cache is not None:
				cache.invalidate_many(touched)
				cache.commit()
	finally:
		if cache is not None:
			cache.close()

	_print_strip_covers_summary(results, do_apply)


def _print_strip_covers_summary(results, do_apply: bool) -> None:  # noqa: ANN001
	console.print()
	t = Table(title=_("Cover strip summary"), show_header=True, header_style="bold cyan")
	t.add_column(_("Metric"), style="bold")
	t.add_column(_("Count"), justify="right")
	t.add_row(_("books scanned"), str(len(results)), style="dim")
	t.add_row(_("cover.jpg renamed to .bak"), str(sum(1 for r in results if r.cover_bak)))
	t.add_row(_("invalid cover files renamed to .bak"), str(sum(len(r.invalid_baks) for r in results)))
	t.add_row(_("embedded EPUB covers stripped"), str(sum(len(r.stripped_epubs) for r in results)))
	t.add_row(_("invalid embedded EPUB covers stripped"), str(sum(len(r.invalid_epubs) for r in results)))
	t.add_row(_("strip failures"), str(sum(len(r.failed_epubs) for r in results)))
	console.print(t)

	touched = [r for r in results if r.touched]
	if touched:
		console.print()
		t = Table(title=_("Affected books (first 25)"), show_header=True, header_style="bold cyan")
		t.add_column(_("Book folder"))
		t.add_column(_("Actions"), style="dim")
		for r in touched[:25]:
			parts = []
			if r.cover_bak:
				parts.append("cover.jpg -> .bak")
			if r.stripped_epubs:
				parts.append(_("stripped: {files}").format(files=", ".join(r.stripped_epubs)))
			if r.invalid_baks:
				parts.append(_("invalid -> .bak: {files}").format(files=", ".join(r.invalid_baks)))
			if r.invalid_epubs:
				parts.append(_("invalid stripped: {files}").format(files=", ".join(r.invalid_epubs)))
			if r.failed_epubs:
				parts.append(_("strip failed: {files}").format(files=", ".join(r.failed_epubs)))
			t.add_row(Path(r.path).name[:60], "; ".join(parts)[:75])
		if len(touched) > 25:
			t.add_row("…", f"({len(touched) - 25} more)")
		console.print(t)
		if any(r.failed_epubs for r in results):
			console.print("[yellow]" + _("Some EPUB covers probed as generated but could not be stripped (corrupt zip / unparseable OPF); see the list above.") + "[/yellow]")
		if not do_apply:
			console.print("[dim]" + _("Dry-run: nothing removed. Re-run with --apply to strip the covers.") + "[/dim]")


@main.command()
@click.option("--library", "library", type=click.Path(file_okay=False, path_type=Path), help=_("Library root"))
@click.option("--no-cache", is_flag=True, help=_("Disable SQLite cache"))
@click.option("--limit", type=int, default=None, help=_("Process only the first N books"))
@click.option("--apply", "do_apply", is_flag=True, help=_("Actually modify the library (default: dry-run)"))
@click.option("--covers/--no-covers", "clean_covers", default=True, help=_("Clean invalid and generated covers (default: yes)"))
@click.option("--unverified/--no-unverified", "clean_unverified", default=True, help=_("Clear `verified` flag from books whose author/series cannot be confirmed online (default: yes)"))
def clean(library: Path | None, no_cache: bool, limit: int | None, do_apply: bool, clean_covers: bool, clean_unverified: bool) -> None:
	"""Clean invalid data and unconfirmed verified flags from the library.

	Unifies cleanup operations in a single pass (dry-run by default):
	1. --covers: Renames generated Calibre placeholder covers and unreadable
	   image files (HTML saved as .jpg) to .bak, and strips them from EPUBs.
	2. --unverified: Audits books marked as `verified: true`. If a book has no
	   ISBN and its author/series does not exist online (suspected LLM hallucination),
	   its `verified` flag is cleared, returning it to review.
	"""
	from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn

	from .covers import strip_generated_covers
	from .enrichers import Enricher
	from .writers import clear_verified

	cfg = Config.from_env()
	if library is not None:
		cfg.library = library
	_validate_library(cfg.library)

	console.print("[bold]" + _("Cleaning library") + f"[/bold] [cyan]{cfg.library}[/cyan] [{'WRITE' if do_apply else 'DRY-RUN'}]", highlight=False)
	console.print("[dim]" + _("covers: {covers}, unverified: {unverified}").format(covers=clean_covers, unverified=clean_unverified) + "[/dim]")

	cache = _open_cache(cfg.cache_db, no_cache=no_cache)
	enricher = Enricher(
		cache_db=cfg.cache_db,
		databazeknih_enabled=cfg.databazeknih_enabled,
		legie_enabled=cfg.legie_enabled,
		abs_czech_url=cfg.abs_czech_url or None,
		abs_czech_token=cfg.abs_czech_token,
	) if clean_unverified else None

	try:
		books = _scan_library_with_progress(cfg, cache, no_cache=no_cache)
		if limit is not None:
			books = books[:limit]

		cover_results: list = []
		unverified_cleared: list[str] = []

		with Progress(
			SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
			BarColumn(complete_style="bright_yellow", finished_style="bright_yellow", pulse_style="bright_yellow"), TextColumn("{task.completed}/{task.total}"),
			TimeRemainingColumn(), console=console, transient=True,
		) as progress:
			task_id = progress.add_task(_("Cleaning"), total=len(books))
			for meta in books:
				# 1. Clean covers
				if clean_covers:
					try:
						cover_results.append(strip_generated_covers(
							meta.path, dry_run=not do_apply, generated="both", invalid="both",
						))
					except Exception as e:  # noqa: BLE001
						log.warning("cover strip failed for %s: %s", meta.path, e)

				# 2. Audit unverified books
				if clean_unverified and meta.verified:
					# First, if it has an ISBN, it's generally safe
					is_safe = False
					if meta.isbn:
						is_safe = True
					else:
						# Check author or series
						author = meta.authors[0] if meta.authors else None
						series = meta.series[0] if meta.series else None
						if isinstance(series, dict):
							series = str(series.get("name") or "")
						elif isinstance(series, str):
							from .models import series_entry_pair
							series, _series_idx = series_entry_pair(series)

						if author and enricher is not None and enricher.author_exists(author):
							is_safe = True
						elif not author and series and enricher is not None and enricher.series_exists(series):
							is_safe = True

					if not is_safe:
						unverified_cleared.append(meta.path)
						if do_apply:
							clear_verified(Path(meta.path))

				progress.update(task_id, advance=1)

		if do_apply:
			touched = [r.path for r in cover_results if getattr(r, "touched", False)] + unverified_cleared
			if touched and cache is not None:
				cache.invalidate_many(touched)
				cache.commit()

	finally:
		if cache is not None:
			cache.close()

	if clean_covers:
		_print_strip_covers_summary(cover_results, do_apply)

	if clean_unverified:
		console.print()
		t = Table(title=_("Unverified Audit Summary"), show_header=True, header_style="bold cyan")
		t.add_column(_("Metric"), style="bold")
		t.add_column(_("Count"), justify="right")
		t.add_row(_("books scanned"), str(len(books)), style="dim")
		t.add_row(_("verified flags cleared"), str(len(unverified_cleared)))
		console.print(t)

		if unverified_cleared:
			console.print()
			t = Table(title=_("Unverified Books (first 25)"), show_header=True, header_style="bold cyan")
			t.add_column(_("Book folder"))
			for p in unverified_cleared[:25]:
				t.add_row(Path(p).name[:80])
			if len(unverified_cleared) > 25:
				t.add_row("…", f"({len(unverified_cleared) - 25} more)")
			console.print(t)

		if not do_apply:
			console.print("[dim]" + _("Dry-run: verified flags were NOT cleared. Re-run with --apply to clear them.") + "[/dim]")


@main.command()
@click.option("--library", "library", type=click.Path(file_okay=False, path_type=Path), help=_("Library root"))
@click.option("--no-cache", is_flag=True, help=_("Disable SQLite cache"))
@click.option("--limit", type=int, default=None, help=_("Process only the first N books (for testing)"))
@click.option("--authors", "do_authors", is_flag=True, default=False, help=_("Unify author-name variants (C15: initials vs full names, diacritics, titles, anonym spellings, swapped name order)"))
@click.option("--genres", "do_genres", is_flag=True, default=False, help=_("Canonicalize genres to Czech names (C16: case/diacritics/word-order duplicates, English→Czech and singular/plural aliases)"))
@click.option("--tags", "do_tags", is_flag=True, default=False, help=_("Canonicalize tags the same way as genres (shares the vocabulary)"))
@click.option("--samples", type=int, default=25, help=_("Number of clusters to show per table"))
@click.option("--apply", "do_apply", is_flag=True, help=_("Write the proposals into review.yaml as C15/C16 entries (book metadata itself is written later by `bmf apply`; default: dry-run)"))
def normalize(library: Path | None, no_cache: bool, limit: int | None, do_authors: bool, do_genres: bool, do_tags: bool, samples: int, do_apply: bool) -> None:
	"""Unify author-name variants and genre tags across the whole library.

	The per-book detectors judge one folder in isolation; a "Robert A.
	Heinlein" vs "Robert Anson Heinlein" pair or a "sci-fi"/"Sci-fi"/
	"Science Fiction" trio only becomes visible across books. This command
	clusters them and (with --apply) fills review.yaml with C15 (author
	variant / swapped name order) and C16 (genre/tag variant) entries —
	deterministic fixes arrive pre-filled `accept`, judgement calls (letter
	variants, unevidenced comma reorders) stay pending. Run `bmf apply`
	afterwards to write them; author renames also move folders, so finish
	with `bmf abs-rescan`. Without a selector flag all three categories run.
	"""
	from .normalize import analyze_library
	from .review import merge_normalizations

	cfg = Config.from_env()
	if library is not None:
		cfg.library = library

	if not (do_authors or do_genres or do_tags):
		do_authors = do_genres = do_tags = True

	_validate_library(cfg.library)
	# The two _() header strings sit OUTSIDE the f-string — babel on py3.10
	# cannot extract calls from f-string holes (same as abs_rescan).
	title = _("Normalizing authors and genres")
	mode = "WRITE" if do_apply else "DRY-RUN"
	console.print(f"[bold]{title}[/bold] [cyan]{cfg.library}[/cyan] [{mode}]", highlight=False)
	console.print("[dim]" + _("authors: {a}, genres: {g}, tags: {t}").format(
		a=_("on") if do_authors else _("off"), g=_("on") if do_genres else _("off"), t=_("on") if do_tags else _("off"),
	) + "[/dim]")

	cache = _open_cache(cfg.cache_db, no_cache=no_cache)
	try:
		books = _scan_library_with_progress(cfg, cache, no_cache=no_cache)
		if limit is not None:
			books = books[:limit]
		if not books:
			console.print("[red]" + _("No books found.") + "[/red]")
			sys.exit(1)
		result = analyze_library(
			books, fields=tuple(n for n, on in (("authors", do_authors), ("genres", do_genres), ("tags", do_tags)) if on)
		)
	finally:
		if cache is not None:
			cache.close()

	_print_normalize_clusters(result, samples)
	n_accept = sum(1 for p in result.proposals if p.high_confidence)
	console.print()
	console.print(
		_("Proposals for {books} book(s): {accept} pre-filled accept, {pending} pending review").format(
			books=len(result.proposals), accept=n_accept, pending=len(result.proposals) - n_accept,
		)
	)
	if do_apply:
		if not result.proposals:
			console.print("[dim]" + _("Nothing to write.") + "[/dim]")
			return
		summary = merge_normalizations(cfg.review_file, result.proposals, books, library_root=cfg.library)
		console.print(
			_("review.yaml updated: {added} entry/entries added, {updated} updated, {skipped} already decided (skipped)").format(
				added=summary["added"], updated=summary["updated"], skipped=summary["skipped_decided"],
			)
		)
		console.print("[dim]" + _("Review with `bmf gui`, then run `bmf apply`. Author renames move folders — finish with `bmf abs-rescan`.") + "[/dim]")
	else:
		console.print("[dim]" + _("Dry-run: nothing written. Re-run with --apply to fill review.yaml.") + "[/dim]")


def _print_normalize_clusters(result, samples: int) -> None:  # noqa: ANN001
	console.print()
	if result.author_clusters:
		t = Table(title=_("Author clusters (C15)"), show_header=True, header_style="bold cyan")
		t.add_column(_("Canonical"))
		t.add_column(_("Variants"), style="dim", max_width=60)
		t.add_column(_("Conf"), justify="center")
		for c in result.author_clusters[:samples]:
			vars_ = "; ".join(f"{v} ×{n}" for v, n in c.variants if v != c.canonical)
			style = "green" if c.confidence.value == "HIGH" else "yellow"
			t.add_row(f"[{style}]{c.canonical}[/{style}]", vars_[:120], f"[{style}]{c.confidence.value}[/{style}]")
		console.print(t)
		if len(result.author_clusters) > samples:
			console.print(f"[dim]… {len(result.author_clusters) - samples} " + _("more") + "[/dim]")
	if result.genre_clusters:
		t = Table(title=_("Genre/tag clusters (C16)"), show_header=True, header_style="bold cyan")
		t.add_column(_("Canonical"))
		t.add_column(_("Variants"), style="dim", max_width=60)
		t.add_column(_("Kind"), justify="center")
		t.add_column(_("Conf"), justify="center")
		for c in result.genre_clusters[:samples]:
			vars_ = "; ".join(f"{v} ×{n}" for v, n in c.variants if v != c.canonical)
			style = "green" if c.confidence.value == "HIGH" else "yellow"
			t.add_row(f"[{style}]{c.canonical}[/{style}]", vars_[:110], c.kind, f"[{style}]{c.confidence.value}[/{style}]")
		console.print(t)
		if len(result.genre_clusters) > samples:
			console.print(f"[dim]… {len(result.genre_clusters) - samples} " + _("more") + "[/dim]")
	if result.multi_author:
		t = Table(title=_("Skipped: multi-author strings (C7/C8 territory)"), show_header=True, header_style="bold yellow")
		t.add_column(_("String"))
		t.add_column(_("Books"), justify="right")
		for raw, n in result.multi_author[: samples]:
			t.add_row(raw[:70], str(n))
		console.print(t)


@main.command(name="abs-rescan")
@click.option("--library", "library", type=click.Path(file_okay=False, path_type=Path), help=_("Library root (default: $BMF_LIBRARY or ~/Books)"))
@click.option("--since", "since", default="24h", help=_("Rescan books changed within this window, e.g. 90m, 2h, 3d (default: 24h)"))
@click.option("--url", "url", default=None, help=_("Audiobookshelf base URL (default: $BMF_ABS_URL)"))
@click.option("--abs-library", "abs_library", default=None, help=_("Audiobookshelf library name or id (default: auto-detect)"))
@click.option("--fix-covers", "fix_covers", is_flag=True, help=_("Also clear broken covers stored in the ABS database (coverPath pointing at a non-image or missing file — behind the ffmpeg 'Invalid data found' errors) and rescan those items"))
@click.option("--force-all", "force_all", is_flag=True, help=_("Force-rescan the whole ABS library instead of only the changed books"))
@click.option("--abs-workers", "abs_workers", type=int, default=None, help=_("Parallel per-item scan requests (default: 4, BMF_ABS_WORKERS; 1 = serial)"))
@click.option("--apply", "do_apply", is_flag=True, help=_("Actually trigger the rescan (default: dry-run)"))
def abs_rescan(library: Path | None, since: str, url: str | None, abs_library: str | None, fix_covers: bool, force_all: bool, abs_workers: int | None, do_apply: bool) -> None:
	"""Tell Audiobookshelf to re-read the metadata of recently changed books.

	ABS keeps its own database and a plain library scan skips every folder
	it considers unchanged (mtime gate; an NFS attribute cache can mask
	even a new mtime), so bmf's writes stay invisible in ABS. This command
	finds book folders changed within --since, maps them to ABS library
	items and triggers a per-item re-scan through the ABS API, which
	re-reads metadata.json unconditionally. Requires BMF_ABS_URL and
	BMF_ABS_TOKEN (an ADMIN API token — scan endpoints reject the rest).

	--fix-covers adds a pass over ALL items: a stored coverPath row that
	points at a non-image file (metadata.json — a leftover no current ABS
	build writes or heals) or at a missing file is nulled via
	DELETE /api/items/{id}/cover and the item joins the rescan, so ABS
	picks a real cover again. Run `bmf strip-covers --invalid --apply`
	FIRST — a folder still holding an unreadable cover.jpg would just get
	it re-picked.
	"""
	import time as _time

	from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn

	from .abs_client import AudiobookshelfClient, broken_cover_items, changed_folders, match_items, parse_since_duration

	cfg = Config.from_env()
	if library is not None:
		cfg.library = library
	if abs_workers is not None:
		cfg.abs_workers = max(1, abs_workers)

	_validate_library(cfg.library)

	base_url = url if url is not None else cfg.abs_url
	if not base_url or not cfg.abs_token:
		console.print(
			f"[bold red]{_('Error:')}[/bold red] "
			+ _("Audiobookshelf is not configured: set BMF_ABS_URL and BMF_ABS_TOKEN (an admin API token) in .env or the environment.")
		)
		sys.exit(1)

	try:
		since_sec = parse_since_duration(since)
	except ValueError:
		console.print(f"[bold red]{_('Error:')}[/bold red] " + _("Invalid --since value: {value}").format(value=since))
		sys.exit(1)
	since_ts = _time.time() - since_sec

	# The two _() calls sit OUTSIDE the f-string on purpose: babel (Python
	# 3.10 tokenizer) cannot see function calls inside f-string holes, so an
	# inlined _('...') would never reach the .pot and stay untranslated.
	header = _("ABS rescan")
	window_lbl = _("changed within {window}").format(window=since)
	console.print(
		f"[bold]{header}[/bold] [cyan]{base_url}[/cyan] [{'WRITE' if do_apply else 'DRY-RUN'}] "
		f"[dim]{window_lbl}[/dim]",
		highlight=False,
	)
	client = AudiobookshelfClient(base_url, cfg.abs_token)

	# The per-item HTTP loops below run for many minutes (each POST waits for
	# ABS to re-read one item); both drive this bar so the run shows N/M + ETA
	# instead of a silent, hung-looking terminal. The bar stays on screen at
	# the end (not transient) as a completion record.
	def _new_progress() -> Progress:
		return Progress(
			SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
			BarColumn(complete_style="cyan", finished_style="cyan", pulse_style="cyan"), TextColumn("{task.completed}/{task.total}"),
			TimeRemainingColumn(), console=console,
		)

	libs = client.libraries()
	if libs is None:
		console.print(f"[bold red]{_('Error:')}[/bold red] " + _("Cannot reach Audiobookshelf at {url} — check the URL and token.").format(url=base_url))
		sys.exit(1)

	book_libs = [l for l in libs if str(l.get("mediaType") or "book") == "book"]
	if not book_libs:
		console.print(f"[bold red]{_('Error:')}[/bold red] " + _("No book libraries found on the ABS server."))
		sys.exit(1)

	selected = _select_abs_library(book_libs, wanted=abs_library if abs_library is not None else cfg.abs_library, library_root=cfg.library)
	if selected is None:
		names = ", ".join(str(l.get("name") or l.get("id")) for l in book_libs)
		console.print(
			f"[bold red]{_('Error:')}[/bold red] "
			+ _("Multiple book libraries on the ABS server; pick one with --abs-library (or BMF_ABS_LIBRARY). Available: {names}").format(names=names)
		)
		sys.exit(1)
	lib_id = str(selected.get("id"))
	console.print(_("ABS library: {name}").format(name=selected.get("name") or lib_id), highlight=False)

	# --fix-covers pass: audit EVERY item's stored cover row (a stale row is
	# usually years old — --since must not filter it). The item listing is
	# fetched here and reused by the changed-folders matching below.
	items = None
	cleared_ids: list[str] = []
	broken: list = []
	if fix_covers:
		items = client.items(lib_id)
		if items is None:
			console.print(f"[bold red]{_('Error:')}[/bold red] " + _("Cannot list ABS library items — check the URL/token."))
			sys.exit(1)
		abs_folders = [str(f.get("fullPath") or "") for f in (selected.get("folders") or [])]
		broken = broken_cover_items(items, cfg.library, abs_folders)
		if not broken:
			console.print("[green]" + _("No broken item covers found in the ABS database.") + "[/green]")
		else:
			_print_broken_covers(broken, do_apply)
			if do_apply:
				failed_clears: list[str] = []
				with _new_progress() as progress:
					task_id = progress.add_task(_("Clearing broken covers"), total=len(broken))
					for b in broken:
						if client.clear_item_cover(b.item.id):
							cleared_ids.append(b.item.id)
						else:
							failed_clears.append(b.item.title or b.item.id)
						progress.update(task_id, advance=1)
				if failed_clears:
					sample = ", ".join(failed_clears[:3])
					console.print(
						"[yellow]"
						+ _("Failed to clear {count} stored item cover(s) (e.g. {sample}) — check the token (must be an ADMIN API token).").format(count=len(failed_clears), sample=sample)
						+ "[/yellow]"
					)
				if cleared_ids:
					console.print("[green]" + _("Cleared {count} stored item cover(s) — the rescan below lets ABS pick a real cover again.").format(count=len(cleared_ids)) + "[/green]")

	if force_all:
		if not do_apply:
			console.print("[dim]" + _("Dry-run: would force-rescan the whole ABS library. Re-run with --apply.") + "[/dim]")
		elif client.scan_library(lib_id, force=True):
			console.print("[green]" + _("Force-rescan of the whole ABS library requested — ABS re-reads every item in the background.") + "[/green]")
		else:
			console.print("[bold red]" + _("Failed to request the library scan — check the token (must be an ADMIN API token) and URL.") + "[/bold red]")
			sys.exit(1)
		return

	folders = changed_folders(cfg.library, since_ts)
	mres = None
	if folders:
		if items is None:
			items = client.items(lib_id)
			if items is None:
				console.print(f"[bold red]{_('Error:')}[/bold red] " + _("Cannot list ABS library items — check the URL/token."))
				sys.exit(1)
		mres = match_items(folders, items, cfg.library)
		_print_abs_rescan_summary(mres, do_apply)
	elif not cleared_ids and not broken:
		console.print("[green]" + _("No books changed within {window} — nothing to rescan.").format(window=since) + "[/green]")
		return

	# Scan set: recently changed items + the freshly cleared ones (a cleared
	# cover row needs a rescan to pick a new cover, changed or not).
	scan_ids = list(mres.item_ids) if mres else []
	scan_ids.extend(i for i in cleared_ids if i not in scan_ids)
	if do_apply and scan_ids:
		with _new_progress() as progress:
			task_id = progress.add_task(_("Rescanning ABS items"), total=len(scan_ids))

			def _scan_cb(done: int, total: int) -> None:
				progress.update(task_id, completed=done)

			failed_scans = client.scan_items(scan_ids, progress_callback=_scan_cb, workers=cfg.abs_workers)
		if failed_scans:
			msg = _("Failed to request the rescan of {count} of {total} ABS items — check the token (must be an ADMIN API token) and URL.").format(
				count=failed_scans, total=len(scan_ids)
			)
			if failed_scans == len(scan_ids):
				console.print(f"[bold red]{msg}[/bold red]", highlight=False)
				sys.exit(1)
			console.print(f"[yellow]{msg}[/yellow]", highlight=False)
		else:
			console.print(
				"[green]"
				+ _("Rescan of {count} ABS items done — refresh the ABS web UI to see the new metadata.").format(count=len(scan_ids))
				+ "[/green]"
			)

	# Unmatched folders (moved or brand-new): ABS has never seen their path,
	# so no per-item id exists to scan. A plain library scan teaches ABS the
	# new paths and fully reads metadata.json for items it discovers; the
	# endpoint is async server-side (200 = accepted, scan runs in the
	# background), so firing it costs no command time. Deliberately AFTER the
	# per-item loop: while our scans run, the library scanner would race over
	# the same items and double the server load for no gain.
	if mres is not None and mres.unmatched:
		if do_apply:
			if client.scan_library(lib_id):
				console.print(
					"[green]"
					+ _("Plain ABS library scan requested in the background — it learns the paths of the {count} unmatched book(s); re-run abs-rescan afterwards if any stay unmatched.").format(count=len(mres.unmatched))
					+ "[/green]"
				)
			else:
				# A follow-up nicety, not the primary work (the matched
				# rescans are already delivered) — warn, do not fail the run.
				console.print(
					"[yellow]"
					+ _("Failed to request the library scan — check the token (must be an ADMIN API token) and URL.")
					+ "[/yellow]"
				)
		else:
			console.print(
				"[dim]"
				+ _("Dry-run: would trigger a plain ABS library scan for the {count} unmatched book(s).").format(count=len(mres.unmatched))
				+ "[/dim]"
			)


def _common_tail_components(a: str, b: str) -> int:
	"""Length of the shared trailing path-component suffix of two posix paths."""
	ac = [c for c in a.split("/") if c]
	bc = [c for c in b.split("/") if c]
	n = 0
	while n < len(ac) and n < len(bc) and ac[-1 - n].lower() == bc[-1 - n].lower():
		n += 1
	return n


def _select_abs_library(book_libs: list[dict], *, wanted: str, library_root: Path) -> dict | None:
	"""Pick the ABS book library: explicit name/id > single library > path tail.

	The tail match compares our library root with each ABS library's folder
	fullPath component-wise from the end — the same storage is usually
	mounted under different prefixes (workstation NFS mount vs. the ABS
	container), so a shared trailing folder name is the expected signal.
	Returns None when it cannot decide (the caller lists the options and
	exits).
	"""
	w = (wanted or "").strip().lower()
	if w:
		for lib in book_libs:
			if str(lib.get("id", "")).lower() == w or str(lib.get("name", "")).lower() == w:
				return lib
		return None
	if len(book_libs) == 1:
		return book_libs[0]
	root_posix = library_root.resolve().as_posix().rstrip("/")
	candidates: list[dict] = []
	for lib in book_libs:
		for folder in lib.get("folders") or []:
			full = str(folder.get("fullPath") or "").rstrip("/")
			if full and _common_tail_components(root_posix, full) >= 1:
				candidates.append(lib)
				break
	return candidates[0] if len(candidates) == 1 else None


def _print_abs_rescan_summary(mres, do_apply: bool) -> None:  # noqa: ANN001
	console.print()
	t = Table(title=_("ABS rescan summary"), show_header=True, header_style="bold cyan")
	t.add_column(_("Metric"), style="bold")
	t.add_column(_("Count"), justify="right")
	t.add_row(_("books changed"), str(len(mres.matched) + len(mres.unmatched)))
	t.add_row(_("matched to ABS items"), str(len(mres.matched)))
	t.add_row(_("not found in ABS"), str(len(mres.unmatched)))
	console.print(t)

	if mres.matched:
		console.print()
		t = Table(title=_("Matched books (first 25)"), show_header=True, header_style="bold cyan")
		t.add_column(_("Book folder"))
		t.add_column(_("ABS item"), style="dim")
		for folder, item in mres.matched[:25]:
			t.add_row(folder.name[:60], (item.title or item.id)[:60])
		if len(mres.matched) > 25:
			t.add_row("…", f"({len(mres.matched) - 25} more)")
		console.print(t)

	if mres.unmatched:
		console.print()
		if do_apply:
			hint = _("Not found in ABS (moved or new): a plain library scan below lets ABS learn the new paths; re-run abs-rescan afterwards if any stay unmatched.")
		else:
			hint = _("Not found in ABS (moved or new): run a plain library scan in ABS once so it learns the new paths, then re-run abs-rescan.")
		console.print("[yellow]" + hint + "[/yellow]")
		t = Table(show_header=False)
		for folder in mres.unmatched[:10]:
			t.add_row(folder.name[:70])
		if len(mres.unmatched) > 10:
			t.add_row(f"… ({len(mres.unmatched) - 10} more)")
		console.print(t)

	if not do_apply:
		console.print("[dim]" + _("Dry-run: nothing sent. Re-run with --apply to trigger the rescan.") + "[/dim]")


def _print_broken_covers(broken, do_apply: bool) -> None:  # noqa: ANN001
	"""The --fix-covers report: items whose stored coverPath row is junk."""
	console.print()
	t = Table(title=_("Broken covers in the ABS database"), show_header=True, header_style="bold cyan")
	t.add_column(_("Title"))
	t.add_column(_("Stored cover path"), style="dim")
	t.add_column(_("Reason"))
	for b in broken[:25]:
		reason = _("not an image file") if b.reason == "ext" else _("file missing")
		t.add_row((b.item.title or b.item.id)[:50], b.cover_path[-64:], reason)
	if len(broken) > 25:
		t.add_row("…", f"({len(broken) - 25} more)")
	console.print(t)
	if not do_apply:
		console.print("[dim]" + _("Dry-run: the stored covers stay as they are. Re-run with --apply to clear and rescan them.") + "[/dim]")


# Required imports for the new commands
# (Enricher is imported lazily inside analyze() to avoid loading requests
#  when the user only runs scan/report.)


def _print_scan_summary(books) -> None:  # noqa: ANN001
	"""Print a rich table of scan statistics."""
	console.print()
	console.print(f"[bold green]{_('Found {count} books').format(count=len(books))}[/bold green]")

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

	t = Table(title=_("Scan summary"), show_header=True, header_style="bold cyan")
	t.add_column(_("Metric"), style="bold")
	t.add_column(_("Count"), justify="right")
	t.add_column(_("%"), justify="right")
	t.add_row(_("Total books"), str(len(books)), "100%")
	t.add_row(_("With metadata.json"), str(has_json), f"{has_json / len(books) * 100:.0f}%")
	t.add_row(_("With ISBN (valid)"), str(has_isbn), f"{has_isbn / len(books) * 100:.0f}%")
	t.add_row(_("With publication year"), str(has_year), f"{has_year / len(books) * 100:.0f}%")
	t.add_row(_("Mojibake repaired"), str(enc_repaired), f"{enc_repaired / len(books) * 100:.0f}%")
	t.add_row(_("Mojibake unrepairable"), str(enc_unrepairable), f"{enc_unrepairable / len(books) * 100:.0f}%")
	console.print(t)

	# Source table
	t = Table(title=_("Metadata source"), show_header=True, header_style="bold cyan")
	t.add_column(_("Source"))
	t.add_column(_("Count"), justify="right")
	for src_name, n in src.most_common():
		t.add_row(src_name, str(n))
	console.print(t)

	# Format table
	t = Table(title=_("File formats present"), show_header=True, header_style="bold cyan")
	t.add_column(_("Format"))
	t.add_column(_("Books"), justify="right")
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
	console.print(f"[bold green]{_('Detected {count} books').format(count=total)}[/bold green]  [dim]{_('(a book may appear in multiple categories)')}[/dim]")

	# Summary table
	t = Table(title=_("Detection summary"), show_header=True, header_style="bold cyan")
	t.add_column(_("Category"), style="bold")
	t.add_column(_("Count"), justify="right")
	t.add_column(_("Verdict"), style="magenta")
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
			console.print(f"[yellow]{_('No books in category {cat}.').format(cat=cat)}[/yellow]")
			continue
		console.print()
		console.print(f"[bold]{_('Category {cat} — {count} books (showing {shown}):').format(cat=cat, count=len(by_cat[cat]), shown=min(samples, len(by_cat[cat])))}")
		t = Table(show_header=True, header_style="bold", show_lines=False)
		t.add_column(_("ID"), justify="right", style="cyan")
		t.add_column(_("Author folder"))
		t.add_column(_("Title"))
		t.add_column(_("Reason"), style="dim")
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
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None, help=_("Write the script to a file instead of stdout."))
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
		console.print(f"[green]{_('Completion script written to')}[/green] {output}")
		# Shell-specific activation hint.
		if shell == "bash":
			console.print(f"[dim]{_('Run:')} source {output}[/dim]")
		elif shell == "zsh":
			console.print(f"[dim]{_('Run:')} source {output} {_('(or add to your ~/.zshrc)')}[/dim]")
		elif shell == "fish":
			console.print("[dim]" + _("Fish loads it automatically from ~/.config/fish/completions/") + "[/dim]")
	else:
		# Print raw script to stdout (not via rich) so 'eval "$(bmf ...)"' works.
		click.echo(script)


if __name__ == "__main__":
	main()
