"""EPUB generation from other formats.

For each OK book that lacks an .epub, generate one from the best available
source format. Uses calibre's `ebook-convert` if available (handles all
common formats with proper encoding detection), otherwise `pandoc` for
txt/doc/rtf/html. Encoding of plain-text sources is detected via
charset-normalizer + chardet voting with CZ/SK preference.

Source-format preference (richest metadata first):
	pdb -> mobi -> pdf -> doc -> rtf -> txt -> html
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import BookMeta

log = logging.getLogger(__name__)

# Format priority for picking the source (lower = preferred).
# We prefer formats that carry structure + metadata, fall back to plain text.
_FORMAT_PRIORITY = {
	".pdb": 1,  # PalmDOC: ebook-convert handles it well
	".prc": 2,  # Palm Resource: MOBI-like, well-handled
	".mobi": 3,
	".azw": 4,
	".azw3": 5,  # KF8: newer Kindle, good source
	".lit": 6,
	".pdf": 7,  # PDF→EPUB is lossy but works
	".doc": 8,
	".docx": 9,
	".rtf": 10,
	".html": 11,
	".htm": 11,
	".txt": 12,  # last resort — no structure, encoding-sensitive
}


@dataclass
class EpubGenResult:
	"""Result of EPUB generation for one book."""

	book_id: int | None
	source_format: str
	source_file: str
	output_file: str | None  # None if generation failed
	tool: str  # 'ebook-convert' | 'pandoc' | 'none'
	dry_run: bool
	error: str | None = None


def pick_source_file(meta: BookMeta) -> tuple[str, str] | None:
	"""Pick the best non-epub source file for EPUB generation.

	Returns (format_ext, abs_path) or None if no usable source exists.
	"""
	if not meta.formats:
		return None
	# Exclude epub (we want to generate one) and unusual formats
	candidates = [f for f in meta.formats if f in _FORMAT_PRIORITY]
	if not candidates:
		return None
	# Sort by priority
	candidates.sort(key=lambda ext: _FORMAT_PRIORITY.get(ext, 999))
	best_ext = candidates[0]
	# Find the actual file in the folder
	folder = Path(meta.path)
	for entry in folder.iterdir():
		if entry.is_file() and entry.suffix.lower() == best_ext:
			return (best_ext, str(entry))
	return None


def generate_epub(
	meta: BookMeta,
	*,
	dry_run: bool = True,
	output_name: str | None = None,
) -> EpubGenResult:
	"""Generate an EPUB for *meta* from its best source file.

	If an .epub already exists, returns with error='already_exists'.
	If no source format is available, returns with error='no_source'.
	"""
	folder = Path(meta.path)
	# Already has an epub?
	existing_epubs = list(folder.glob("*.epub"))
	if existing_epubs:
		return EpubGenResult(
			book_id=meta.calibre_id,
			source_format="-",
			source_file="-",
			output_file=str(existing_epubs[0]),
			tool="none",
			dry_run=dry_run,
			error="already_exists",
		)

	source = pick_source_file(meta)
	if source is None:
		return EpubGenResult(
			book_id=meta.calibre_id,
			source_format="-",
			source_file="-",
			output_file=None,
			tool="none",
			dry_run=dry_run,
			error="no_source",
		)
	source_ext, source_path = source

	# Determine output filename. Default: "<title> - <author>.epub"
	author = meta.authors[0] if meta.authors else "Unknown"
	title = meta.title or "Untitled"
	if output_name:
		out_name = output_name
	else:
		from .mover import sanitize_segment

		out_name = f"{sanitize_segment(title)} - {sanitize_segment(author)}.epub"
	out_path = folder / out_name

	if dry_run:
		return EpubGenResult(
			book_id=meta.calibre_id,
			source_format=source_ext,
			source_file=source_path,
			output_file=str(out_path),
			tool="(dry-run)",
			dry_run=True,
		)

	# Try ebook-convert first (handles everything, knows encoding)
	tool, err = _try_ebook_convert(source_path, str(out_path), meta)
	if tool is not None:
		return EpubGenResult(
			book_id=meta.calibre_id,
			source_format=source_ext,
			source_file=source_path,
			output_file=str(out_path) if Path(out_path).exists() else None,
			tool=tool,
			dry_run=False,
			error=err,
		)

	# Fallback: pandoc (for txt/doc/rtf/html only)
	if source_ext in (".txt", ".doc", ".docx", ".rtf", ".html", ".htm"):
		tool, err = _try_pandoc(source_path, str(out_path), meta)
		if tool is not None:
			return EpubGenResult(
				book_id=meta.calibre_id,
				source_format=source_ext,
				source_file=source_path,
				output_file=str(out_path) if Path(out_path).exists() else None,
				tool=tool,
				dry_run=False,
				error=err,
			)

	return EpubGenResult(
		book_id=meta.calibre_id,
		source_format=source_ext,
		source_file=source_path,
		output_file=None,
		tool="none",
		dry_run=False,
		error=err or "no_converter_available",
	)


def _try_ebook_convert(source: str, dest: str, meta: BookMeta) -> tuple[str, str | None]:
	"""Try calibre's ebook-convert. Returns (tool_name, error_or_None)."""
	tool = shutil.which("ebook-convert")
	if tool is None:
		return ("none", "ebook-convert not found")
	# ebook-convert <input> <output> [--input-encoding=...] [--title ...] [--authors ...]
	cmd = [tool, source, dest]
	# Pass metadata so the generated EPUB has correct title/author
	if meta.title:
		cmd.extend(["--title", meta.title])
	if meta.authors:
		cmd.extend(["--authors", " & ".join(meta.authors)])
	if meta.language:
		cmd.extend(["--language", meta.language])
	# For plain text sources, detect encoding and pass it
	if source.lower().endswith(".txt"):
		enc = _detect_text_encoding(source)
		if enc:
			cmd.extend(["--input-encoding", enc])
	try:
		proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
		if proc.returncode == 0 and Path(dest).exists():
			return ("ebook-convert", None)
		err = f"ebook-convert exit {proc.returncode}: {proc.stderr[-300:]}" if proc.stderr else f"ebook-convert exit {proc.returncode}"
		log.debug("ebook-convert failed for %s: %s", source, err)
		return ("none", err)
	except subprocess.TimeoutExpired:
		return ("none", "ebook-convert timeout")
	except FileNotFoundError:
		return ("none", "ebook-convert not found")


def _try_pandoc(source: str, dest: str, meta: BookMeta) -> tuple[str, str | None]:
	"""Try pandoc as a fallback. Returns (tool_name, error_or_None)."""
	tool = shutil.which("pandoc")
	if tool is None:
		return ("none", "pandoc not found")
	cmd = [tool, source, "-o", dest, "--from", _pandoc_input_format(source)]
	# Metadata via --metadata flags
	if meta.title:
		cmd.extend(["--metadata", f"title={meta.title}"])
	if meta.authors:
		cmd.extend(["--metadata", f"author={'; '.join(meta.authors)}"])
	if meta.language:
		cmd.extend(["--metadata", f"lang={meta.language}"])
	try:
		proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
		if proc.returncode == 0 and Path(dest).exists():
			return ("pandoc", None)
		err = f"pandoc exit {proc.returncode}: {(proc.stderr or '')[-300:]}"
		return ("none", err)
	except subprocess.TimeoutExpired:
		return ("none", "pandoc timeout")
	except FileNotFoundError:
		return ("none", "pandoc not found")


def _pandoc_input_format(source: str) -> str:
	"""Map file extension to pandoc's --from format string."""
	ext = Path(source).suffix.lower()
	return {
		".txt": "markdown",  # pandoc autodetects; markdown is forgiving
		".html": "html",
		".htm": "html",
		".docx": "docx",
		".rtf": "rtf",
	}.get(ext, "markdown")


def _detect_text_encoding(path: str) -> str | None:
	"""Detect the encoding of a plain-text file via two-library voting.

	Uses charset-normalizer + chardet, with CZ/SK encoding preference on ties.
	Reads up to 64KB of the file (enough for reliable detection).
	"""
	try:
		raw = Path(path).read_bytes()[:65536]
	except OSError:
		return None
	votes: dict[str, float] = {}
	# charset-normalizer (better for CZ/SK)
	try:
		from charset_normalizer import from_bytes

		result = from_bytes(raw).best()
		if result is not None and result.encoding:
			votes[result.encoding.lower()] = votes.get(result.encoding.lower(), 0) + 2.0
	except Exception:  # noqa: BLE001
		pass
	# chardet
	try:
		import chardet

		r = chardet.detect(raw)
		if r and r.get("encoding"):
			enc = r["encoding"].lower()
			weight = r.get("confidence", 0.5)
			votes[enc] = votes.get(enc, 0) + weight
	except Exception:  # noqa: BLE001
		pass
	if not votes:
		return None
	# CZ/SK preference order
	for preferred in ("cp1250", "windows-1250", "iso-8859-2", "utf-8", "iso-8859-1"):
		# Normalize aliases
		norm = preferred.replace("windows-1250", "cp1250")
		for cand in votes:
			if cand.replace("_", "-") == norm or cand == preferred:
				return _canonical_enc_name(preferred)
	# Otherwise: highest vote
	best = max(votes, key=votes.get)
	return _canonical_enc_name(best)


def _canonical_enc_name(enc: str) -> str:
	"""Return a canonical encoding name that ebook-convert/pandoc accept."""
	enc = enc.lower().replace("_", "-")
	aliases = {
		"windows-1250": "cp1250",
		"latin-2": "iso-8859-2",
		"latin2": "iso-8859-2",
		"latin-1": "iso-8859-1",
		"latin1": "iso-8859-1",
		"utf8": "utf-8",
	}
	return aliases.get(enc, enc)
