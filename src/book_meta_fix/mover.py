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
"""
from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .models import BookMeta, Verdict

log = logging.getLogger(__name__)

DEFAULT_PATH_PATTERN = "{author}/{title} ({id})"
DEFAULT_NEEDFIX_DIR = "needfix"

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
	action: str  # 'moved' | 'already_correct' | 'collision_renamed' | 'error'
	error: str | None = None


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
	author = meta.authors[0] if meta.authors else "Anonym"
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
	return library / needfix_dir / rel


def organize(
	books_with_verdicts: list[tuple[BookMeta, Verdict]],
	library: Path,
	*,
	path_pattern: str = DEFAULT_PATH_PATTERN,
	needfix_dir: str = DEFAULT_NEEDFIX_DIR,
	dry_run: bool = True,
	ok_verdicts: tuple[Verdict, ...] = (Verdict.OK, Verdict.VERIFIED),
) -> list[MoveResult]:
	"""Move OK books to pattern path and broken books to needfix/.

	Placement is driven by the current verdict, not by the book's location:
	  - OK/VERIFIED  -> <lib>/<pattern>/ (moved OUT of needfix/ if it was there)
	  - other         -> <lib>/<needfix_dir>/<rel path>

	A book already at its destination yields a MoveResult with action
	'already_correct' (see move_book). This is what lets a book fixed by
	`apply` move back out of needfix/ on the next organize run.
	"""
	results: list[MoveResult] = []
	for meta, verdict in books_with_verdicts:
		if verdict in ok_verdicts:
			dest = compute_target_path(meta, path_pattern, library)
		else:
			dest = compute_needfix_path(meta, library, needfix_dir)
		result = move_book(Path(meta.path), dest, dry_run=dry_run, library=library)
		results.append(result)
	return results
