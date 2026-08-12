"""Cross-format content consistency check.

For each book folder that holds two or more ebook formats (pdf, epub, txt, doc,
prc, pdb, mobi, azw3, ...), verify that every format is actually the **same
book** the folder's metadata declares, and quarantine (move into ``needfix/``)
any format file whose content is a *different* book. Each rogue gets its own
fully-isolated folder so two rogues from the same book are never merged (they
may be different wrong books).

Exposed via ``bmf crosscheck`` (dry-run by default; ``--apply`` moves).

Design notes
------------
* **Metadata is the anchor.** The folder's ``metadata.json``/``metadata.opf``
  title + ISBN is the reference of truth. Formats that corroborate it stay;
  rogues get quarantined. (Per the project's source-of-truth rule: the sidecar
  metadata files are authoritative, the file contents are checked against them.)

* **Only text-mined signals decide AGREES/DISAGREES.** Embedded EPUB/PDF
  metadata is uninformative here: Calibre wrote the DB metadata back into the
  file at import time, so embedded == DB by construction. We therefore decide
  solely from ``isbn_from_text``, ``title_from_text``, and ``meta.title``
  fuzzy-searched in ``first_page_text`` — all already produced by
  :func:`book_meta_fix.extractors.extract`. This is the same insight
  :mod:`book_meta_fix.verifier` relies on.

* **Quarantine path:** each rogue file moves into its own standalone folder

      <lib>/<needfix_dir>/crosscheck/<Author> - <Title> (<id>) - <filename>/<filename>

  The folder name encodes origin + filename, so it is self-describing and no
  two rogues ever share a directory. Collisions append `` (dup N)`` to the
  folder name — the same convention :func:`book_meta_fix.mover.move_book` uses
  (never merge, never overwrite, never fail).
"""
from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .extractors import ExtractedMeta, extract
from .isbn import canonicalize
from .models import BookMeta
from .mover import MoveResult, sanitize_segment
from .verifier import _title_in_text

if TYPE_CHECKING:
	from .library import Cache

log = logging.getLogger(__name__)

# Default fuzzy thresholds — mirror VerifierConfig (verify_fuzzy_strong/weak).
DEFAULT_STRONG = 0.8  # >= -> AGREES
DEFAULT_WEAK = 0.5  # <  -> DISAGREES (between weak and strong = UNCERTAIN)

# Sub-directory inside <needfix_dir> that holds crosscheck quarantines, kept
# separate from `organize` whole-folder moves so the two namespaces don't mix.
CROSSCHECK_SUBDIR = "crosscheck"

# Verdict labels produced by classify_format().
AGREES = "AGREES"
DISAGREES = "DISAGREES"
UNCERTAIN = "UNCERTAIN"

# Decision labels produced by crosscheck_book().
DECISION_CLEAN = "clean"  # no disagreement detected (nothing to move)
DECISION_QUARANTINE = "quarantine"  # >=1 AGREES + >=1 DISAGREES -> move rogues
DECISION_AMBIGUOUS = "ambiguous"  # DISAGREES but no AGREES -> metadata suspect, don't move
DECISION_SKIPPED = "skipped"  # fewer than 2 formats -> nothing to cross-check


@dataclass
class FormatVerdict:
	"""Classification of one format file's content vs the book's metadata."""

	file: str  # absolute path of the format file
	fmt: str  # '.epub', '.pdf', ...
	verdict: str  # AGREES | DISAGREES | UNCERTAIN
	reason: str  # human-readable, e.g. "ISBN … differs from metadata …"


@dataclass
class CrosscheckResult:
	"""Outcome of cross-checking one book folder's formats."""

	book_id: int | None
	path: str  # book folder
	origin: str  # raw "<Author> - <Title> (<id>)" label, for the quarantine path
	formats_checked: int
	verdicts: list[FormatVerdict]
	rogues: list[FormatVerdict]  # the DISAGREES files (moved only on quarantine)
	decision: str  # clean | quarantine | ambiguous | skipped
	moved: list[MoveResult] = field(default_factory=list)  # filled by quarantine()


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_format(
	meta: BookMeta,
	extracted: ExtractedMeta,
	*,
	strong: float = DEFAULT_STRONG,
	weak: float = DEFAULT_WEAK,
) -> tuple[str, str]:
	"""Classify one format's content against the book's metadata.

	Returns ``(verdict, reason)`` where verdict is AGREES / DISAGREES / UNCERTAIN.

	Only *text-mined* signals are used (``isbn_from_text``, ``title_from_text``,
	and ``meta.title`` fuzzy-searched in ``first_page_text``). The embedded
	metadata block is deliberately ignored — for formats Calibre could rewrite
	(EPUB, PDF) it equals the DB metadata by construction and is therefore
	uninformative as an independent check.

	Signal order (first that applies wins):
	  1. ISBN from text vs DB ISBN  — equal ⇒ AGREES, differ ⇒ DISAGREES (strong).
	  2. Title: text-mined title vs DB title (token_sort), or DB title searched
	     in the first-page text (partial). ≥ strong ⇒ AGREES, < weak ⇒ DISAGREES,
	     otherwise UNCERTAIN.
	  3. Nothing comparable ⇒ UNCERTAIN.
	"""
	# 1. ISBN — strongest signal, independent of the (possibly corrupt) embedded
	#    metadata block. meta.isbn is already canonical (readers), isbn_from_text
	#    too (extract_isbn), but canonicalize both to be safe.
	fmt_isbn = extracted.isbn_from_text
	if fmt_isbn and meta.isbn:
		c_fmt = canonicalize(fmt_isbn)
		c_db = canonicalize(meta.isbn)
		if c_fmt and c_db:
			if c_fmt == c_db:
				return AGREES, f"ISBN {c_fmt} matches metadata"
			return DISAGREES, f"ISBN {c_fmt} differs from metadata {c_db}"

	# 2. Title. Search the DB title inside the first-page text — the same method
	#    verifier.verify() uses (partial_ratio of the normalised title against the
	#    normalised text). This is more robust than comparing two *mined* titles:
	#    text_meta's mined title_from_text can pick up stray page noise (a stray
	#    letter from a <title> tag, an ALL-CAPS run), which drags the score toward
	#    the threshold. partial_ratio of the DB title against the raw page text is
	#    independent of that mining noise and is the project's canonical title
	#    check. (title_from_text is mined FROM first_page_text, so the page text
	#    is the superset signal.)
	score: float | None = None
	if extracted.first_page_text and meta.title:
		score = _title_in_text(meta.title, extracted.first_page_text)
	if score is not None:
		if score >= strong:
			return AGREES, f"title matches metadata (fuzzy {score:.2f})"
		if score < weak:
			return DISAGREES, f"title differs from metadata (fuzzy {score:.2f})"
		return UNCERTAIN, f"title partial match (fuzzy {score:.2f})"

	return UNCERTAIN, "no comparable text signal (no ISBN, no usable title text)"


def _ebook_files(meta: BookMeta) -> list[tuple[str, str]]:
	"""Return ``[(ext, abs_path), ...]`` for the ebook files in the book folder.

	Filters the folder contents by the extensions the scan already recognised
	(``meta.formats``), so this stays consistent with what was parsed. Files are
	returned in name-sorted order for determinism.
	"""
	folder = Path(meta.path)
	formats = set(meta.formats)
	files: list[tuple[str, str]] = []
	if folder.is_dir() and formats:
		for entry in sorted(folder.iterdir(), key=lambda e: e.name):
			if entry.is_file() and entry.suffix.lower() in formats:
				files.append((entry.suffix.lower(), str(entry)))
	return files


def _origin_label(meta: BookMeta) -> str:
	"""Raw (unsanitized) '<Author> - <Title> (<id>)' label for the quarantine path."""
	author = meta.authors[0] if meta.authors else "Anonym"
	title = meta.title or "Untitled"
	book_id = str(meta.calibre_id) if meta.calibre_id is not None else "noid"
	return f"{author} - {title} ({book_id})"


def crosscheck_book(
	meta: BookMeta,
	*,
	strong: float = DEFAULT_STRONG,
	weak: float = DEFAULT_WEAK,
	extract_fn: Callable[[str], ExtractedMeta] | None = None,
) -> CrosscheckResult:
	"""Cross-check every format file in *meta*'s folder against its metadata.

	Skips folders with fewer than two formats. See module docstring for the
	decision rules (clean / quarantine / ambiguous / skipped).

	*extract_fn* defaults to :func:`extractors.extract`; tests may pass a stub
	to avoid invoking external binaries (pdftotext, ebook-meta, ...).
	"""
	xtract = extract_fn or extract
	files = _ebook_files(meta)
	if len(files) < 2:
		return CrosscheckResult(
			book_id=meta.calibre_id,
			path=meta.path,
			origin=_origin_label(meta),
			formats_checked=len(files),
			verdicts=[],
			rogues=[],
			decision=DECISION_SKIPPED,
		)

	verdicts: list[FormatVerdict] = []
	for ext, path in files:
		try:
			extracted = xtract(path)
		except Exception as e:  # noqa: BLE001
			verdicts.append(FormatVerdict(file=path, fmt=ext, verdict=UNCERTAIN, reason=f"extraction failed: {e}"))
			continue
		v, reason = classify_format(meta, extracted, strong=strong, weak=weak)
		verdicts.append(FormatVerdict(file=path, fmt=ext, verdict=v, reason=reason))

	n_agree = sum(1 for v in verdicts if v.verdict == AGREES)
	n_disagree = sum(1 for v in verdicts if v.verdict == DISAGREES)

	if n_agree >= 1 and n_disagree >= 1:
		# Metadata is corroborated by the AGREES group → the DISAGREES files are
		# the rogues (a different book got mixed into this folder). These move.
		decision = DECISION_QUARANTINE
		rogues = [v for v in verdicts if v.verdict == DISAGREES]
	elif n_disagree >= 1 and n_agree == 0:
		# Nothing corroborates the metadata and at least one format disagrees —
		# the metadata itself may be wrong. Moving files here would risk
		# quarantining good content, so flag for manual review instead. The
		# disagreeing files stay listed in `verdicts` for the report; `rogues`
		# is empty because nothing gets moved.
		decision = DECISION_AMBIGUOUS
		rogues = []
	else:
		# No disagreement detected (all agree, or only UNCERTAIN). Nothing to move.
		decision = DECISION_CLEAN
		rogues = []

	return CrosscheckResult(
		book_id=meta.calibre_id,
		path=meta.path,
		origin=_origin_label(meta),
		formats_checked=len(files),
		verdicts=verdicts,
		rogues=rogues,
		decision=decision,
	)


# ---------------------------------------------------------------------------
# Quarantine (file-level move)
# ---------------------------------------------------------------------------


def rogue_destination(result: CrosscheckResult, rogue_file: str, library: Path, needfix_dir: str) -> Path:
	"""Compute the isolated destination *folder* for one rogue file.

	  <lib>/<needfix_dir>/crosscheck/<origin> - <filename>>

	The folder name encodes origin + filename so it is self-describing and no
	two rogues share a directory. ``sanitize_segment`` makes it FS-safe and
	truncates; should two rogues nonetheless canonicalize to the same name,
	:func:`move_file` appends `` (dup N)`` so they still land apart.
	"""
	filename = Path(rogue_file).name
	folder_name = sanitize_segment(f"{result.origin} - {filename}")
	return Path(library) / needfix_dir / CROSSCHECK_SUBDIR / folder_name


def move_file(
	src: str | Path,
	dest_folder: str | Path,
	*,
	dry_run: bool = True,
) -> MoveResult:
	"""Move a single file into *dest_folder* (created if needed).

	On collision (the destination folder already exists) the folder name gets a
	`` (dup N)`` suffix — mirroring :func:`mover.move_book`'s convention: never
	merge, never overwrite, never fail. Returns a :class:`MoveResult` whose
	``destination`` is the final file path.
	"""
	src = Path(src)
	final_folder = Path(dest_folder)
	action = "moved"
	if final_folder.exists():
		n = 1
		while True:
			candidate = final_folder.with_name(f"{final_folder.name} (dup {n})")
			if not candidate.exists():
				final_folder = candidate
				break
			n += 1
		action = "collision_renamed"

	dest_file = final_folder / src.name
	if dry_run:
		return MoveResult(str(src), str(dest_file), action)

	try:
		final_folder.mkdir(parents=True, exist_ok=True)
		shutil.move(str(src), str(dest_file))
	except (OSError, shutil.Error) as e:
		return MoveResult(str(src), str(dest_file), "error", str(e))
	return MoveResult(str(src), str(dest_file), action)


def quarantine(
	results: list[CrosscheckResult],
	library: str | Path,
	*,
	needfix_dir: str = "needfix",
	dry_run: bool = True,
	cache: Cache | None = None,
	progress_callback: Any = None,
) -> list[MoveResult]:
	"""Move every rogue file from the given results into its own isolated folder.

	Only results with ``decision == 'quarantine'`` carry rogues worth moving; we
	filter defensively anyway. The source book folder of every real move is
	cache-invalidated (its formats list changed → next scan must re-parse), and
	the cache is committed once at the end.

	*progress_callback*, if given, is called as ``callback(done, total)`` after
	each rogue file is processed, so the CLI can drive a progress bar.
	"""
	library = Path(library)
	rogues = [(r, rv) for r in results for rv in r.rogues]
	total = len(rogues)
	all_moves: list[MoveResult] = []
	touched_books: set[str] = set()

	for i, (result, rv) in enumerate(rogues, start=1):
		dest_folder = rogue_destination(result, rv.file, library, needfix_dir)
		move = move_file(rv.file, dest_folder, dry_run=dry_run)
		result.moved.append(move)
		all_moves.append(move)
		if not dry_run and move.action in ("moved", "collision_renamed"):
			touched_books.add(str(Path(result.path)))
		if progress_callback is not None:
			progress_callback(i, total)

	if cache is not None and not dry_run and touched_books:
		cache.invalidate_many(touched_books)
		cache.commit()
	return all_moves
