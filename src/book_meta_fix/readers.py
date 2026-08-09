"""Readers for book metadata sources.

Primary source is metadata.json (Audiobookshelf manifest — cleaner than OPF).
Fallback is metadata.opf (Calibre OPF 2.0).

Both produce a normalized BookMeta. The path itself is also a (weak) source,
parsed by parse_path().
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from lxml import etree

from .isbn import canonicalize
from .models import BookMeta

log = logging.getLogger(__name__)

# OPF 2.0 namespaces (uniform across the whole library)
NS = {
	"opf": "http://www.idpf.org/2007/opf",  # also the default namespace
	"dc": "http://purl.org/dc/elements/1.1/",
}

# Path pattern: <library>/<Author>/<Title> (<calibre_id>)/
_TITLE_ID_RE = re.compile(r"^(?P<title>.*)\s+\((?P<id>\d+)\)\s*$")

# Calibre NULL date marker
_NULL_DATE = "0101-01-01"


def read_book_folder(folder: Path) -> BookMeta:
	"""Read a single book folder, building a normalized BookMeta.

	Precedence within the file sources: metadata.json > metadata.opf.
	The path (author_folder / title_folder / calibre_id) is always populated.
	After loading, text fields are scanned for mojibake and repaired if possible
	(unrepairable fields are flagged for later content-based / LLM repair).
	"""
	from .encoding import repair  # local import to avoid cycle at module load

	meta = _parse_path(folder)
	# Populate formats and primary file regardless of metadata source
	_collect_formats(folder, meta)

	json_path = folder / "metadata.json"
	opf_path = folder / "metadata.opf"

	if json_path.is_file():
		try:
			_fill_from_json(json_path, meta)
			meta.source = "json"
		except Exception as e:  # noqa: BLE001
			log.warning("metadata.json parse failed for %s: %s; falling back to OPF", folder, e)
			_fill_from_opf(opf_path, meta)
			meta.source = "opf(fallback)"
	elif opf_path.is_file():
		_fill_from_opf(opf_path, meta)
		meta.source = "opf"
	else:
		log.warning("no metadata file in %s; using path only", folder)
		meta.source = "path"

	# Attempt mojibake repair on text fields. We repair in place and record
	# which fields couldn't be repaired (they'll need content lookup or LLM).
	_apply_encoding_repair(meta, repair)
	return meta


def _apply_encoding_repair(meta: BookMeta, repair_fn) -> None:  # noqa: ANN001
	"""Scan text fields for mojibake; repair what we can, flag the rest.

	Repaired fields set meta.encoding_repaired=True. Fields where mojibake was
	detected but the bytes couldn't be recovered are appended to
	encoding_unrepairable (so detectors can mark the book NEEDS_REVIEW).
	"""
	# Single-value text fields: (attribute_name, value)
	single_fields = [
		("title", meta.title),
		("publisher", meta.publisher),
	]
	for name, value in single_fields:
		if not value:
			continue
		fixed, _kind = repair_fn(value)
		if fixed is None:
			# mojibake detected but unrepairable
			if name not in meta.encoding_unrepairable:
				meta.encoding_unrepairable.append(name)
		elif fixed != value:
			setattr(meta, name, fixed)
			meta.encoding_repaired = True

	# Authors: list of strings
	new_authors: list[str] = []
	author_changed = False
	for a in meta.authors:
		if not a:
			continue
		fixed, _kind = repair_fn(a)
		if fixed is None:
			if "authors" not in meta.encoding_unrepairable:
				meta.encoding_unrepairable.append("authors")
			new_authors.append(a)  # keep original; review will handle it
		else:
			if fixed != a:
				author_changed = True
			new_authors.append(fixed)
	if author_changed:
		meta.authors = new_authors
		meta.encoding_repaired = True


def _parse_path(folder: Path) -> BookMeta:
	"""Parse <Author>/<Title> (<id>)/ into author_folder / title_folder / calibre_id."""
	meta = BookMeta(path=str(folder))
	# folder.name = "<Title> (<id>)" ; parent.name = "<Author>"
	meta.title_folder = folder.name
	meta.author_folder = folder.parent.name
	m = _TITLE_ID_RE.match(folder.name)
	if m:
		# Note: we do NOT copy the path-parsed title into meta.title here;
		# readers fill title from json/opf. calibre_id from folder is authoritative
		# only if the metadata sources don't provide it.
		try:
			meta.calibre_id = int(m.group("id"))
		except ValueError:
			pass
	return meta


def _collect_formats(folder: Path, meta: BookMeta) -> None:
	"""List book file extensions present and pick a primary file for extraction."""
	# Known ebook extensions, ordered by extraction preference (richest metadata first)
	pref = [".epub", ".pdf", ".mobi", ".azw", ".pdb", ".doc", ".rtf", ".txt", ".lit", ".djvu"]
	seen: list[str] = []
	for entry in folder.iterdir():
		if not entry.is_file():
			continue
		suffix = entry.suffix.lower()
		if suffix in pref:
			seen.append(suffix)
	# Sort by preference
	seen.sort(key=lambda s: pref.index(s) if s in pref else 999)
	meta.formats = seen
	if seen:
		# primary_file = the first preferred format actually present
		for entry in sorted(folder.iterdir(), key=lambda e: pref.index(e.suffix.lower()) if e.suffix.lower() in pref else 999):
			if entry.is_file() and entry.suffix.lower() in pref:
				meta.primary_file = str(entry)
				break


def _fill_from_json(path: Path, meta: BookMeta) -> None:
	"""Populate BookMeta from a metadata.json (Audiobookshelf manifest)."""
	data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

	authors = data.get("authors") or []
	# ABS often stores translators alongside real authors; we keep them all here
	# and let detectors/verifier separate translators later.
	meta.authors = [a for a in authors if a]
	meta.title = data.get("title") or ""
	meta.subtitle = data.get("subtitle")
	meta.publisher = data.get("publisher")
	meta.language = data.get("language")
	meta.description = data.get("description")
	meta.genres = data.get("genres") or []
	meta.tags = data.get("tags") or []

	# publishedYear is a clean 4-digit string (or null). Calibre's NULL marker
	# ("0101") sometimes leaks through; treat years < 1000 as unknown.
	year = data.get("publishedYear")
	if year:
		try:
			y = int(str(year)[:4])
			meta.year = y if y >= 1000 else None
		except ValueError:
			meta.year = None

	# ISBN: validate and canonicalize
	meta.isbn = canonicalize(data.get("isbn"))

	# Series: ABS stores as list of {name, sequence}; normalize
	series = data.get("series") or []
	if isinstance(series, list):
		meta.series = [s for s in series if s]


def _fill_from_opf(path: Path, meta: BookMeta) -> None:
	"""Populate BookMeta from a metadata.opf (Calibre OPF 2.0)."""
	if not path.is_file():
		return
	tree = etree.parse(str(path))
	root = tree.getroot()
	md = root.find("opf:metadata", NS)
	if md is None:
		return

	# Title
	title_el = md.find("dc:title", NS)
	if title_el is not None and title_el.text:
		meta.title = title_el.text.strip()

	# Authors (all dc:creator with role aut)
	authors: list[str] = []
	for creator in md.findall("dc:creator", NS):
		role = creator.get(f"{{{NS['opf']}}}role", "aut")
		if role == "aut" and creator.text:
			authors.append(creator.text.strip())
	# Deduplicate while preserving order
	seen: set[str] = set()
	meta.authors = [a for a in authors if not (a in seen or seen.add(a))]

	# Publisher
	pub = md.find("dc:publisher", NS)
	if pub is not None and pub.text:
		meta.publisher = pub.text.strip()

	# Language
	lang = md.find("dc:language", NS)
	if lang is not None and lang.text:
		meta.language = lang.text.strip()

	# Description (may contain HTML entities)
	desc = md.find("dc:description", NS)
	if desc is not None and desc.text:
		meta.description = desc.text.strip()

	# ISBN (pick first valid one; there may be multiple identifiers)
	for ident in md.findall("dc:identifier", NS):
		scheme = ident.get(f"{{{NS['opf']}}}scheme", "")
		if scheme.upper() == "ISBN" and ident.text:
			canon = canonicalize(ident.text)
			if canon:
				meta.isbn = canon
				break

	# Date: OPF <dc:date> is often a noisy ISO timestamp; extract year if plausible
	date_el = md.find("dc:date", NS)
	if date_el is not None and date_el.text:
		meta.year = _extract_year(date_el.text.strip())

	# Calibre id from identifier (fallback if path didn't have one)
	if meta.calibre_id is None:
		for ident in md.findall("dc:identifier", NS):
			if ident.get(f"{{{NS['opf']}}}scheme") == "calibre" and ident.text:
				try:
					meta.calibre_id = int(ident.text)
				except ValueError:
					pass
				break

	# Series: <meta name="calibre:series" content="..."/> + calibre:series_index
	series_name = None
	series_index = None
	for m in md.findall("opf:meta", NS):
		name = m.get("name", "")
		if name == "calibre:series":
			series_name = m.get("content")
		elif name == "calibre:series_index":
			series_index = m.get("content")
	if series_name:
		meta.series = [{"name": series_name, "index": series_index}]

	# Tags: <dc:subject>...</dc:subject>
	tags = [s.text.strip() for s in md.findall("dc:subject", NS) if s.text]
	meta.tags = tags


def _extract_year(date_str: str) -> int | None:
	"""Extract a plausible publication year from an OPF date string.

	'0101-01-01...' is Calibre's NULL marker -> None.
	Otherwise take the first 4-digit group.
	"""
	if not date_str:
		return None
	if date_str.startswith(_NULL_DATE):
		return None
	m = re.match(r"(\d{4})", date_str)
	if m:
		year = int(m.group(1))
		# Sanity bounds
		if 1400 <= year <= 2100:
			return year
	return None
