"""Organize the library: move OK books to a clean path, broken books to needfix/.

Two operations, exposed via `bmf organize`:

1. OK books (verdict OK or VERIFIED) -> moved to a configurable path pattern,
   defaulting to `<Author>/<Title> (<calibre_id>)/`. Pattern supports:
   {author}, {author_sort}, {title}, {title_sort}, {id}, {isbn}, {year},
   {language}, {series}, {series_index}.

2. Broken books (verdict NEEDS_REVIEW / UNFIXABLE / AUTO_FIXABLE) -> moved
   to a `needfix/` folder (configurable), preserving the rest of their path.
   Example:
     <lib>/Karel Capek/R.U.R. (4895)/  ->  <lib>/needfix/Karel Capek/R.U.R. (4895)/

Both are dry-run by default. Moves are atomic per book (rename within the
same filesystem); cross-filesystem moves fall back to copy+delete.

OK books that land on the same target path are de-duplicated instead of
silently renamed: same-book duplicates are MERGED (one folder, all formats,
field-merged metadata), and genuinely-different books are disambiguated by
year (``Title (2026)/``) or id (``Title (id123)/``); `` (dup N)`` survives
only as a last-resort fallback (duplicate calibre_ids under an ``{id}``
pattern). See :func:`same_book` / :func:`merge_folders`.
"""
from __future__ import annotations

import copy
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .detectors import _is_anonym_spelling
from .isbn import canonicalize
from .models import BookMeta, Verdict
from .verifier import _fuzzy_match

if TYPE_CHECKING:
	from .library import Cache

log = logging.getLogger(__name__)

DEFAULT_PATH_PATTERN = "{author}/{title} ({id})"
DEFAULT_NEEDFIX_DIR = "needfix"

# Canonical folder name for books whose author is an anonym spelling
# ("Neznamy", "neznámý - neuveden", "Unknown", …). All variants collapse here
# so the library has a single anonym tree instead of one per spelling.
ANONYM_AUTHOR_NAME = "Anonym"

# Characters forbidden in file/folder names on Windows + most filesystems.
# Also strip leading/trailing dots and spaces (filesystem-specific issues).
_FORBIDDEN_CHARS = re.compile(r'[\\/:*?"<>|\n\r\t]')
# Control characters 0x00-0x1F
_CONTROL_CHARS = re.compile(r"[\x00-\x1f]")


@dataclass
class MoveResult:
	"""Result of moving a single book."""

	source: str
	destination: str
	action: str  # 'moved' | 'already_correct' | 'collision_renamed' | 'merged' | 'error'
	error: str | None = None
	# For 'merged': list of (loser_filename, outcome_or_new_name) describing the
	# format files moved from the loser folder. None for non-merge actions.
	details: list[tuple[str, str]] | None = None


def compute_target_path(meta: BookMeta, pattern: str, library: Path) -> Path:
	"""Compute the target folder for a book, given a path pattern.

	The pattern is a Python format-string. Available fields:
		{author}      — first author (or "Anonym" if none)
		{author_sort} — "Lastname, Firstname" form
		{title}       — book title
		{title_sort}  — title with leading article moved (best-effort)
		{id}          — calibre_id
		{isbn}        — ISBN (or empty)
		{year}        — publication year (or empty)
		{language}    — language code
		{series}      — series name (or empty)
		{series_index}— index within series (or empty)
	"""
	author = meta.authors[0] if meta.authors else ANONYM_AUTHOR_NAME
	# Collapse every anonym spelling ("Neznamy", "neznámý - neuveden",
	# "Unknown", …) into one canonical folder so the library has a single
	# anonym tree.
	if _is_anonym_spelling(author):
		author = ANONYM_AUTHOR_NAME
	author_sort = _author_sort(author)
	title = meta.title or "Untitled"
	title_sort = _title_sort(title)
	series = ""
	series_index = ""
	if meta.series and isinstance(meta.series, list) and meta.series:
		s0 = meta.series[0]
		if isinstance(s0, dict):
			series = s0.get("name", "") or ""
			series_index = str(s0.get("index", "") or "")

	fields = {
		"author": sanitize_segment(author),
		"author_sort": sanitize_segment(author_sort),
		"title": sanitize_segment(title),
		"title_sort": sanitize_segment(title_sort),
		"id": str(meta.calibre_id) if meta.calibre_id is not None else "noid",
		"isbn": sanitize_segment(meta.isbn or ""),
		"year": str(meta.year) if meta.year else "",
		"language": sanitize_segment(meta.language or ""),
		"series": sanitize_segment(series),
		"series_index": sanitize_segment(series_index),
	}
	try:
		rel = pattern.format(**fields)
	except KeyError as e:
		log.warning("unknown pattern field %s in %r; falling back to default", e, pattern)
		rel = DEFAULT_PATH_PATTERN.format(**fields)
	# Collapse multiple slashes, strip trailing
	rel = re.sub(r"/+", "/", rel).strip("/")
	return library / rel


def sanitize_segment(name: str) -> str:
	"""Make a string safe for use as a file/folder name.

	- Replace forbidden chars with underscore
	- Strip control characters
	- Collapse whitespace
	- Strip leading/trailing dots and spaces
	- Truncate to 200 chars (filesystem limits with some headroom)
	"""
	s = _FORBIDDEN_CHARS.sub("_", name)
	s = _CONTROL_CHARS.sub("", s)
	s = re.sub(r"\s+", " ", s).strip()
	s = s.strip(". ")
	# Empty? use a placeholder
	if not s:
		s = "_"
	return s[:200]


def _author_sort(author: str) -> str:
	"""Convert 'First Last' -> 'Last, First' for sorting."""
	parts = author.strip().split()
	if len(parts) >= 2:
		return f"{parts[-1]}, {' '.join(parts[:-1])}"
	return author


def _title_sort(title: str) -> str:
	"""Best-effort title_sort: strip leading 'The/A/An' (also CZ 'Příběh o')."""
	for prefix in ("The ", "A ", "An ", "Le ", "La ", "Les ", "Die ", "Das "):
		if title.startswith(prefix):
			return title[len(prefix) :] + ", " + prefix.strip()
	return title


def move_book(
	source: Path,
	destination: Path,
	*,
	dry_run: bool = True,
	overwrite: bool = False,
	library: Path | None = None,
) -> MoveResult:
	"""Move a book folder from source to destination.

	- If destination already exists and is the same path -> 'already_correct'.
	- If destination exists but differs -> append ' (dup N)' suffix.
	- Within the same filesystem: atomic rename.
	- Cross-filesystem: copy + delete.
	- After a real move, empty parent directories under *library* are removed
	  up to (but not including) the library root, so relocating a book does not
	  leave behind orphaned author folders.
	"""
	source = Path(source)
	destination = Path(destination)

	# Already at the right place?
	try:
		if source.resolve() == destination.resolve():
			return MoveResult(str(source), str(destination), "already_correct")
	except OSError:
		pass

	# Handle collisions
	final_dest = destination
	if final_dest.exists():
		if overwrite:
			if not dry_run:
				shutil.rmtree(final_dest)
		else:
			# Append (dup 1), (dup 2), ... until free
			n = 1
			while True:
				candidate = destination.with_name(f"{destination.name} (dup {n})")
				if not candidate.exists():
					final_dest = candidate
					break
				n += 1
			action = "collision_renamed"
	else:
		action = "moved"

	if dry_run:
		return MoveResult(str(source), str(final_dest), action)

	# Ensure parent exists
	final_dest.parent.mkdir(parents=True, exist_ok=True)
	try:
		shutil.move(str(source), str(final_dest))
	except shutil.Error as e:
		return MoveResult(str(source), str(final_dest), "error", str(e))
	except OSError as e:
		return MoveResult(str(source), str(final_dest), "error", str(e))
	# Clean up empty parent directories left behind by the move (e.g. an author
	# folder that becomes empty once its only book moved elsewhere). Walk
	# upwards from the original source's parent and rmtree nothing — only
	# remove truly empty dirs, stopping at the library root (or the source's
	# own grandparent if no library bound was given).
	_prune_empty_parents(source.parent, library)
	return MoveResult(str(source), str(final_dest), action)


def _prune_empty_parents(start: Path, library: Path | None) -> None:
	"""Remove empty parent directories from *start* upwards, stopping at *library*.

	Only directories that contain nothing (no files, no subdirs) are removed.
	Never removes *library* itself. If *library* is None, stops at the parent
	of *start* (i.e. removes at most one level — the most conservative cleanup
	that still catches a newly-orphaned author folder).
	"""
	try:
		start = Path(start).resolve()
	except OSError:
		return
	stop: Path | None = None
	if library is not None:
		try:
			stop = Path(library).resolve()
		except OSError:
			stop = None
	cur = start
	while True:
		if stop is not None and cur == stop:
			break
		try:
			if cur == cur.parent:  # filesystem root
				break
			next(cur.iterdir())  # raises StopIteration if empty
			return  # not empty — stop
		except StopIteration:
			pass
		except OSError:
			return
		try:
			cur.rmdir()
		except OSError:
			return  # not empty or not removable — stop quietly
		if stop is not None and cur == stop:
			break
		cur = cur.parent


def compute_needfix_path(meta: BookMeta, library: Path, needfix_dir: str) -> Path:
	"""Compute the destination path for a broken book under needfix/.

	Preserves the relative subpath under the library root:
		<lib>/<author>/<title> (<id>)/  ->  <lib>/<needfix>/<author>/<title> (<id>)/

	If the book is already under needfix/ (re-diagnosed in a later run), the
	existing prefix is stripped first to avoid a double needfix/needfix/ path.

	Anonym author folders ("Neznamy", "neznámý - neuveden", "Unknown", …) are
	collapsed to the canonical ANONYM_AUTHOR_NAME so all anonym books live
	under a single needfix/<Anonym>/ tree regardless of the spelling variant
	in their metadata.
	"""
	try:
		rel = Path(meta.path).relative_to(library)
	except ValueError:
		# meta.path is not under library (shouldn't happen, but be safe)
		rel = Path(meta.author_folder) / meta.title_folder
	else:
		# Strip an existing needfix/ prefix so re-running on books already in
		# needfix/ yields the same path rather than needfix/needfix/...
		parts = rel.parts
		if len(parts) > 1 and parts[0] == needfix_dir:
			rel = Path(*parts[1:])
	# Canonicalize the author folder: any anonym spelling -> "Anonym".
	parts = rel.parts
	if len(parts) > 1 and _is_anonym_spelling(parts[0]):
		rel = Path(ANONYM_AUTHOR_NAME, *parts[1:])
	return library / needfix_dir / rel


# ---------------------------------------------------------------------------
# Same-book detection + collision resolution (merge duplicates, disambiguate
# different books that collide under an {id}-less path pattern).
# ---------------------------------------------------------------------------

# Title/author fuzzy ratio at/above which two records are considered the same
# work (when ISBN is absent or agrees). Mirrors the verifier's strong band.
SAME_BOOK_FUZZY = 0.85

# Metadata sidecar files that are subsumed by the merged metadata (never moved
# as "format files" during a folder merge).
_SIDECAR_FILES = {"metadata.json", "metadata.opf"}


def same_book(a: BookMeta, b: BookMeta, *, fuzzy: float = SAME_BOOK_FUZZY) -> bool:
	"""Are *a* and *b* the same work (the same book, possibly the same edition)?

	Policy (cheap-first):
	  1. Both have a valid ISBN and it canonicalizes equal ⇒ SAME (same edition).
	  2. Title ≥ *fuzzy* AND best-author ≥ *fuzzy*:
	     - if both have a year AND the years differ ⇒ NOT SAME (different edition);
	     - otherwise ⇒ SAME.
	  3. Otherwise ⇒ NOT SAME.

	Embedded/format metadata is irrelevant here — both sides are the library's
	own (sidecar) metadata records, compared as-is.
	"""
	# 1. ISBN — strongest signal.
	if a.isbn and b.isbn:
		ca, cb = canonicalize(a.isbn), canonicalize(b.isbn)
		if ca and cb and ca == cb:
			return True
	# 2. Title + author fuzzy, with the different-edition tie-breaker.
	if a.title and b.title and _fuzzy_match(a.title, b.title) >= fuzzy:
		if _best_author(a.authors, b.authors) >= fuzzy:
			if a.year is not None and b.year is not None and a.year != b.year:
				return False  # same title+author, different edition
			return True
	return False


def _best_author(authors_a: list[str], authors_b: list[str]) -> float:
	"""Best fuzzy ratio over all author pairs (so "Karel Čapek" matches either
	of two co-authors). 0.0 when either side has no authors."""
	if not authors_a or not authors_b:
		return 0.0
	return max(_fuzzy_match(x, y) for x in authors_a for y in authors_b)


def _collision_clusters(metas: list[BookMeta]) -> list[list[BookMeta]]:
	"""Greedy partition of *metas* into same-book clusters.

	A book joins the first existing cluster whose base it is :func:`same_book`
	with; otherwise it starts a new cluster. Order is preserved.
	"""
	clusters: list[list[BookMeta]] = []
	for m in metas:
		for cl in clusters:
			if same_book(_pick_base(cl), m):
				cl.append(m)
				break
		else:
			clusters.append([m])
	return clusters


def _pick_base(metas: list[BookMeta]) -> BookMeta:
	"""Choose the cluster's base record: prefer one with a valid ISBN, then the
	lowest calibre_id. The base's id determines the merged folder's path."""
	with_isbn = [m for m in metas if m.isbn and canonicalize(m.isbn)]
	pool = with_isbn or list(metas)
	return min(pool, key=lambda m: (m.calibre_id if m.calibre_id is not None else float("inf")))


def _union(a: list[str], b: list[str]) -> list[str]:
	"""List union preserving order (a first), case-insensitive dedup."""
	out = list(a)
	seen = {x.lower() for x in a if x}
	for x in b:
		if x and x.lower() not in seen:
			out.append(x)
			seen.add(x.lower())
	return out


def merge_meta(base: BookMeta, other: BookMeta) -> BookMeta:
	"""Field-merge *other* into *base* (base wins on conflict).

	Scalars: keep base's value when present, else fill from other. Lists
	(authors/tags/genres): order-preserving union (base first). Series: union by
	name. ``calibre_id``/``uuid`` come from base. ``path`` is left unset (the
	caller sets it to the destination folder before writing).
	"""
	m = copy.copy(base)
	for attr in ("title", "subtitle", "isbn", "publisher", "language", "description"):
		if not getattr(m, attr):
			val = getattr(other, attr)
			if val:
				setattr(m, attr, val)
	if m.year is None and other.year is not None:
		m.year = other.year
	m.authors = _union(base.authors, other.authors)
	m.tags = _union(base.tags, other.tags)
	m.genres = _union(base.genres, other.genres)
	m.series = _union_series(base.series, other.series)
	m.calibre_id = base.calibre_id
	m.uuid = base.uuid
	# source/path left as base's; caller sets .path to the destination folder.
	return m


def _union_series(a: list[dict], b: list[dict]) -> list[dict]:
	"""Union of two series lists by series name (base order first)."""
	out = list(a)
	seen = {(s.get("name") or "").lower() for s in a if isinstance(s, dict)}
	for s in b:
		if isinstance(s, dict):
			name = (s.get("name") or "").lower()
			if name and name not in seen:
				out.append(s)
				seen.add(name)
	return out


def _merge_format_files(
	loser_folder: Path, winner_folder: Path, loser_id: int | None, *, dry_run: bool,
) -> list[tuple[str, str]]:
	"""Move ebook + cover files from *loser_folder* into *winner_folder*.

	Returns ``[(loser_filename, outcome), ...]`` where outcome is the target
	filename (renamed on collision) or ``"<skipped:cover>"`` / ``"<skipped:identical>"``.
	Metadata sidecars are NOT moved (subsumed by the merged metadata). In dry-run
	nothing is moved; the planned outcomes are still returned.
	"""
	from .readers import EBOOK_EXTS

	loser_folder, winner_folder = Path(loser_folder), Path(winner_folder)
	moved: list[tuple[str, str]] = []
	for entry in sorted(loser_folder.iterdir(), key=lambda e: e.name):
		if not entry.is_file():
			continue
		lname = entry.name.lower()
		if lname in _SIDECAR_FILES or lname.endswith(".bak"):
			continue  # subsumed by merged metadata (and its backups)
		is_ebook = entry.suffix.lower() in EBOOK_EXTS
		is_cover = lname == "cover.jpg"
		if not (is_ebook or is_cover):
			continue
		target = winner_folder / entry.name
		if target.exists():
			if is_cover:
				# Winner already has a cover — keep it; drop the loser's.
				moved.append((entry.name, "<skipped:cover>"))
				continue
			if _files_identical(entry, target):
				moved.append((entry.name, "<skipped:identical>"))
				continue
			# Same-name, different content → rename with the loser's id.
			stem = entry.stem
			sfx = entry.suffix
			n = 1
			new_target = winner_folder / f"{stem} (id{loser_id}){sfx}"
			while new_target.exists():
				new_target = winner_folder / f"{stem} (id{loser_id}) {n}{sfx}"
				n += 1
			target = new_target
		if not dry_run:
			winner_folder.mkdir(parents=True, exist_ok=True)
			shutil.move(str(entry), str(target))
		moved.append((entry.name, target.name))
	return moved


def _files_identical(a: Path, b: Path) -> bool:
	"""True if *a* and *b* have identical bytes (size then content)."""
	try:
		if a.stat().st_size != b.stat().st_size:
			return False
		return a.read_bytes() == b.read_bytes()
	except OSError:
		return False


def merge_folders(
	winner_folder: str | Path,
	winner_meta: BookMeta,
	loser_meta: BookMeta,
	*,
	dry_run: bool = True,
	cache: "Cache | None" = None,
	library: Path | None = None,
) -> MoveResult:
	"""Merge *loser_meta*'s folder into *winner_folder* (a single book result).

	Moves the loser's ebook/cover files into the winner folder, writes the
	field-merged metadata to the winner (via :func:`writers.write_book_meta`),
	and removes the (now empty) loser folder. The loser's metadata sidecars are
	dropped — the merged metadata subsumes them.

	*winner_meta* provides the metadata baseline (the cluster's base); its
	values are field-merged with *loser_meta*. Returns a :class:`MoveResult`
	with ``action='merged'`` and the per-file moves in ``details``.
	"""
	from .writers import write_book_meta

	winner_folder = Path(winner_folder)
	loser_folder = Path(loser_meta.path)
	merged = merge_meta(winner_meta, loser_meta)
	merged.path = str(winner_folder)
	moves = _merge_format_files(loser_folder, winner_folder, loser_meta.calibre_id, dry_run=dry_run)
	if not dry_run:
		winner_folder.mkdir(parents=True, exist_ok=True)
		write_book_meta(merged, dry_run=False, backup=True)
		shutil.rmtree(loser_folder, ignore_errors=True)
		_prune_empty_parents(loser_folder, library)
	return MoveResult(
		source=str(loser_folder),
		destination=str(winner_folder),
		action="merged",
		details=moves,
	)


def _disambiguated_dest(dest: str | Path, value: int | str | None, *, kind: str) -> Path | None:
	"""Append a disambiguation suffix to *dest*'s folder name.

	*kind* ``'year'`` → `` (2026)``; ``'id'`` → `` (id123)`` (the ``id`` prefix
	keeps an id-suffix visually distinct from a year-suffix). Returns None when
	*value* is empty or the value is already represented in the folder name
	(e.g. the pattern already produced ``Title (123)`` and two books share that
	id — id-disambiguation can't help, caller falls back to `` (dup N)``).
	"""
	if value in (None, ""):
		return None
	dest = Path(dest)
	name = dest.name
	sval = str(value)
	if kind == "year":
		if f"({sval})" in name:
			return None
		suffix = f" ({sval})"
	else:  # id
		if f"({sval})" in name or f"(id{sval})" in name:
			return None
		suffix = f" (id{sval})"
	return dest.with_name(name + suffix)


def _try_read_meta(folder: Path) -> BookMeta | None:
	"""Read a BookMeta from *folder* (its sidecar metadata). None if unreadable."""
	from .readers import read_book_folder

	try:
		return read_book_folder(folder)
	except Exception:  # noqa: BLE001
		return None


def _place_clusters(
	clusters: list[list[BookMeta]],
	dest: Path,
	*,
	anchor: BookMeta | None,
	library: Path,
	dry_run: bool,
	cache: "Cache | None",
) -> list[MoveResult]:
	"""Place same-book clusters at/around *dest*.

	*anchor*, when given, is a book already living at *dest* (a pre-existing
	occupant); it keeps the bare *dest* and is included when picking the
	disambiguator. With no anchor and a single cluster, that cluster takes the
	bare *dest* (the common "all same" merge). Otherwise every cluster is
	disambiguated — by year when all clusters (+anchor) have mutually-distinct
	years, else by id — with `` (dup N)`` as the last-resort fallback.
	"""
	results: list[MoveResult] = []
	reps = [_pick_base(cl) for cl in clusters]
	reps_all = ([anchor] if anchor else []) + reps

	if anchor is None and len(clusters) == 1:
		# Bare dest: move the base, merge the rest of the cluster into it.
		cl = clusters[0]
		base = reps[0]
		results.append(move_book(Path(base.path), dest, dry_run=dry_run, library=library))
		for m in cl:
			if m is base:
				continue
			results.append(merge_folders(dest, base, m, dry_run=dry_run, cache=cache, library=library))
		return results

	# Multiple clusters (or an anchor): disambiguate every cluster.
	use_year = (
		all(r.year is not None for r in reps_all)
		and len({r.year for r in reps_all}) == len(reps_all)
	)
	kind = "year" if use_year else "id"
	for cl, base in zip(clusters, reps, strict=True):
		value = base.year if kind == "year" else base.calibre_id
		d = _disambiguated_dest(dest, value, kind=kind)
		if d is None:
			# Can't disambiguate (e.g. duplicate id under an {id} pattern) → dup-N.
			results.append(move_book(Path(base.path), dest, dry_run=dry_run, library=library))
		else:
			results.append(move_book(Path(base.path), d, dry_run=dry_run, library=library))
			for m in cl:
				if m is base:
					continue
				results.append(merge_folders(d, base, m, dry_run=dry_run, cache=cache, library=library))
	return results


def _resolve_at_dest(
	claimants: list[BookMeta],
	dest: Path,
	library: Path,
	*,
	dry_run: bool,
	cache: "Cache | None",
) -> list[MoveResult]:
	"""Resolve where each *claimant* (batch books wanting *dest*) actually lands.

	Handles both batch-internal collisions (≥2 claimants) and a pre-existing
	occupant folder at *dest* (read and compared). Same-book claimants merge;
	different-book ones get disambiguated. The occupant, when present, keeps the
	bare *dest* (we never rename a folder we didn't move this run).
	"""
	occupant: BookMeta | None = None
	if dest.is_dir():
		claimant_paths: set[Path] = set()
		for m in claimants:
			try:
				claimant_paths.add(Path(m.path).resolve())
			except OSError:
				pass
		try:
			already = dest.resolve() in claimant_paths
		except OSError:
			already = False
		if not already:
			occupant = _try_read_meta(dest)

	clusters = _collision_clusters(claimants)
	if occupant is None:
		return _place_clusters(clusters, dest, anchor=None, library=library, dry_run=dry_run, cache=cache)

	# Occupant present: split clusters into same-as-occupant (merge into dest)
	# and different (disambiguate around dest).
	results: list[MoveResult] = []
	same: list[list[BookMeta]] = []
	other: list[list[BookMeta]] = []
	for cl in clusters:
		(same if same_book(_pick_base(cl), occupant) else other).append(cl)
	for cl in same:
		for m in cl:
			results.append(merge_folders(dest, occupant, m, dry_run=dry_run, cache=cache, library=library))
	if other:
		results += _place_clusters(other, dest, anchor=occupant, library=library, dry_run=dry_run, cache=cache)
	return results


def organize(
	books_with_verdicts: list[tuple[BookMeta, Verdict]],
	library: Path,
	*,
	path_pattern: str = DEFAULT_PATH_PATTERN,
	needfix_dir: str = DEFAULT_NEEDFIX_DIR,
	dry_run: bool = True,
	ok_verdicts: tuple[Verdict, ...] = (Verdict.OK, Verdict.VERIFIED),
	cache: "Cache | None" = None,
	progress_callback: Any = None,
) -> list[MoveResult]:
	"""Move OK books to pattern path and broken books to needfix/.

	Placement is driven by the current verdict, not by the book's location:
	  - OK/VERIFIED  -> <lib>/<pattern>/ (moved OUT of needfix/ if it was there)
	  - other         -> <lib>/<needfix_dir>/<rel path>

	A book already at its destination yields a MoveResult with action
	'already_correct' (see move_book). This is what lets a book fixed by
	`apply` move back out of needfix/ on the next organize run.

	*cache*: when given (and not a dry run), the source and destination paths
	of each actual move are invalidated so the next scan re-parses them rather
	than trusting a possibly-stale cached entry (the moved files keep their
	mtime across a rename/copy, and on NFS the attribute cache can mask the
	change for a while).

	*progress_callback*: if given, called as ``callback(done, total)`` after
	each book is processed, so the CLI can drive a progress bar with ETA.

	OK books that resolve to the same target path are de-duplicated: same-book
	duplicates are **merged** (one folder, all formats, field-merged metadata)
	and genuinely-different books are disambiguated — by year
	(``Title (2026)/``) when their years differ, else by id
	(``Title (id123)/``) — with `` (dup N)`` only as the last-resort fallback
	(e.g. duplicate calibre_ids under an ``{id}`` pattern). See
	:func:`same_book` and :func:`_resolve_at_dest`.
	"""
	library = Path(library)
	total = len(books_with_verdicts)
	results: list[MoveResult] = []
	done = 0
	cache_dirty = False

	def _record(result: MoveResult) -> None:
		"""Append a result and invalidate cache for real moves/merges (both ends)."""
		nonlocal cache_dirty
		results.append(result)
		if cache is not None and not dry_run and result.action in ("moved", "collision_renamed", "merged"):
			if result.source:
				cache.invalidate(result.source)
			if result.destination:
				cache.invalidate(result.destination)
			cache_dirty = True

	# Split OK vs broken. Broken books keep the simple move-to-needfix behaviour
	# (needfix paths are unique by relpath+id, so collisions there are left on
	# move_book's dup-N). OK books are grouped by target path and resolved
	# together so collisions (duplicates) can be merged/disambiguated.
	ok_books: list[BookMeta] = []
	for meta, verdict in books_with_verdicts:
		if verdict in ok_verdicts:
			ok_books.append(meta)
		else:
			dest = compute_needfix_path(meta, library, needfix_dir)
			_record(move_book(Path(meta.path), dest, dry_run=dry_run, library=library))
			done += 1
			if progress_callback is not None:
				progress_callback(done, total)

	# Group OK books by their computed target path.
	groups: dict[Path, list[BookMeta]] = {}
	for meta in ok_books:
		dest = compute_target_path(meta, path_pattern, library)
		groups.setdefault(dest, []).append(meta)

	for dest, metas in groups.items():
		for result in _resolve_at_dest(metas, dest, library, dry_run=dry_run, cache=cache):
			_record(result)
		done += len(metas)
		if progress_callback is not None:
			progress_callback(done, total)

	if cache is not None and not dry_run and cache_dirty:
		cache.commit()
	return results
