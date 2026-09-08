"""Content-based ebook file validity — the engine of ``bmf clean --files``.

SAFETY MODEL (agreed with the user, 2026-09-08): a file may be proposed for
deletion ONLY when its CONTENT is recognizable as no ebook format at all.
The probes answer "is this content SOME valid format?", never "does it match
its extension?" — a perfectly valid EPUB saved as ``.pdf`` is recognized as
epub content and is never deletable on our word. The extension enters only
the final fallthrough: "nothing recognized" means INVALID solely for
suffixes whose every valid content IS recognizable by the probes
(``_FULLY_PROBED_SUFFIXES``); for the rest (.prc/.pdb/.lit/… — formats with
variants bmf cannot positively identify) an unrecognized file stays UNKNOWN
and is never flagged.

Deliberate misses (safety over recall): files with an intact header but a
broken body (a truncated PDF still starts ``%PDF-``), text junk (an HTML
error page saved as .epub reads as text) — nothing recoverable is flagged.
A second, independent opinion vetoes everything: when calibre's
``ebook-meta`` reads the file successfully, it is NOT invalid regardless of
what the probes said.

The proposals this module writes into review.yaml are always review-gated
(``action: delete`` pre-filled on FRESH entries only) and ``bmf apply``
re-checks every file immediately before deleting it — a proposal can never
delete a file that is healthy at apply time.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .readers import EBOOK_EXTS

# Annotation sidecars are the reading device's data, never a book — probing
# them risks flagging binary records no content probe recognizes.
SKIP_SUFFIXES = frozenset({".mbp"})

# Suffixes whose VALID contents are always recognizable by the probes below.
# Only for these does "nothing recognized" imply invalid; elsewhere it means
# unknown (a format variant bmf cannot positively identify is not garbage).
_FULLY_PROBED_SUFFIXES = frozenset({".epub", ".cbz", ".pdf", ".mobi", ".azw3", ".cbr", ".txt"})

# Recognized content kinds -> suffixes that are "right" for them (drives the
# informational wrong-extension note; never an action).
_EXT_FOR_KIND = {
	"epub": {".epub"},
	"cbz": {".cbz"},
	"pdf": {".pdf"},
	"mobi": {".mobi", ".azw3", ".azw", ".prc"},
	"cbr": {".cbr"},
	"palm": {".pdb", ".prc"},
	"text": {".txt"},
	"doc": {".doc", ".rtf"},
	"lit": {".lit"},
	"djvu": {".djvu"},
	"cb7": {".cb7"},
}

_HEAD = 4096  # header window: covers the %PDF 1KB allowance and offset 60


def file_content_kind(path: str | Path) -> str:
	"""Identify the file's CONTENT, ignoring its extension.

	Returns one of: ``epub``/``cbz``/``pdf``/``mobi``/``cbr``/``palm``/
	``text``/``doc``/``lit``/``djvu``/``cb7`` (recognized — never deletable),
	``invalid`` (0 bytes, or binary noise under a fully-probed suffix), or
	``unknown`` (I/O error, or unrecognized content under a suffix whose
	valid variants bmf cannot positively identify).
	"""
	p = Path(path)
	try:
		size = p.stat().st_size
		with open(p, "rb") as fh:
			head = fh.read(_HEAD)
	except OSError:
		return "unknown"
	if not head:
		return "invalid"  # 0 bytes: nothing recoverable, universally safe

	# Mobipocket family (MOBI/KF8/PalmDOC-with-MOBI): type/creator at 60..68.
	if head[60:68] == b"BOOKMOBI":
		return "mobi"
	# PDF: the spec allows the header within the first 1024 bytes.
	if b"%PDF-" in head[:1024]:
		return "pdf"
	# RAR archive (CBR).
	if head.startswith(b"Rar!\x1a\x07"):
		return "cbr"
	# MS Composite Document (legacy .doc, some .lit) — a container catdoc and
	# Word read fine; recognized even though bmf has no deep validator.
	if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
		return "doc"
	if head.startswith(b"ITOLITLS"):
		return "lit"
	if head.startswith(b"AT&TFORM"):
		return "djvu"
	if head[:6] == b"7z\xbc\xaf\x27\x1c":
		return "cb7"
	# ZIP family: EPUB (container.xml), CBZ (images), or a zip of readable
	# text entries. A readable zip holding NONE of those is a strict-tier
	# invalid: an archive, but no book content anywhere in it.
	if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
		try:
			with zipfile.ZipFile(p) as zf:
				names = zf.namelist()
				if "META-INF/container.xml" in names:
					return "epub"
				lower = [n.lower() for n in names]
				if any(n.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")) for n in lower):
					return "cbz"
				if _zip_text_entry(zf, lower):
					return "text"
		except Exception:  # noqa: BLE001 - BadZipFile et al.: truncated archive
			pass  # fall through: nothing else can match binary zip data
		else:
			return "invalid"  # readable zip, zero book content
	# PalmDB structural recognition (PalmDOC/Plucker/…): the record list must
	# fit the file and point inside it. Type-agnostic on purpose — exotic
	# creators are still valid books.
	if _looks_like_palmdb(head, size):
		return "palm"
	if _looks_like_text(head):
		return "text"
	if p.suffix.lower() in _FULLY_PROBED_SUFFIXES:
		return "invalid"
	return "unknown"


def _zip_text_entry(zf: zipfile.ZipFile, lower_names: list[str]) -> bool:
	"""Does the zip hold a readable text-ish member (html/txt/opf/…)?"""
	for name, lname in zip(zf.namelist(), lower_names, strict=True):
		if not lname.endswith((".html", ".xhtml", ".htm", ".txt", ".xml", ".opf", ".ncx")):
			continue
		try:
			data = zf.read(name)[:65536]
		except Exception:  # noqa: BLE001 - unreadable member: keep looking
			continue
		if _looks_like_text(data):
			return True
	return False


def _looks_like_palmdb(head: bytes, size: int) -> bool:
	"""Structural PalmDB check: numRecords at 76, record-info list of 8-byte
	entries right after the 78-byte header, offsets inside the file."""
	if size < 86 or len(head) < 78:
		return False
	num_records = int.from_bytes(head[76:78], "big")
	if not 1 <= num_records <= 65535:
		return False
	list_end = 78 + 8 * num_records
	if list_end > size:
		return False
	if int.from_bytes(head[78:82], "big") < list_end:
		return False  # first record starts inside the record list
	if list_end <= len(head):
		if int.from_bytes(head[list_end - 8 : list_end - 4], "big") > size:
			return False  # last record points past EOF
	return True


def _looks_like_text(data: bytes) -> bool:
	"""Heuristic text recognition, biased towards NOT-flagging (safety):
	NUL bytes or too few printable ASCII characters mean binary; otherwise a
	handful of ASCII letters is enough (UTF-8 and single-byte Czech text both
	carry plenty of ASCII letters; diacritics ride along as high bytes)."""
	if not data or b"\x00" in data:
		return False
	printable = sum(1 for b in data if 32 <= b <= 126 or b in (9, 10, 13))
	if printable * 10 < len(data) * 6:  # < 60% printable → binary payload
		return False
	letters = sum(1 for b in data if 65 <= b <= 90 or 97 <= b <= 122)
	return letters >= 4


def calibre_reads_file(path: str | Path) -> bool:
	"""Would calibre's ``ebook-meta`` read this file CLEANLY? The independent
	second opinion: a clean read vetoes an invalid verdict. False when calibre
	is not installed (no veto, probes decide alone).

	Measured 2026-09-08 on calibre 7.x: the exit code alone is worthless —
	ebook-meta exits 0 even for binary garbage, printing a Python traceback
	to stderr and falling back to filename-derived metadata ("Title:
	garbage / Author(s): Neznámý"). The clean-read signature is RC 0 AND an
	EMPTY stderr (a valid EPUB/PDB reads with zero stderr bytes).
	"""
	ebook_meta = shutil.which("ebook-meta")
	if not ebook_meta:
		return False
	try:
		proc = subprocess.run([ebook_meta, str(path)], capture_output=True, timeout=20)
	except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
		return False
	return proc.returncode == 0 and not proc.stderr.strip()


def file_is_invalid(path: str | Path) -> bool:
	"""Should this file be PROPOSED for deletion? Recognized content -> no;
	unrecognized -> only for fully-probed suffixes, and never when calibre
	reads it (the veto)."""
	if file_content_kind(path) != "invalid":
		return False
	return not calibre_reads_file(path)


def extension_note(path: str | Path, kind: str) -> str | None:
	"""Informational wrong-extension note (never an action): recognized
	content kind under a mismatching ebook suffix."""
	expected = _EXT_FOR_KIND.get(kind)
	suffix = Path(path).suffix.lower()
	if not expected or suffix in expected:
		return None
	return f"obsah typu {kind} pod příponou {suffix or '(bez přípony)'}"


@dataclass
class FileFinding:
	"""One book folder with invalid ebook files (the review proposal unit)."""

	path: Path
	uuid: str | None
	files: list[str] = field(default_factory=list)  # bare file names
	reason: str = ""


def scan_invalid_files(books, library_root: Path | None = None) -> tuple[list[FileFinding], list[str]]:
	"""Probe every ebook-ext file of every book folder.

	Returns ``(findings, notes)``: findings are folders with at least one
	invalid file (see :func:`file_is_invalid`); notes are informational
	wrong-extension lines for RECOGNIZED content (no action attached).
	"""
	findings: list[FileFinding] = []
	notes: list[str] = []
	for meta in books:
		folder = Path(meta.path)
		try:
			entries = sorted(folder.iterdir())
		except OSError:
			continue
		bad: FileFinding | None = None
		for entry in entries:
			suffix = entry.suffix.lower()
			if suffix not in EBOOK_EXTS or suffix in SKIP_SUFFIXES:
				continue
			if not entry.is_file():
				continue
			kind = file_content_kind(entry)
			if kind in ("invalid", "unknown"):
				if file_is_invalid(entry):
					if bad is None:
						bad = FileFinding(folder, meta.uuid)
					bad.files.append(entry.name)
			elif (note := extension_note(entry, kind)) is not None:
				rel = folder.name if library_root is None else str(folder.relative_to(library_root))
				notes.append(f"{rel}/{entry.name}: {note}")
		if bad is not None:
			bad.reason = (
				"obsah souboru není rozpoznatelný jako žádný známý formát e-knihy"
				f" ({', '.join(bad.files)})"
			)
			findings.append(bad)
	return findings, notes


def merge_file_deletions(
	path: str | Path,
	findings: list[FileFinding],
	books,
	library_root: Path | None = None,
) -> dict[str, int]:
	"""Merge invalid-file delete proposals into review.yaml (in place, atomic).

	Same contract as :func:`book_meta_fix.review.merge_normalizations`: a
	PENDING entry gets ``proposed.delete_files`` overlaid and the C17
	diagnosis appended; a DECIDED entry is never touched; a book without an
	entry gets a fresh one with ``action: delete`` pre-filled (the user's
	bulk-veto workflow — the GUI filters by the delete state and mass-clears
	what should survive). Returns ``{added, updated, skipped_decided}``.
	"""
	from .review import _build_current, _header, _load_raw_entries, _relative_path, _render_entry

	p = Path(path)
	entries = _load_raw_entries(p) if p.is_file() else []
	by_uuid = {e.get("uuid"): e for e in entries if e.get("uuid")}
	metas = {b.uuid: b for b in books if b.uuid}
	added = updated = skipped = 0
	for finding in findings:
		meta = metas.get(finding.uuid)
		if meta is None:
			continue
		diag = {"category": "C17", "reason": finding.reason, "confidence": "HIGH"}
		existing = by_uuid.get(finding.uuid)
		if existing is not None:
			if existing.get("action") is not None:
				skipped += 1
				continue
			proposed = dict(existing.get("proposed") or {})
			files = sorted(set(proposed.get("delete_files") or []) | set(finding.files))
			proposed["delete_files"] = files
			existing["proposed"] = proposed
			diags = existing.get("diagnoses") or ([existing["diagnosis"]] if existing.get("diagnosis") else [])
			if not any(d.get("category") == "C17" for d in diags):
				diags.append(diag)
			if diags:
				existing["diagnoses"] = diags
			updated += 1
		else:
			entry = {
				"id": meta.calibre_id,
				"uuid": meta.uuid,
				"path": _relative_path(meta, library_root),
				"diagnosis": diag,
				"current": _build_current(meta),
				"proposed": {"delete_files": sorted(finding.files), "source": "filecheck"},
				"action": "delete",
			}
			entries.append(entry)
			by_uuid[meta.uuid] = entry
			added += 1
	if not (added or updated):
		return {"added": 0, "updated": 0, "skipped_decided": skipped}
	header = _header(len(entries))
	body = "\n".join(_render_entry(e) for e in entries)
	if body:
		body += "\n"
	tmp = p.with_suffix(p.suffix + ".tmp")
	tmp.write_text(header + body, encoding="utf-8")
	os.replace(tmp, p)
	return {"added": added, "updated": updated, "skipped_decided": skipped}
