"""Atomic writers for metadata.json and metadata.opf.

Both files are written via a .tmp + os.replace() pattern for crash safety.
A .bak copy of the previous version is kept (configurable count).

	metadata.json  -> Audiobookshelf manifest (source of truth)
	metadata.opf   -> Calibre OPF 2.0 (kept for compatibility / Kavita)
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from lxml import etree

from .models import BookMeta

log = logging.getLogger(__name__)

NS_OPF = "http://www.idpf.org/2007/opf"
NS_DC = "http://purl.org/dc/elements/1.1/"


def write_book_meta(meta: BookMeta, *, dry_run: bool = True, backup: bool = True) -> dict:
	"""Write both metadata.json and metadata.opf for *meta*.

	Returns a dict describing what was written (or would be, if dry_run).
	"""
	folder = Path(meta.path)
	result = {"folder": str(folder), "dry_run": dry_run, "written": []}
	if not folder.is_dir():
		result["error"] = f"folder not found: {folder}"
		return result

	json_path = folder / "metadata.json"
	opf_path = folder / "metadata.opf"

	json_payload = _render_json(meta)
	opf_payload = _render_opf(meta)

	if dry_run:
		result["would_write"] = [str(json_path), str(opf_path)]
		result["json_preview"] = json_payload[:200]
		return result

	if backup:
		_backup(json_path)
		_backup(opf_path)

	_atomic_write(json_path, json_payload)
	_atomic_write(opf_path, opf_payload)
	result["written"] = [str(json_path), str(opf_path)]
	return result


# ---------------------------------------------------------------------------
# JSON renderer (Audiobookshelf manifest)
# ---------------------------------------------------------------------------


def _render_json(meta: BookMeta) -> str:
	"""Render BookMeta back to the Audiobookshelf metadata.json schema."""
	data = {
		"tags": meta.tags,
		"chapters": [],
		"title": meta.title,
		"subtitle": meta.subtitle,
		"authors": meta.authors,
		"narrators": [],
		"series": meta.series if isinstance(meta.series, list) else [],
		"genres": meta.genres,
		"publishedYear": str(meta.year) if meta.year else None,
		"publishedDate": None,
		"publisher": meta.publisher,
		"description": meta.description,
		"isbn": meta.isbn,
		"asin": None,
		"language": meta.language,
		"explicit": False,
		"abridged": False,
	}
	return json.dumps(data, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# OPF renderer (Calibre OPF 2.0)
# ---------------------------------------------------------------------------


def _render_opf(meta: BookMeta) -> str:
	"""Render BookMeta to an OPF 2.0 XML string."""
	# Preserve uuid if present, else mint one
	uuid = meta.uuid or str(uuid4())
	now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

	nsmap = {None: NS_OPF, "dc": NS_DC, "opf": NS_OPF}
	root = etree.Element("{%s}package" % NS_OPF, nsmap=nsmap, attrib={"unique-identifier": "uuid_id", "version": "2.0"})
	md = etree.SubElement(root, "{%s}metadata" % NS_OPF)

	# Identifiers
	if meta.calibre_id is not None:
		etree.SubElement(
			md, "{%s}identifier" % NS_DC,
			attrib={"{%s}scheme" % NS_OPF: "calibre", "id": "calibre_id"},
		).text = str(meta.calibre_id)
	etree.SubElement(
		md, "{%s}identifier" % NS_DC,
		attrib={"{%s}scheme" % NS_OPF: "uuid", "id": "uuid_id"},
	).text = uuid
	if meta.isbn:
		etree.SubElement(
			md, "{%s}identifier" % NS_DC,
			attrib={"{%s}scheme" % NS_OPF: "ISBN"},
		).text = meta.isbn

	# Title
	if meta.title:
		etree.SubElement(md, "{%s}title" % NS_DC).text = meta.title
		# title_sort (sort under first letter, ignore leading "the/a")
		etree.SubElement(
			md, "{%s}meta" % NS_OPF,
			attrib={"name": "calibre:title_sort", "content": meta.title},
		)

	# Authors (with file-as)
	for author in meta.authors:
		file_as = _file_as(author)
		etree.SubElement(
			md, "{%s}creator" % NS_DC,
			attrib={"{%s}file-as" % NS_OPF: file_as, "{%s}role" % NS_OPF: "aut"},
		).text = author

	# Publisher / date / language
	if meta.publisher:
		etree.SubElement(md, "{%s}publisher" % NS_DC).text = meta.publisher
	if meta.year:
		etree.SubElement(md, "{%s}date" % NS_DC).text = f"{meta.year}-01-01T00:00:00+00:00"
	if meta.language:
		etree.SubElement(md, "{%s}language" % NS_DC).text = meta.language
	if meta.description:
		etree.SubElement(md, "{%s}description" % NS_DC).text = meta.description

	# Contributor (us)
	etree.SubElement(
		md, "{%s}contributor" % NS_DC,
		attrib={"{%s}file-as" % NS_OPF: "book-meta-fix", "{%s}role" % NS_OPF: "bkp"},
	).text = "book-meta-fix"

	# Timestamp
	etree.SubElement(
		md, "{%s}meta" % NS_OPF,
		attrib={"name": "calibre:timestamp", "content": now},
	)

	# Cover reference (if cover.jpg exists)
	cover_path = Path(meta.path) / "cover.jpg"
	if cover_path.is_file():
		guide = etree.SubElement(root, "{%s}guide" % NS_OPF)
		etree.SubElement(
			guide, "{%s}reference" % NS_OPF,
			attrib={"type": "cover", "title": "Obálka", "href": "cover.jpg"},
		)

	xml = etree.tostring(root, xml_declaration=True, encoding="utf-8", pretty_print=True)
	return xml.decode("utf-8")


def _file_as(author: str) -> str:
	"""Generate a 'Lastname, Firstname' file-as form for sorting."""
	parts = author.strip().split()
	if len(parts) >= 2:
		return f"{parts[-1]}, {' '.join(parts[:-1])}"
	return author


# ---------------------------------------------------------------------------
# Atomic write + backup
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, content: str) -> None:
	"""Write content to path via .tmp + os.replace()."""
	tmp = path.with_suffix(path.suffix + ".tmp")
	tmp.write_text(content, encoding="utf-8")
	os.replace(tmp, path)


def _backup(path: Path) -> None:
	"""Copy path to path.bak (overwriting any previous backup)."""
	if path.is_file():
		bak = path.with_suffix(path.suffix + ".bak")
		shutil.copy2(path, bak)


# ---------------------------------------------------------------------------
# Library-wide snapshot (tar.gz of all metadata.json/opf before bulk apply)
# ---------------------------------------------------------------------------


def snapshot_metadata(library: Path, output: Path | None = None) -> Path:
	"""Create a tar.gz snapshot of all metadata.json/opf files in the library.

	This is the safety net for --auto-apply: if something goes wrong, you can
	restore the entire library's metadata from this single tarball. Only
	metadata files are included (not the ebooks themselves — those don't change).

	Returns the path to the created .tar.gz file.
	"""
	import tarfile
	from datetime import datetime

	library = Path(library)
	if output is None:
		stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
		output = Path(f"metadata_snapshot_{stamp}.tar.gz")
	output = Path(output)

	count = 0
	with tarfile.open(output, "w:gz") as tar:
		# Walk the library and add only metadata.json / metadata.opf / .bak
		for path in library.rglob("metadata.*"):
			if path.name in ("metadata.json", "metadata.opf") or path.name.endswith(".bak"):
				arcname = path.relative_to(library.parent) if library.parent in path.parents else path
				tar.add(path, arcname=str(arcname))
				count += 1
	log.info("snapshot: %d metadata files -> %s", count, output)
	return output
