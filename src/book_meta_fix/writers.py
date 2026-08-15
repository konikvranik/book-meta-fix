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
from typing import Any
from uuid import uuid4

from lxml import etree

from .models import BookMeta

log = logging.getLogger(__name__)

NS_OPF = "http://www.idpf.org/2007/opf"
NS_DC = "http://purl.org/dc/elements/1.1/"


def write_book_meta(meta: BookMeta, *, dry_run: bool = True, backup: bool = True) -> dict:
	"""Write both metadata.json and metadata.opf for *meta*.

	metadata.json is a SURGICAL MERGE: bmf overlays only the fields it manages
	(title, authors, isbn, year, genres, tags, series, publisher, language,
	description, subtitle, uuid) onto the existing manifest, so ABS-owned fields
	bmf does not model (narrators, chapters, asin, explicit, abridged,
	publishedDate, ...) survive a metadata edit instead of being nulled —
	adding an ISBN must not wipe the narrators. metadata.opf is regenerated
	wholesale (a derived mirror for Calibre/Kavita; it cannot represent those
	ABS fields anyway).

	Returns a dict describing what was written (or would be, if dry_run).
	"""
	folder = Path(meta.path)
	result = {"folder": str(folder), "dry_run": dry_run, "written": []}
	if not folder.is_dir():
		result["error"] = f"folder not found: {folder}"
		return result

	json_path = folder / "metadata.json"
	opf_path = folder / "metadata.opf"

	# Overlay bmf-managed fields onto the existing manifest (preserves the rest).
	manifest = {**_load_manifest(json_path), **_json_overlay(meta)}
	json_payload = json.dumps(manifest, ensure_ascii=False, indent=2)
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


def ensure_uuid(meta: BookMeta, *, dry_run: bool = False) -> str | None:
	"""Ensure *meta* carries a stable uuid, persisting it to disk if missing.

	The uuid is bmf's per-book identity, shared by the review system (carry-
	over / prune) and the cache (PK). It is minted **lazily** — the first time
	a book is needed (a cache miss during scan, or an apply) — and written into
	both ``metadata.json`` (source of truth) and ``metadata.opf``.

	Like ``write_book_meta`` (but narrower) this is a key-preserving injection:
	it loads the existing manifest, sets ONLY the ``uuid`` key, and writes it
	back, so ABS-owned fields bmf does not model (narrators, chapters, asin,
	explicit, abridged, ...) survive. ``write_book_meta`` itself now merges its
	field overlay onto the existing manifest too, so applies no longer clobber
	those fields either; this helper exists only because minting a uuid should
	not require rendering the whole manifest.

	Returns ``meta.uuid``. In dry_run nothing is written (the uuid is still
	minted onto *meta* so an in-memory caller can proceed). Returns ``None``
	if the folder does not exist.
	"""
	folder = Path(meta.path)
	if not folder.is_dir():
		return None
	if meta.uuid:
		return meta.uuid

	uuid = str(uuid4())
	json_path = folder / "metadata.json"
	opf_path = folder / "metadata.opf"

	if not dry_run:
		# metadata.json: load, set uuid, write back preserving every other key.
		if json_path.is_file():
			_backup(json_path)
			data = json.loads(json_path.read_text(encoding="utf-8"))
		else:
			data = {}
		if not isinstance(data, dict):  # corrupt/unexpected — don't clobber
			data = {}
		data["uuid"] = uuid
		_atomic_write(json_path, json.dumps(data, ensure_ascii=False, indent=2))

		# metadata.opf: inject/refresh the uuid identifier. Best-effort — the
		# json above is authoritative, so an opf failure is only a warning.
		if opf_path.is_file():
			try:
				_inject_opf_uuid(opf_path, uuid)
			except Exception:  # noqa: BLE001
				log.warning("could not inject uuid into %s; json remains authoritative", opf_path, exc_info=True)

	meta.uuid = uuid
	return uuid


def _inject_opf_uuid(opf_path: Path, uuid: str) -> None:
	"""Set ``<dc:identifier opf:scheme="uuid">`` in an existing OPF to *uuid*.

	Updates the text if a uuid identifier already exists, otherwise appends a
	new one to ``<metadata>``. Backed up and rewritten in place. lxml round-
	trips/reformats the file, which is fine — the opf is machine-generated and
	kept only for Calibre/Kavita compatibility.
	"""
	_backup(opf_path)
	tree = etree.parse(str(opf_path))
	md = tree.find(f"{{{NS_OPF}}}metadata")
	if md is None:
		return
	uuid_el = None
	for ident in md.findall(f"{{{NS_DC}}}identifier"):
		if ident.get(f"{{{NS_OPF}}}scheme") == "uuid":
			uuid_el = ident
			break
	if uuid_el is None:
		uuid_el = etree.SubElement(
			md, f"{{{NS_DC}}}identifier",
			attrib={f"{{{NS_OPF}}}scheme": "uuid", "id": "uuid_id"},
		)
	uuid_el.text = uuid
	xml = etree.tostring(tree, xml_declaration=True, encoding="utf-8", pretty_print=True)
	_atomic_write(opf_path, xml.decode("utf-8"))


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


def _load_manifest(json_path: Path) -> dict[str, Any]:
	"""Read the existing metadata.json as a dict, or {} if missing/corrupt.

	write_book_meta merges its field overlay onto this so a metadata edit
	preserves ABS-owned keys bmf does not model (narrators, chapters, asin, ...).
	"""
	if not json_path.is_file():
		return {}
	try:
		data = json.loads(json_path.read_text(encoding="utf-8"))
	except (json.JSONDecodeError, OSError):
		return {}
	return data if isinstance(data, dict) else {}


def _json_overlay(meta: BookMeta) -> dict[str, Any]:
	"""The bmf-managed subset of the Audiobookshelf manifest, as a dict.

	Only fields bmf models and may edit. Deliberately does NOT include
	narrators/chapters/asin/explicit/abridged/publishedDate: those are ABS-owned
	and must be preserved by merging onto the existing manifest (see
	write_book_meta / _load_manifest) — emitting them here would overwrite real
	values with defaults.
	"""
	return {
		"tags": meta.tags,
		# uuid is the stable per-book identity; persisted to the source of truth
		# so it survives folder renames/moves (organize). Read back by
		# _fill_from_json, so it round-trips instead of being regenerated.
		"uuid": meta.uuid,
		"title": _sanitize_xml_text(meta.title),
		"subtitle": _sanitize_xml_text(meta.subtitle),
		"authors": [_sanitize_xml_text(a) for a in meta.authors] if meta.authors else [],
		"series": meta.series if isinstance(meta.series, list) else [],
		"genres": meta.genres,
		"publishedYear": str(meta.year) if meta.year else None,
		"publisher": _sanitize_xml_text(meta.publisher),
		"description": _sanitize_xml_text(meta.description),
		"isbn": meta.isbn,
		"language": meta.language,
	}


def _render_json(meta: BookMeta) -> str:
	"""Render the bmf-managed manifest fields as JSON (no existing-file merge).

	Lossy by design (drops ABS-owned fields bmf does not model) — use only for
	previews/tests. The real write path is write_book_meta, which merges
	_json_overlay onto the existing manifest.
	"""
	return json.dumps(_json_overlay(meta), ensure_ascii=False, indent=2)


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

	# Series — calibre reads these exact meta names back (see readers.py's
	# OPF fallback), so the mirror stays lossless for series data.
	series_name, series_index = meta.series_pair()
	if series_name:
		etree.SubElement(
			md, f"{{{NS_OPF}}}meta",
			attrib={"name": "calibre:series", "content": _sanitize_xml_text(series_name)},
		)
		if series_index:
			etree.SubElement(
				md, f"{{{NS_OPF}}}meta",
				attrib={"name": "calibre:series_index", "content": _sanitize_xml_text(series_index) or ""},
			)

	# Genres + tags as dc:subject (calibre's representation for both); de-dup
	# while keeping order, genres first.
	for subject in dict.fromkeys([*(meta.genres or []), *(meta.tags or [])]):
		etree.SubElement(md, f"{{{NS_DC}}}subject").text = _sanitize_xml_text(str(subject))

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
