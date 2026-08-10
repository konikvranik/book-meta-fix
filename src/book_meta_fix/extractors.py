"""Content extractors — read embedded metadata + ISBN from book files.

For each format we extract two things:
1. **Embedded metadata** — title/author/isbn declared inside the file
   (EPUB's content.opf, PDF's Info dictionary, etc.). This is the most
   reliable source when present.
2. **ISBN from text** — ISBN scanned from the first few pages (copyright page),
   for cases where embedded metadata is missing or wrong.

Format strategy:
	EPUB  -> zipfile + lxml on content.opf            (best, ~75% of library)
	PDF   -> pdfinfo (metadata) + pdftotext (ISBN)     (poppler, reliable)
	PDB   -> ebook-meta subprocess (fallback only)     (PalmDOC, opaque)
	MOBI  -> ebook-meta subprocess                     (Amazon, opaque)
	TXT   -> first/last lines + filename               (no embedded metadata)
	DOC   -> ebook-meta or filename                    (binary, hard to parse)
	other -> ebook-meta if available, else filename only

The dispatch function `extract()` picks the right extractor by file extension.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from .isbn import extract_isbn

log = logging.getLogger(__name__)

# EPUB OPF namespaces (same as readers.py)
NS = {
	"opf": "http://www.idpf.org/2007/opf",
	"dc": "http://purl.org/dc/elements/1.1/",
}


@dataclass
class ExtractedMeta:
	"""Metadata extracted from a book file's content (not from sidecar OPF/json).

	Any field may be None if the format doesn't carry it. `source_format`
	records which file format these came from.

	The ``title`` / ``authors`` / ``isbn`` / ``publisher`` / ``language``
	fields are sourced from the EMBEDDED metadata block (EPUB content.opf,
	pdfinfo, ebook-meta) — which calibre may have corrupted. The
	``*_from_text`` fields are mined from the book's actual page text by
	``text_meta.extract_metadata_from_text`` and are independent of the
	embedded block, so callers can prefer them when the embedded values look
	broken (see pipeline._is_better).
	"""

	title: str | None = None
	authors: list[str] = field(default_factory=list)
	isbn: str | None = None  # canonicalized, validated, from embedded metadata
	publisher: str | None = None
	language: str | None = None
	# ISBN found by scanning the first N pages of text (not from embedded metadata)
	isbn_from_text: str | None = None
	# First-page text sample (for fuzzy title/author matching in verifier)
	first_page_text: str | None = None
	# Metadata mined from first_page_text by text_meta (independent of the
	# embedded OPF block, which calibre may have overwritten).
	title_from_text: str | None = None
	authors_from_text: list[str] = field(default_factory=list)
	publisher_from_text: str | None = None
	year_from_text: int | None = None
	# Which extractor produced this
	source_format: str = ""
	# Any error encountered during extraction
	error: str | None = None


# ---------------------------------------------------------------------------
# EPUB extractor
# ---------------------------------------------------------------------------


def extract_epub(path: str | Path) -> ExtractedMeta:
	"""Extract metadata from an EPUB file via its content.opf.

	EPUB is a ZIP; META-INF/container.xml points to the OPF, which holds
	dc:title / dc:creator / dc:identifier (ISBN).
	"""
	result = ExtractedMeta(source_format="epub")
	try:
		with zipfile.ZipFile(path, "r") as zf:
			# 1. Find the OPF path via container.xml
			try:
				container = etree.fromstring(zf.read("META-INF/container.xml"))
			except KeyError:
				result.error = "no META-INF/container.xml"
				return result
			ns_container = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
			rootfile = container.find(".//c:rootfile", ns_container)
			if rootfile is None:
				result.error = "no rootfile in container.xml"
				return result
			opf_path = rootfile.get("full-path")
			if not opf_path:
				result.error = "empty full-path"
				return result

			# 2. Parse the OPF
			try:
				opf = etree.fromstring(zf.read(opf_path))
			except KeyError:
				result.error = f"opf not found at {opf_path}"
				return result
			md = opf.find("{http://www.idpf.org/2007/opf}metadata")
			if md is None:
				# Try with explicit ns map (some EPUBs declare differently)
				md = opf.find("opf:metadata", NS)
			if md is None:
				result.error = "no <metadata> in opf"
				return result

			# Title
			title_el = md.find("dc:title", NS)
			if title_el is not None and title_el.text:
				result.title = title_el.text.strip()

			# Authors
			for creator in md.findall("dc:creator", NS):
				if creator.text and creator.text.strip():
					result.authors.append(creator.text.strip())

			# ISBN
			for ident in md.findall("dc:identifier", NS):
				scheme = (ident.get("{http://www.idpf.org/2007/opf}scheme") or "").upper()
				text = (ident.text or "").strip()
				if scheme == "ISBN" or _looks_like_isbn(text):
					canon = _canonicalize_or_none(text)
					if canon:
						result.isbn = canon
						break
				# Also accept raw urn:isbn:... values
				if text.lower().startswith("urn:isbn:"):
					canon = _canonicalize_or_none(text[9:])
					if canon:
						result.isbn = canon
						break

			# Publisher / language
			pub = md.find("dc:publisher", NS)
			if pub is not None and pub.text:
				result.publisher = pub.text.strip()
			lang = md.find("dc:language", NS)
			if lang is not None and lang.text:
				result.language = lang.text.strip()

			# 3. First-page text for ISBN-from-content + fuzzy matching +
			#    deterministic metadata extraction (text_meta). We ALWAYS scan
			#    the text for an ISBN (even when the embedded OPF carried one),
			#    because the embedded ISBN may be the wrong one calibre wrote
			#    back — the text-scan is independent and can flag the mismatch.
			result.first_page_text = _epub_first_page_text(zf, opf_path)
			if result.first_page_text:
				result.isbn_from_text = extract_isbn(result.first_page_text[:3000])
				# Mine title/authors/publisher/year from the page text. These are
				# independent of the OPF block above and feed the pipeline's
				# deterministic fix stage.
				from .text_meta import extract_metadata_from_text

				tm = extract_metadata_from_text(result.first_page_text)
				result.title_from_text = tm.title
				result.authors_from_text = tm.authors
				result.publisher_from_text = tm.publisher
				result.year_from_text = tm.year
				# Prefer the text-mined ISBN when canonicalization differed.
				if tm.isbn and not result.isbn_from_text:
					result.isbn_from_text = tm.isbn
	except zipfile.BadZipFile:
		result.error = "bad zip / not an epub"
	except Exception as e:  # noqa: BLE001
		result.error = f"epub parse error: {e}"
		log.debug("epub extract failed for %s: %s", path, e)
	return result


def _epub_first_page_text(zf: zipfile.ZipFile, opf_path: str) -> str | None:
	"""Extract text from the first few reading-order chapters of an EPUB.

	We follow the OPF spine and concatenate the text of the first ~8 items
	(cover, title page, copyright page, dedication, first chapter...).
	Returns up to ~8000 chars. This is necessary because many EPUBs have the
	cover as an image-only HTML page (CSS only), so the *real* title appears
	only on the 2nd or 3rd spine item.
	"""
	try:
		opf_dir = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
		opf = etree.fromstring(zf.read(opf_path))
		manifest: dict[str, str] = {}
		for item in opf.iter("{http://www.idpf.org/2007/opf}item"):
			item_id = item.get("id")
			href = item.get("href")
			if item_id and href:
				manifest[item_id] = href
		# Spine order: collect text from up to 8 items
		chunks: list[str] = []
		seen = 0
		for itemref in opf.iter("{http://www.idpf.org/2007/opf}itemref"):
			if seen >= 8:
				break
			idref = itemref.get("idref")
			if not idref or idref not in manifest:
				continue
			href = manifest[idref]
			full = opf_dir + href if not href.startswith("/") else href[1:]
			try:
				raw = zf.read(full)
			except KeyError:
				continue
			text = _strip_html(raw)
			if text and len(text.strip()) > 5:
				chunks.append(text)
				seen += 1
		if not chunks:
			return None
		combined = " | ".join(chunks)
		return combined[:8000]
	except Exception:  # noqa: BLE001
		return None


def _strip_html(raw: bytes) -> str:
	"""Crude HTML tag stripper + encoding detection for EPUB content files.

	Preserves block structure: ``</p>``, ``</div>``, ``</h1>`` and ``<br>``
	become newlines so downstream per-line heuristics in text_meta (ALL-CAPS
	title detection, ``Název:`` label regexes) can anchor on line starts.
	Collapsing everything to one line (the previous behaviour) glued the
	title page together with the first paragraph, so the title run could
	never be isolated.
	"""
	# Detect encoding from XML declaration
	encoding = "utf-8"
	if raw[:5] == b"<?xml":
		m = re.search(rb'encoding=["\']([^"\']+)', raw[:80])
		if m:
			encoding = m.group(1).decode("ascii", errors="ignore")
	try:
		text = raw.decode(encoding, errors="replace")
	except (LookupError, UnicodeDecodeError):
		text = raw.decode("utf-8", errors="replace")
	# Insert newlines at block boundaries BEFORE removing tags, so the line
	# structure of the title page survives the tag strip.
	text = re.sub(
		r"</(p|div|h[1-6]|li|tr|td|th|section|article|header|footer|body|blockquote)\s*>",
		"\n", text, flags=re.IGNORECASE,
	)
	text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
	# Remove tags
	text = re.sub(r"<[^>]+>", " ", text)
	# Unescape basic entities
	text = (
		text.replace("&nbsp;", " ")
		.replace("&amp;", "&")
		.replace("&lt;", "<")
		.replace("&gt;", ">")
		.replace("&quot;", '"')
		.replace("&#8217;", "'")
	)
	# Collapse spaces/tabs within a line, but keep newlines.
	text = re.sub(r"[ \t]+", " ", text)
	# Collapse 3+ consecutive newlines to 2.
	text = re.sub(r"\n{3,}", "\n\n", text)
	return text.strip()


# ---------------------------------------------------------------------------
# PDF extractor
# ---------------------------------------------------------------------------


def extract_pdf(path: str | Path) -> ExtractedMeta:
	"""Extract metadata from PDF via pdfinfo + ISBN from first pages via pdftotext."""
	result = ExtractedMeta(source_format="pdf")
	path = str(path)

	# 1. pdfinfo for embedded metadata (Title/Author/Subject)
	pdfinfo = shutil.which("pdfinfo")
	if pdfinfo:
		try:
			proc = subprocess.run(
				[pdfinfo, path], capture_output=True, text=True, timeout=10
			)
			if proc.returncode == 0:
				for line in proc.stdout.splitlines():
					if ":" not in line:
						continue
					key, _, val = line.partition(":")
					val = val.strip()
					if not val:
						continue
					k = key.strip().lower()
					if k == "title":
						result.title = val
					elif k == "author":
						# May contain multiple authors separated by ; or ,
						result.authors = [a.strip() for a in re.split(r"[;,]", val) if a.strip()]
					elif k == "isbn":
						canon = _canonicalize_or_none(val)
						if canon:
							result.isbn = canon
		except (subprocess.TimeoutExpired, FileNotFoundError) as e:
			log.debug("pdfinfo failed for %s: %s", path, e)

	# 2. pdftotext for first 3 pages (ISBN on copyright page + title verification)
	pdftotext = shutil.which("pdftotext")
	if pdftotext:
		try:
			proc = subprocess.run(
				[pdftotext, "-f", "1", "-l", "3", path, "-"],
				capture_output=True, text=True, timeout=15,
			)
			if proc.returncode == 0 and proc.stdout:
				text = proc.stdout[:5000]
				result.first_page_text = text
				# Always scan the text for an ISBN (independent of pdfinfo's
				# embedded value) and mine the other text-based fields.
				result.isbn_from_text = extract_isbn(text)
				from .text_meta import extract_metadata_from_text

				tm = extract_metadata_from_text(text)
				result.title_from_text = tm.title
				result.authors_from_text = tm.authors
				result.publisher_from_text = tm.publisher
				result.year_from_text = tm.year
				if tm.isbn and not result.isbn_from_text:
					result.isbn_from_text = tm.isbn
		except (subprocess.TimeoutExpired, FileNotFoundError) as e:
			log.debug("pdftotext failed for %s: %s", path, e)

	if not result.title and not result.authors and not result.isbn and not result.first_page_text:
		result.error = result.error or "no metadata extracted"
	return result


# ---------------------------------------------------------------------------
# ebook-meta fallback (PDB, MOBI, DOC, RTF, LIT, DJVU)
# ---------------------------------------------------------------------------


def extract_via_ebook_meta(path: str | Path) -> ExtractedMeta:
	"""Use calibre's `ebook-meta` for embedded metadata + `ebook-convert` for
	page text.

	This is the realistic option for binary/opaque formats (pdb, mobi, doc).
	`ebook-meta` returns the embedded title/author/publisher/isbn (which
	calibre may have corrupted, same as OPF), and `ebook-convert` renders the
	book to plain text so text_meta heuristics can mine a title/ISBN from the
	actual page content. Returns empty ExtractedMeta if calibre is not installed.
	"""
	result = ExtractedMeta(source_format="ebook-meta")
	ebook_meta = shutil.which("ebook-meta")
	if not ebook_meta:
		result.error = "ebook-meta (calibre) not installed"
		return result
	try:
		proc = subprocess.run(
			[ebook_meta, str(path)], capture_output=True, text=True, timeout=15
		)
		if proc.returncode != 0:
			result.error = f"ebook-meta exited {proc.returncode}"
			return result
		# Output is "Key           : Value" lines
		for line in proc.stdout.splitlines():
			if ":" not in line:
				continue
			key, _, val = line.partition(":")
			val = val.strip()
			if not val:
				continue
			k = key.strip().lower()
			if k == "title":
				result.title = val
			elif k == "author(s)":
				result.authors = [a.strip() for a in re.split(r"[&,;]", val) if a.strip()]
			elif k == "publisher":
				result.publisher = val
			elif k == "languages":
				result.language = val.split(",")[0].strip()
			elif k == "identifiers":
				# Format: "isbn:978..., google:ABC, ..."
				for ident in val.split(","):
					ident = ident.strip()
					if ident.lower().startswith("isbn:"):
						canon = _canonicalize_or_none(ident[5:])
						if canon:
							result.isbn = canon
							break
	except (subprocess.TimeoutExpired, FileNotFoundError) as e:
		result.error = f"ebook-meta error: {e}"
		return result

	# Render the book to plain text via `ebook-convert` so text_meta can mine
	# a title / ISBN / publisher from the page content (independent of the
	# embedded block, which for pdb/doc is often the filename). This is the
	# same role _epub_first_page_text plays for EPUBs. We keep only the first
	# ~8000 chars to bound runtime and memory.
	page_text = _ebook_convert_to_text(path)
	if page_text:
		result.first_page_text = page_text[:8000]
		result.isbn_from_text = extract_isbn(result.first_page_text[:3000])
		from .text_meta import extract_metadata_from_text

		tm = extract_metadata_from_text(result.first_page_text)
		result.title_from_text = tm.title
		result.authors_from_text = tm.authors
		result.publisher_from_text = tm.publisher
		result.year_from_text = tm.year
		if tm.isbn and not result.isbn_from_text:
			result.isbn_from_text = tm.isbn

	return result


def _ebook_convert_to_text(path: str | Path) -> str | None:
	"""Render a binary ebook (pdb/mobi/doc/...) to plain text via calibre's
	`ebook-convert`. Returns None on failure / timeout / missing calibre.

	Calibre writes the whole book; we keep only the first 8000 chars in the
	caller. Runs with a 30s timeout per book.
	"""
	ebook_convert = shutil.which("ebook-convert")
	if not ebook_convert:
		return None
	import tempfile

	with tempfile.TemporaryDirectory(prefix="bmf-conv-") as tmp:
		out = Path(tmp) / "out.txt"
		try:
			proc = subprocess.run(
				[ebook_convert, str(path), str(out)],
				capture_output=True, text=True, timeout=30,
			)
			if proc.returncode != 0 or not out.is_file():
				return None
			text = out.read_text(encoding="utf-8", errors="replace")
			return text or None
		except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
			return None


# ---------------------------------------------------------------------------
# TXT extractor (no embedded metadata — filename + content only)
# ---------------------------------------------------------------------------


def extract_txt(path: str | Path) -> ExtractedMeta:
	"""TXT files have no embedded metadata. Read first/last lines for ISBN + title hints."""
	result = ExtractedMeta(source_format="txt")
	try:
		# Try utf-8, then cp1250/iso-8859-2 for CZ content
		raw = Path(path).read_bytes()[:8000]
		text = None
		for enc in ("utf-8", "cp1250", "iso-8859-2"):
			try:
				text = raw.decode(enc)
				break
			except UnicodeDecodeError:
				continue
		if text is None:
			text = raw.decode("utf-8", errors="replace")
		result.first_page_text = text[:5000]
		result.isbn_from_text = extract_isbn(text[:5000])
	except OSError as e:
		result.error = f"txt read error: {e}"
	return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _looks_like_isbn(s: str) -> bool:
	"""Cheap check: does *s* look like an ISBN (10/13 digits + maybe X + hyphens)?"""
	clean = re.sub(r"[^0-9Xx]", "", s or "")
	return len(clean) in (10, 13) and clean[:-1].isdigit()


def _canonicalize_or_none(raw: str | None) -> str | None:
	from .isbn import canonicalize

	return canonicalize(raw)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

# Map file extension -> extractor function
_EXTRACTORS = {
	".epub": extract_epub,
	".pdf": extract_pdf,
	".txt": extract_txt,
	# Binary formats: only via ebook-meta if available
	".pdb": extract_via_ebook_meta,
	".mobi": extract_via_ebook_meta,
	".azw": extract_via_ebook_meta,
	".doc": extract_via_ebook_meta,
	".rtf": extract_via_ebook_meta,
	".lit": extract_via_ebook_meta,
	".djvu": extract_via_ebook_meta,
}


def extract(path: str | Path) -> ExtractedMeta:
	"""Extract content metadata from a book file. Dispatches by extension.

	If the format is unknown or the extractor fails, returns an ExtractedMeta
	with `error` set (never raises).
	"""
	path = Path(path)
	suffix = path.suffix.lower()
	extractor = _EXTRACTORS.get(suffix)
	if extractor is None:
		return ExtractedMeta(source_format=suffix.lstrip(".") or "unknown", error=f"no extractor for {suffix}")
	if not path.is_file():
		return ExtractedMeta(source_format=suffix.lstrip("."), error="file not found")
	return extractor(path)
