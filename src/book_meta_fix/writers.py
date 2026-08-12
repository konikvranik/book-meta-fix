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
import re
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


def _sanitize_xml_text(s: str | None) -> str | None:
	"""Strip XML-illegal control characters from a string field.

	lxml rejects control chars (other than \\t \\n \\r) when building OPF, and
	some LLM/online proposals carry them (mojibake from a first-page scan, a
	rogue NUL byte). We strip them here so a single bad field does not abort
	the whole apply run. Returns None unchanged; non-strings are stringified.
	"""
	if s is None:
		return None
	if not isinstance(s, str):
		s = str(s)
	# XML 1.0 allows \t \n \r and any char >= \x20 (plus \x85/\xa0 as XML 1.1).
	# Strip everything else.
	return re.sub(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd]", "", s)


def _render_json(meta: BookMeta) -> str:
	"""Render BookMeta back to the Audiobookshelf metadata.json schema."""
	data = {
		"tags": meta.tags,
		"chapters": [],
		"title": _sanitize_xml_text(meta.title),
		"subtitle": _sanitize_xml_text(meta.subtitle),
		"authors": [_sanitize_xml_text(a) for a in meta.authors] if meta.authors else [],
		"narrators": [],
		"series": meta.series if isinstance(meta.series, list) else [],
		"genres": meta.genres,
		"publishedYear": str(meta.year) if meta.year else None,
		"publishedDate": None,
		"publisher": _sanitize_xml_text(meta.publisher),
		"description": _sanitize_xml_text(meta.description),
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
	root = etree.Element(f"{{{NS_OPF}}}package", nsmap=nsmap, attrib={"unique-identifier": "uuid_id", "version": "2.0"})
	md = etree.SubElement(root, f"{{{NS_OPF}}}metadata")

	# Identifiers
	if meta.calibre_id is not None:
		etree.SubElement(
			md, f"{{{NS_DC}}}identifier",
			attrib={f"{{{NS_OPF}}}scheme": "calibre", "id": "calibre_id"},
		).text = str(meta.calibre_id)
	etree.SubElement(
		md, f"{{{NS_DC}}}identifier",
		attrib={f"{{{NS_OPF}}}scheme": "uuid", "id": "uuid_id"},
	).text = uuid
	if meta.isbn:
		etree.SubElement(
			md, f"{{{NS_DC}}}identifier",
			attrib={f"{{{NS_OPF}}}scheme": "ISBN"},
		).text = meta.isbn

	# Title
	if meta.title:
		etree.SubElement(md, f"{{{NS_DC}}}title").text = _sanitize_xml_text(meta.title)
		# title_sort (sort under first letter, ignore leading "the/a")
		etree.SubElement(
			md, f"{{{NS_OPF}}}meta",
			attrib={"name": "calibre:title_sort", "content": _sanitize_xml_text(meta.title) or ""},
		)

	# Authors (with file-as)
	for author in meta.authors:
		file_as = _sanitize_xml_text(_file_as(author)) or ""
		etree.SubElement(
			md, f"{{{NS_DC}}}creator",
			attrib={f"{{{NS_OPF}}}file-as": file_as, f"{{{NS_OPF}}}role": "aut"},
		).text = _sanitize_xml_text(author) or ""

	# Publisher / date / language
	if meta.publisher:
		etree.SubElement(md, f"{{{NS_DC}}}publisher").text = _sanitize_xml_text(meta.publisher)
	if meta.year:
		etree.SubElement(md, f"{{{NS_DC}}}date").text = f"{meta.year}-01-01T00:00:00+00:00"
	if meta.language:
		etree.SubElement(md, f"{{{NS_DC}}}language").text = _sanitize_xml_text(meta.language)
	if meta.description:
		etree.SubElement(md, f"{{{NS_DC}}}description").text = _sanitize_xml_text(meta.description)

	# Contributor (us)
	etree.SubElement(
		md, f"{{{NS_DC}}}contributor",
		attrib={f"{{{NS_OPF}}}file-as": "book-meta-fix", f"{{{NS_OPF}}}role": "bkp"},
	).text = "book-meta-fix"

	# Timestamp
	etree.SubElement(
		md, f"{{{NS_OPF}}}meta",
		attrib={"name": "calibre:timestamp", "content": now},
	)

	# Cover reference (if cover.jpg exists)
	cover_path = Path(meta.path) / "cover.jpg"
	if cover_path.is_file():
		guide = etree.SubElement(root, f"{{{NS_OPF}}}guide")
		etree.SubElement(
			guide, f"{{{NS_OPF}}}reference",
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
