"""Interactive Tkinter editor for review.yaml (``bmf gui``).

A keyboard-first reviewer: the user walks the review entries one at a time,
sees the read-only *current* fields next to editable *target* fields, swaps
author/title with one key, previews the current / .bak / recommended covers
plus each format's EMBEDDED cover (with per-cover delete checkboxes, so
invalid calibre-titled files can be cleaned out), and reads each format's
extracted text (with optional double-encoding repair). Every action has a
``Ctrl+letter`` shortcut — bare letters are intentionally NOT used so they
keep typing into the fields.

Layout: a single scrollable column on the right (covers + content sit *below*
the metadata fields — the screen is big, scroll when it doesn't fit) instead
of tabs. ``Tab`` cycles only the editable fields; buttons are reached only by
their shortcut. ``Ctrl+A`` selects all in an Entry (X11's default is "home",
so we rebind it).

Persistence model (deliberately no new writer): the editor loads the file's
**raw entry dicts** via :func:`review._load_raw_entries` and writes them back
with the same primitives the streaming writer already uses
(:func:`review._header` + :func:`review._render_entry`). Mutations touch
``action`` / ``proposed`` (the field values; ``null`` = field delete) /
``notes``; every other key is preserved verbatim, so the round-trip is
byte-compatible with ``analyze`` output. Cover and content operations are
immediate, reversible file reads (``.bak`` backed); the actual metadata write
still happens via ``bmf apply``.

Tkinter is optional: the top-level import is guarded so that importing this
module (e.g. in tests, which exercise only the pure helpers below) does not
require ``python3-tk``. Only :func:`run_gui` needs a working Tk.
"""
from __future__ import annotations

import bisect
import io
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import NamedTuple
from uuid import uuid4

from .covers import (
	analyze_cover,
	download_cover,
	epub_cover_image,
	extract_cover_from_book,
	strip_cover_from_book,
)
from .detectors import split_series_index
from .encoding import detect_double_decode, recode, recode_failure_reason, repair_chain
from .extractors import FULL_TEXT_LIMIT, extract, stream_full_text
from .i18n import _
from .library import _META_FILES, Cache, _is_excluded, iter_book_folders
from .models import series_entry_pair
from .mover import merge_folders, merge_meta, same_book
from .readers import EBOOK_EXTS, read_book_folder
from .review import _build_current, _header, _load_raw_entries, _render_entry
from .writers import ensure_uuid, write_book_meta

log = logging.getLogger(__name__)

# Codec choices for the manual z/do recode experiment (result is always
# displayed as UTF-8 text — a str is a str; the pair only says how to recover
# the original bytes).
ENCODING_CHOICES = (
	"utf-8", "cp1250", "iso-8859-2", "cp1252", "latin-1", "cp1251", "maccentraleurope",
)

# Optional Tk — guarded so the pure helpers stay importable without python3-tk.
try:  # pragma: no cover - exercised only when Tk is present
	import tkinter as tk
	from tkinter import messagebox, ttk

	from PIL import ImageTk
except ImportError:  # pragma: no cover
	ttk = None  # type: ignore[assignment]


# Editable target fields, in Tab-traversal order. ``authors`` / ``genres`` are
# list-valued (stored comma-separated in the Entry, split on save). ``series`` /
# ``series_index`` are written into meta.series as {"name", "index"} at apply.
FIELD_SPECS: list[tuple[str, str]] = [
	("title", _("Title")),
	("author", _("Author")),
	("isbn", "ISBN"),
	("year", _("Year")),
	("publisher", _("Publisher")),
	("language", _("Language")),
	("series", _("Series")),
	("series_index", _("Series order")),
	("authors", _("Authors (comma-separated)")),
	("genres", _("Genres (comma-separated)")),
]
LIST_FIELDS = {"authors", "genres"}

COVER_FILE = "cover.jpg"
COVER_BAK = "cover.jpg.bak"

# A custom bindtag prepended to every focusable widget so our <Tab>/<Shift-Tab>
# handler runs FIRST (before Tk's default focus traversal) and can "break" it.
# ``bind_all`` binds to the "all" tag, which runs LAST in the bindtags order,
# so the default traversal would already have moved focus by the time our
# handler sees the event — that was the user's "Tab jumps to RO/buttons" bug.
_TAB_TRAP_TAG = "BmfTabTrap"
# Widget classes that can receive keyboard focus and thus must respect the
# Tab trap (so Tab never lands on a button / RO label / checkbox).
_FOCUSABLE_CLASSES = frozenset({
	"TEntry", "TButton", "TCheckbutton", "TRadiobutton", "TCombobox",
	"Treeview", "Entry", "Text",
})

# Ctrl+letter shortcuts that must NOT be intercepted (so text editing in Entry
# fields keeps working): copy / paste / cut / select-all / undo / redo. Ctrl+A
# is additionally rebound on each Entry to real "select all" (X11 default is
# "move to start"), but it stays here as a passthrough so the generic handler
# never blocks it.
_PASSTHROUGH = {"c", "v", "x", "a", "z", "y"}

# Responsive detail split: below this width (px of the DETAIL area, not the
# whole window — widening the book list with the outer sash counts too) the
# content preview moves from the right column to a bottom row. It moves back
# only after the width exceeds the threshold by _DETAIL_HYSTERESIS, so
# dragging a sash across the boundary cannot flap between the two layouts.
_DETAIL_NARROW_WIDTH = 940
_DETAIL_HYSTERESIS = 80


# ---------------------------------------------------------------------------
# Pure helpers (no Tk, no network — unit-tested directly)
# ---------------------------------------------------------------------------


def cover_paths(library: Path | str, rel_path: str) -> tuple[Path, Path]:
	"""Absolute ``(cover.jpg, cover.jpg.bak)`` paths for a book's folder."""
	folder = Path(library) / rel_path
	return folder / COVER_FILE, folder / COVER_BAK


def list_format_files(folder: Path | str) -> list[Path]:
	"""Ebook files in *folder*, ordered by the readers' format preference.

	Never raises — a missing or unreadable folder yields ``[]`` (the book may
	have been moved/deleted since review.yaml was generated).
	"""
	folder = Path(folder)
	if not folder.is_dir():
		return []
	pref = {ext: i for i, ext in enumerate(EBOOK_EXTS)}
	try:
		found = [e for e in folder.iterdir() if e.is_file() and e.suffix.lower() in pref]
	except OSError:
		return []
	found.sort(key=lambda e: pref.get(e.suffix.lower(), 999))
	return found


def collect_vocab_values(library: Path | str) -> tuple[list[str], list[str]]:
	"""Distinct ``(authors, series)`` names across the whole library.

	Feeds the Entry autocomplete in the editor: the pools must contain not
	just the review entries' own values but every author/series already in
	use, so a repair can be typed consistently with the rest of the library.
	Reads only ``metadata.json`` (the source of truth) — no OPF fallback, no
	cover/content work; unreadable folders are skipped. Series names go
	through ``series_entry_pair`` so the suggestion is the BARE name — the
	ABS-native ``"Name #N"`` string and the ``{"name", ...}`` dict both
	yield "Name", never the glued "#N" form.
	"""
	from .models import series_entry_pair

	authors: set[str] = set()
	series: set[str] = set()
	try:
		folders = list(iter_book_folders(Path(library)))
	except (OSError, ValueError):
		return [], []
	for folder in folders:
		try:
			data = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
		except (OSError, ValueError):
			continue
		for a in data.get("authors") or []:
			if isinstance(a, str) and a.strip():
				authors.add(a.strip())
		s = data.get("series") or []
		if isinstance(s, (str, dict)):
			s = [s]
		for item in s if isinstance(s, list) else []:
			name, _ = series_entry_pair(item)
			if name.strip():
				series.add(name.strip())
	return sorted(authors), sorted(series)


def compose_overlay(values: dict[str, str], cleared=frozenset()) -> dict | None:
	"""Build the field overlay the editor merges into ``proposed`` on save.

	What is in the field is saved; an EMPTY field means "leave as is"
	(skipped) so a book without a proposal cannot accidentally blank
	existing data. List fields are split on commas; ``year`` is coerced
	to int when numeric. A role in *cleared* is the editor's "delete this
	field" mark (the proposal is wrong, the correct value unknown): it is
	stored as ``proposed[field]: null`` regardless of the Entry text, and
	apply then CLEARS the stored value. Returns ``None`` when nothing was
	filled and nothing cleared (nothing to merge).
	"""
	overlay: dict = {}
	for field, value in values.items():
		if field in cleared:
			continue
		v = str(value).strip()
		if not v:
			continue
		if field in LIST_FIELDS:
			overlay[field] = [p.strip() for p in v.split(",") if p.strip()]
		elif field == "year":
			overlay[field] = int(v) if v.isdigit() else v
		else:
			overlay[field] = v
	for field in cleared:
		overlay[field] = None
	return overlay or None


def action_value(action: str) -> str | None:
	"""Radio state → review.yaml value: "pending" (no decision yet) serialises
	as ``action: null``. Shared by the immediate write (_on_action_changed) and
	the switch/save collect (_collect_current) so the two cannot drift."""
	return action if action != "pending" else None


# Fields a bulk edit makes sense for — both flat strings in review.yaml.
BULK_FIELDS = ("author", "series")


def apply_bulk_field(entries: list[dict], indices, field: str, value: str | None,
                     *, delete: bool = False) -> int:
	"""Merge one bulk-edited value into the selected entries' ``proposed``.

	The bulk twin of the editor's per-field edit: a non-empty *value* (or
	*delete* — the ∅ semantics, ``proposed[field]: null``) is merged over
	each selected entry's proposal. A pending entry gains ``action:
	accept``: a proposal without a decision is skipped by ``bmf apply``,
	and a bulk edit IS the decision (the same rule the ∅ button follows);
	existing decisions are never overridden. The series ORDER stays
	per-book — only the name is touched, ``series_index`` is never merged.
	Entries decided as ``merge`` are SKIPPED: apply's merge branch ignores
	field proposals (it only consumes ``merge_into``), so a field edit on
	them would be silently lost — un-decide them first. The proposal dict
	is REBOUND, never mutated in place: a library-served
	entry shares its ``proposed`` with the pristine index (search serves
	shallow copies), which must stay untouched. Returns how many entries
	were touched.
	"""
	v = None if delete else str(value or "").strip()
	if not delete and not v:
		return 0
	n = 0
	for i in indices:
		if not (0 <= i < len(entries)):
			continue
		e = entries[i]
		if e.get("action") == "merge":
			continue
		e["proposed"] = {**(e.get("proposed") or {}), field: v}
		if not e.get("action"):
			e["action"] = "accept"
		n += 1
	return n


def apply_bulk_action(entries: list[dict], indices, action: str) -> int:
	"""Set the action on every selected entry (bulk Ctrl+Shift+A).

	Unlike :func:`apply_bulk_field` — which only DECIDES pending entries,
	because a field edit merely implies the decision — this is the explicit
	"these selected books are accepted" command, so it overrides whatever
	decision was there before. Returns how many entries were set.
	"""
	n = 0
	for i in indices:
		if not (0 <= i < len(entries)):
			continue
		entries[i]["action"] = action
		n += 1
	return n


def apply_bulk_verified(entries: list[dict], indices, value: bool) -> int:
	"""Set/clear the persistent OK mark on every selected entry (Ctrl+Shift+O).

	The mark composes with any action ("accept + verified" = fix AND close);
	``value=False`` pops the key so it serialises as absent, exactly like the
	single-book checkbox. Returns how many entries were touched.
	"""
	n = 0
	for i in indices:
		if not (0 <= i < len(entries)):
			continue
		if value:
			entries[i]["verified"] = True
		else:
			entries[i].pop("verified", None)
		n += 1
	return n


def execute_bulk_cover_delete(entries: list[dict], indices, library: Path | str, *,
                              covers: bool = True, baks: bool = False,
                              embedded: bool = False) -> tuple[int, int]:
	"""Delete covers on disk for every selected entry (bulk Ctrl+Shift+M).

	The bulk twin of the per-book Delete-checked button: sidecar
	``cover.jpg`` / ``cover.jpg.bak`` removal plus (opt-in) the embedded-cover
	strip out of the EPUB files — immediate file operations, exactly like the
	single-book path. When the sidecar itself is being deleted, a proposed
	``cover_url`` is dropped from the touched entries (REBOUND, never mutated
	in place — a library-served entry shares its proposal with the pristine
	index): apply re-downloads a proposed cover for a C11/MISSING_COVER book,
	so keeping the URL would undo the deletion on the next run. Returns
	``(files_removed, epubs_stripped)``.
	"""
	library = Path(library)
	removed = stripped = 0
	for i in indices:
		if not (0 <= i < len(entries)):
			continue
		e = entries[i]
		paths = []
		cover_path, bak_path = cover_paths(library, e.get("path", ""))
		if covers:
			paths.append(cover_path)
		if baks:
			paths.append(bak_path)
		removed += delete_covers(paths)
		if embedded:
			for f in list_format_files(library / e.get("path", "")):
				if strip_cover_from_book(f):
					stripped += 1
		if covers:
			prop = e.get("proposed") or {}
			if "cover_url" in prop:
				e["proposed"] = {k: v for k, v in prop.items() if k != "cover_url"}
	return removed, stripped


class MergeOutcome(NamedTuple):
	"""Result of :func:`execute_merge` (what moved, what left the list)."""

	winner: dict          # the surviving entry (identity-stable across the list rebuild)
	merged_count: int     # losers actually folded in (uuid-less legacy ones included)
	dropped_uuids: list   # uuids removed from the list (library-index pruning)
	failures: list        # "label: error" per loser that could not be merged
	moved_files: int      # ebook/cover files moved into the winner folder


# Fields the merge dialog offers for a per-book choice, in display order.
# ``series`` travels as a ``(name, index)`` pair so one radio pick takes both
# halves (splitting them would almost always be a mistake).
MERGE_FIELDS: tuple[tuple[str, str], ...] = (
	("title", _("Title")),
	("authors", _("Authors (comma-separated)")),
	("isbn", "ISBN"),
	("year", _("Year")),
	("publisher", _("Publisher")),
	("language", _("Language")),
	("series", _("Series")),
	("genres", _("Genres (comma-separated)")),
	("description", _("Description")),
)


def _merge_value_empty(v) -> bool:
	"""True when a merge-field value carries nothing (an ∅ pick clears it)."""
	if v is None:
		return True
	if isinstance(v, tuple):  # the series (name, index) pair
		return not v[0]
	if isinstance(v, (list, str)):
		return not v
	return False  # a bare 0/False year is still a value


def merge_field_value(entry: dict | None, meta, field: str):
	"""Effective per-field value for the merge dialog (raw, not display).

	A DECIDED proposal (accept/keep) wins over the disk value — the same
	decision-aware convention the list labels follow — because that is what
	``bmf apply`` would write for that book; without a decision the entry
	shows what is actually stored (an undecided proposal is still only a
	suggestion, and library-served entries have none). The series field
	returns a ``(name, index)`` pair, ``authors`` / ``genres`` lists.
	"""
	entry = entry or {}
	prop = entry.get("proposed") or {}
	decided = entry.get("action") in ("accept", "keep")
	if field == "series":
		if decided and ("series" in prop or "series_index" in prop):
			return (prop.get("series") or "", str(prop.get("series_index") or ""))
		name, idx = meta.series_pair()
		return (name, str(idx or ""))
	if field == "authors":
		if decided and "authors" in prop:
			v = prop["authors"]
			return list(v) if isinstance(v, list) else ([v] if v else [])
		if decided and "author" in prop:
			v = prop["author"]
			return [v] if isinstance(v, str) and v else []
		return list(meta.authors)
	if decided and field in prop:
		return prop[field]
	return {
		"title": meta.title,
		"isbn": meta.isbn,
		"year": meta.year,
		"publisher": meta.publisher,
		"language": meta.language,
		"genres": list(meta.genres),
		"description": meta.description,
	}.get(field)


def merge_cell_text(field: str, value) -> str:
	"""Short cell label for the merge dialog's field grid (∅ = pick as empty)."""
	if _merge_value_empty(value):
		return "∅"
	if field == "series":
		name, idx = value
		return f"{name} #{idx}" if idx else name
	if isinstance(value, list):
		value = ", ".join(str(v) for v in value)
	text = str(value)
	return text if len(text) <= 56 else text[:55] + "…"


def _apply_merge_choice(meta, field: str, value) -> None:
	"""Write one merge-dialog choice onto the merged BookMeta (in place)."""
	if field == "series":
		name, idx = value if isinstance(value, tuple) else (value, "")
		meta.series = [{"name": name, "index": idx}] if name else []
	elif field in ("authors", "genres"):
		setattr(meta, field, list(value or []))
	elif field == "title":
		meta.title = value or ""
	else:  # isbn / year / publisher / language / description — None clears
		setattr(meta, field, value or None)


def merge_choice_proposal(field: str, value) -> dict:
	"""``proposed`` overlay fragment for one merge choice.

	Maps a dialog pick onto the review.yaml proposal vocabulary so
	:func:`execute_merge` can rebase a conflicting stale proposal (see its
	docstring); ``None`` values mirror the ∅ delete-mark semantics.
	"""
	if field == "series":
		name, idx = value if isinstance(value, tuple) else (value, "")
		out = {"series": name or None}
		if idx:
			out["series_index"] = idx
		return out
	if isinstance(value, list):  # authors / genres
		return {field: list(value)}
	return {field: value}


# Severity order for the "Found problems" section: pending ACTION proposals
# first (they wait for a decision), then the NEEDS_REVIEW-class damage,
# MISSING_* info last-ish, unknown categories kept at the very end in the
# analyzer's own order (the sort is stable).
_DIAG_SEVERITY: dict[str, int] = {
	"C6": 0, "C17": 0, "C19": 0,
	"C1": 1, "C2": 1, "C3": 1, "C4": 1, "C5": 1, "C7": 1, "C8": 1,
	"C10": 1, "C11": 1, "C12": 1, "C13": 1, "C14": 1,
}
_CONFIDENCE_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def _select_all_text(event) -> str:
	"""Ctrl+A inside a Text widget: select everything.

	X11's Text class binding for Control-a is "beginning of line" (the same
	surprise as in Entry, which is rebound per-widget); the readonly views
	want the universal select-all semantics.
	"""
	w = event.widget
	w.tag_add("sel", "1.0", "end")
	return "break"


def sort_diagnoses(diags: list[dict]) -> list[dict]:
	"""All of an entry's diagnoses, most important first.

	The review header used to show only the primary diagnosis plus
	"(+N more)" — and WHICH diagnosis landed first was the detector's
	accident, so the load-bearing one could hide behind the counter. The
	section lists everything, ordered by a small severity table
	(decision-waiting proposals → damage → missing-field info), confidence
	breaking ties.
	"""
	def _key(item):
		i, d = item
		cat = str(d.get("category") or "")
		weight = _DIAG_SEVERITY.get(cat, 2 if cat.startswith("MISSING_") else 3)
		return (weight, _CONFIDENCE_RANK.get(str(d.get("confidence") or "").upper(), 3), i)

	return [d for _, d in sorted(enumerate(diags), key=_key)]


def merge_projection_rows(survivor_meta, loser_meta, entry: dict | None = None) -> list[dict]:
	"""Comparison rows for the C19 merge panel — what apply WILL write.

	One row per :data:`MERGE_FIELDS` entry where the two folders differ or an
	explicit pick exists. Each row carries the survivor's disk value, the
	loser's disk value and the EFFECTIVE post-merge value: the automatic
	survivor-wins result (:func:`mover.merge_meta`) with the entry's explicit
	``proposed`` picks applied on top — the same projection the apply branch
	executes, so the panel can never disagree with the outcome. Display
	strings reuse the merge dialog's cell renderer (∅ = empty).
	"""
	merged = merge_meta(survivor_meta, loser_meta)
	picks: dict = {}
	if entry is not None:
		picks = {
			k: v for k, v in (entry.get("proposed") or {}).items()
			if k not in ("merge_into", "source", "delete_files", "cover_source")
		}
		if picks:
			from .pipeline import _apply_fields  # lazy: pipeline pulls the world

			_apply_fields(merged, picks)
	rows = []
	for field, label in MERGE_FIELDS:
		sv = merge_field_value(None, survivor_meta, field)
		lv = merge_field_value(entry, loser_meta, field)
		ev = merge_field_value(None, merged, field)
		s_text, l_text, e_text = (merge_cell_text(field, v) for v in (sv, lv, ev))
		if s_text == l_text and field not in picks:
			continue  # identical on both sides and unpicked — no decision to show
		rows.append({
			"field": field, "label": label,
			"survivor": sv, "loser": lv, "effective": ev,
			"s_text": s_text, "l_text": l_text, "e_text": e_text,
			"picked": field in picks,
		})
	return rows


def merge_file_plan(loser_folder: Path, survivor_folder: Path,
                    cover_source: str | None = None) -> list[str]:
	"""Human-readable fate of the loser's files, using the SAME rules
	:func:`mover._merge_format_files` applies (pure reporting — no disk
	changes, identical bytes skipped, name collisions get the ``(id N)``
	suffix, the survivor's cover wins unless an explicit ``cover_source``
	pick says otherwise)."""
	try:
		survivor_files = {
			p.name.lower(): p for p in survivor_folder.iterdir() if p.is_file()
		}
	except OSError:
		survivor_files = {}
	lines: list[str] = []
	try:
		entries_iter = sorted(loser_folder.iterdir(), key=lambda p: p.name)
	except OSError:
		return lines
	for p in entries_iter:
		if not p.is_file():
			continue
		lname = p.name.lower()
		if lname in _META_FILES or lname.endswith(".bak"):
			continue  # subsumed by the merged metadata
		is_ebook = p.suffix.lower() in EBOOK_EXTS
		is_cover = lname == "cover.jpg"
		if not (is_ebook or is_cover):
			continue
		target = survivor_files.get(lname)
		if is_cover:
			if cover_source in ("loser", "loser_epub"):
				lines.append(_("cover.jpg → this book's cover becomes THE cover (survivor's → cover.jpg.bak)"))
			elif target is not None:
				lines.append(_("cover.jpg → the survivor's cover wins (this one is discarded)"))
			else:
				lines.append(_("cover.jpg → moves to the survivor (it has none)"))
			continue
		if target is None:
			lines.append(_("{name} → moves to the survivor").format(name=p.name))
			continue
		try:
			if (target.stat().st_size == p.stat().st_size
					and target.read_bytes() == p.read_bytes()):
				lines.append(_("{name} → identical bytes at the survivor (skipped)").format(name=p.name))
				continue
		except OSError:
			pass
		lines.append(_('{name} → name collision at the survivor, renamed to "{stem} (id){sfx}"').format(
			name=p.name, stem=p.stem, sfx=p.suffix))
	return lines


def execute_merge(entries: list[dict], winner_idx: int, loser_idxs,
                  library: Path | str, *, values: dict | None = None) -> MergeOutcome:
	"""Merge the selected entries' folders into the WINNER's folder (Ctrl+J).

	The user-driven counterpart of apply's placement merge (which only fires
	when two folders collide at the same pattern target): each loser is folded
	into the winner via :func:`mover.merge_folders` — ebook/cover files move
	in (collisions rename with the loser's calibre id), metadata is
	field-merged (winner wins, losers fill gaps), the loser folder is removed.
	The winner's review entry survives (``bmf apply`` finishes it later);
	successfully merged losers are REMOVED from *entries* in place — their
	folders are gone, and a stale entry would fail the next apply with
	"folder not found". The winner's ``current`` is refreshed from the merged
	on-disk metadata so the editor (and the library-entry change detection)
	shows the truth. Losers that fail (missing folder, IO error) stay in the
	list and land in ``failures``.

	*values* (the merge dialog's per-field picks, ``field → raw value`` as
	produced by :func:`merge_field_value`) OVERRIDES the automatic field
	merge: after the files move, the chosen values are written onto the
	survivor's metadata on disk, and every EXISTING proposed key they conflict
	with is rebased to the chosen value — a stale analyzer proposal must not
	silently undo an explicit per-field decision at the next apply. Fields
	without a proposal key are left alone: the disk already carries the
	chosen value, and inventing proposal noise for a clean book buys nothing.
	"""
	library = Path(library)
	winner = entries[winner_idx]
	winner_folder = (library / winner.get("path", "")).resolve()
	winner_meta = read_book_folder(winner_folder)
	merged_losers: list[dict] = []
	failures: list[str] = []
	moved = 0
	for i in loser_idxs:
		if i == winner_idx or not (0 <= i < len(entries)):
			continue
		loser = entries[i]
		loser_folder = (library / loser.get("path", "")).resolve()
		try:
			if loser_folder == winner_folder:
				raise FileNotFoundError(_("same folder as the survivor"))
			if not loser_folder.is_dir():
				raise FileNotFoundError(_("folder not found"))
			loser_meta = read_book_folder(loser_folder)
			res = merge_folders(winner_folder, winner_meta, loser_meta,
			                    dry_run=False, library=library)
			if res.error:
				raise RuntimeError(res.error)
			moved += len(res.details or [])
			# The merged write replaced the winner's metadata — re-read so the
			# NEXT loser merges on top of the accumulated state.
			winner_meta = read_book_folder(winner_folder)
			merged_losers.append(loser)
		except Exception as e:  # noqa: BLE001
			failures.append(f"{entry_label(loser)}: {e}")
	if merged_losers:
		# Drop by IDENTITY, not uuid — a legacy uuid-less entry must not take
		# every other uuid-less entry with it. ``entries[:] =`` keeps the list
		# object (self.entries) intact.
		entries[:] = [e for e in entries
		              if not any(e is lo for lo in merged_losers)]
		final_meta = read_book_folder(winner_folder)
		if values:
			for field, value in values.items():
				_apply_merge_choice(final_meta, field, value)
			write_book_meta(final_meta, dry_run=False, backup=True)
			prop = dict(winner.get("proposed") or {})
			rebased = False
			for field, value in values.items():
				for key, val in merge_choice_proposal(field, value).items():
					if key in prop and prop[key] != val:
						prop[key] = val
						rebased = True
			if rebased:  # a proposal-less book must not gain proposal noise
				winner["proposed"] = prop
		winner["current"] = _build_current(final_meta)
	return MergeOutcome(
		winner=winner,
		merged_count=len(merged_losers),
		dropped_uuids=[e.get("uuid") for e in merged_losers if e.get("uuid")],
		failures=failures,
		moved_files=moved,
	)


def render_review_text(entries: list[dict]) -> str:
	"""Render raw entry dicts as a multi-doc review.yaml string.

	Reuses :func:`review._header` + :func:`review._render_entry` — the exact
	primitives the streaming writer uses — so an edit/save round-trip stays
	byte-compatible with ``analyze`` output (insertion order, single-issue
	shape, unicode all preserved).
	"""
	body = "\n".join(_render_entry(e) for e in entries)
	if body:
		body += "\n"
	return _header(len(entries)) + body


def restore_bak_cover(cover_path: Path, bak_path: Path) -> bool:
	"""Restore ``cover.jpg`` from its ``.bak`` (the previous cover). No-op if the
	backup is absent. Returns True when a restore actually happened."""
	if not bak_path.is_file():
		return False
	shutil.copy2(bak_path, cover_path)
	return True


def delete_covers(paths: list[Path]) -> int:
	"""Unlink each existing path in *paths*. Returns how many were removed."""
	n = 0
	for raw in paths:
		p = Path(raw)
		if p.is_file():
			p.unlink(missing_ok=True)
			n += 1
	return n


def load_thumb(source: Path | str | bytes, max_w: int, max_h: int):
	"""Return a PIL thumbnail (RGB, fitted into max_w/max_h) or None.

	*source* is a path or raw image bytes. Never raises — a missing/corrupt
	cover yields None, which the UI renders as a placeholder."""
	try:
		from PIL import Image
	except ImportError:
		return None
	try:
		if isinstance(source, (bytes, bytearray)):
			img = Image.open(io.BytesIO(bytes(source)))
		else:
			img = Image.open(Path(source))
		img = img.convert("RGB")
		img.thumbnail((max_w, max_h))
		return img
	except Exception:  # noqa: BLE001
		return None


def fetch_url_bytes(url: str, timeout: float = 15.0) -> bytes | None:
	"""Fetch image bytes from *url* (recommended cover). None on any failure."""
	try:
		import requests
	except ImportError:
		return None
	try:
		resp = requests.get(
			url,
			timeout=timeout,
			headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*,*/*;q=0.8"},
		)
		return resp.content if resp.status_code == 200 else None
	except Exception:  # noqa: BLE001
		return None


def open_folder_in_manager(folder: Path | str) -> str | None:
	"""Open *folder* in the platform file manager; None on success, else why.

	Detached (``Popen`` — the GUI must never block on the manager). A path
	pointing at a file opens its parent. The opener is platform-delegated:
	``xdg-open`` on Linux/BSD, ``open`` on macOS, ``explorer`` on Windows.
	Returns a short localized error for the status line, or None when spawned.
	"""
	folder = Path(folder)
	try:
		if folder.is_file():
			folder = folder.parent
		if not folder.is_dir():
			return _("folder does not exist: {folder}").format(folder=folder)
		opener = {"darwin": "open", "win32": "explorer"}.get(sys.platform, "xdg-open")
		if shutil.which(opener) is None:
			return _("tool '{opener}' not found").format(opener=opener)
		subprocess.Popen([opener, str(folder)])  # noqa: S603 - fixed argv, user-visible folder
		return None
	except OSError as exc:
		return _("opening failed: {exc}").format(exc=exc)


def embedded_cover_thumb(book_path: Path | str, max_w: int = 240, max_h: int = 320):
	"""Preview thumbnail of the cover EMBEDDED in an ebook file (any format).

	EPUBs are read straight from the zip via :func:`covers.epub_cover_image`
	— deterministic, no calibre subprocess, and no rendered-first-page
	fallback, so after a :func:`covers.strip_cover_from_book` the preview
	correctly shows nothing (calibre's ``ebook-meta --get-cover`` would hand
	back a render of page 1 and look like the cover survived). Other formats
	extract via :func:`covers.extract_cover_from_book` (calibre ebook-meta)
	into a temp file, which is removed — nothing lands in the library.
	Unlike :func:`covers.recover_cover_from_book` this does NOT gate on
	generated placeholders: the point of the GUI preview is to SEE a
	calibre-written placeholder, so the embedded cover can be flagged for
	stripping. Returns a PIL image or None (nothing embedded / corrupt).
	"""
	if Path(book_path).suffix.lower() == ".epub":
		# Zip probe ONLY — no calibre fallback here. An SVG cover is the price
		# of truthfulness: calibre's --get-cover renders page 1 as a "default
		# cover" even for a genuinely coverless EPUB, so a fallback path would
		# show a fake "cover" right after a successful strip.
		return load_thumb(epub_cover_image(book_path) or b"", max_w, max_h)
	tmp = extract_cover_from_book(book_path)
	if tmp is None:
		return None
	tmp = Path(tmp)  # tolerate str returns (defensive; never raise in helpers)
	try:
		return load_thumb(tmp, max_w, max_h)
	finally:
		try:
			tmp.unlink(missing_ok=True)
		except OSError:
			log.warning("could not remove temp cover %s", tmp, exc_info=True)


def extract_series_values(src: object) -> list[str]:
	"""Extract searchable series strings (names, indices, pairs) from a container.

	Handles:
	- A container dict with "series" and/or "series_index" keys (like current/proposed/edited)
	- Raw series values: str, dict ({"name", "index"/"sequence"}), or list thereof
	"""
	if not src:
		return []
	tokens: list[str] = []

	def _add_pair(name: object, idx: object = None) -> None:
		name_s = str(name).strip() if name is not None else ""
		if name_s:
			tokens.append(name_s)
		idx_s = str(idx).strip() if idx is not None else ""
		if idx_s:
			tokens.append(idx_s)
		if name_s and idx_s:
			tokens.append(f"{name_s} {idx_s}")
			tokens.append(f"{name_s} #{idx_s}")

	if isinstance(src, dict) and ("series" in src or "series_index" in src):
		s_val = src.get("series")
		s_idx = src.get("series_index")
		items = [s_val] if isinstance(s_val, (str, dict)) else (s_val if isinstance(s_val, list) else [])
		if not items and s_idx is not None:
			_add_pair(None, s_idx)
		for item in items:
			if isinstance(item, str):
				_add_pair(item, s_idx)
			elif isinstance(item, dict):
				name = item.get("name")
				idx = item.get("index") if item.get("index") is not None else item.get("sequence")
				if idx is None:
					idx = s_idx
				_add_pair(name, idx)
	elif isinstance(src, str):
		_add_pair(src, None)
	elif isinstance(src, dict):
		name = src.get("name")
		idx = src.get("index") if src.get("index") is not None else src.get("sequence")
		_add_pair(name, idx)
	elif isinstance(src, list):
		for item in src:
			tokens.extend(extract_series_values(item))

	return tokens


def entry_series_pair(e: dict) -> tuple[str, str]:
	"""Decision-aware ``(name, order)`` pair for an entry's series.

	``("", "")`` when the book has no series. An ACCEPT/KEEP decision
	applies ``proposed`` like apply will, so the pair reflects the series
	as the decision leaves it (a ``null`` proposal deletes the field — the
	same semantics ``_apply_fields`` uses); pending and delete entries show
	``current``. Composes the flat ``series`` / ``series_index`` pair
	(review.yaml shape); the raw ABS shapes (a dict, a list of dicts with
	the legacy ``sequence`` key) are tolerated the same way
	:func:`extract_series_values` tolerates them. An order glued into the
	name ("Mark Stone #73", index empty) is normalised through the C14
	splitter so the pair comes out split; an index that DIFFERS from an
	embedded one is left alone (the deliberate state C14 refuses to touch).
	"""
	cur = (e or {}).get("current") or {}
	prop = (e or {}).get("proposed") or {}
	if prop and e.get("action") in ("accept", "keep"):
		cur = dict(cur)
		for k in ("series", "series_index"):
			if k in prop:
				if prop[k] is None:
					cur.pop(k, None)
				else:
					cur[k] = prop[k]
	name, idx = "", ""
	s = cur.get("series")
	if isinstance(s, list):
		s = s[0] if s else None
	if isinstance(s, dict):
		raw = s.get("name")
		name = str(raw).strip() if raw is not None else ""
		raw = s.get("index")
		if raw is None:
			raw = s.get("sequence")
		idx = "" if raw is None else str(raw).strip()
	elif isinstance(s, str):
		name = s.strip()
	# A flat series_index beats whatever the raw shape carried — in the
	# normal review shape the two never coexist as different sources.
	si = cur.get("series_index")
	if si is not None:
		idx = str(si).strip()
	split = split_series_index(name) if name else None
	if split is not None and (not idx or idx == split[1]):
		name, idx = split
	return name, idx


def entry_series_label(e: dict) -> str:
	"""Display label ``"Series #N"`` for an entry's series, "" if none.

	Composed from :func:`entry_series_pair` (which carries the
	decision-aware normalization this label shows).
	"""
	name, idx = entry_series_pair(e)
	if not name:
		return f"#{idx}" if idx else ""
	return f"{name} #{idx}" if idx else name


def entry_sort_key(e: dict) -> tuple:
	"""Left-panel display order: series (name, then order in it), author, title.

	Series books form blocks at the TOP of the list, each block ordered by
	its series ORDER — a series run read in sequence is the unit of review
	(C14 glued-order splits, `+ library` merges of a series the user just
	searched); books without a series sort AFTER the blocks, by author then
	title. The order sorts NUMERICALLY ("2" < "10", "1.5" between 1 and 2);
	a missing or non-numeric order closes its block (books with a known
	order come first, the rest by author/title). Series and author are the
	decision-aware labels the row SHOWS (:func:`entry_series_pair`,
	:func:`entry_author_label`); the title is the current one (the title
	column shows current). Strings compare casefolded — a lowercase author
	must not sink below "Z".
	"""
	name, idx = entry_series_pair(e)
	try:
		num = float(idx)
		order: tuple = (num, "") if math.isfinite(num) else (math.inf, idx.casefold())
	except ValueError:
		order = (math.inf, idx.casefold())
	title = str(((e or {}).get("current") or {}).get("title") or "")
	return (
		1 if not name else 0,
		name.casefold(),
		order,
		entry_author_label(e).casefold(),
		title.casefold(),
	)


def entry_author_label(e: dict) -> str:
	"""Author shown in a list row — the decision-aware twin of
	:func:`entry_series_label`. An ACCEPT/KEEP decision applies
	``proposed`` like apply will, so the row shows the author the decision
	leaves it (a bulk edit or a C1 swap is visible in the list at once,
	not only after ``bmf apply``); pending and delete entries show
	``current``. A ``null`` proposal (the ∅ mark) deletes the field → "".
	"""
	prop = (e or {}).get("proposed") or {}
	if prop and e.get("action") in ("accept", "keep") and "author" in prop:
		v = prop["author"]
		return str(v) if v is not None else ""
	return str(((e or {}).get("current") or {}).get("author") or "")


def entry_label(e: dict) -> str:
	"""Short "title — author" label for bulk dialogs and failure reports."""
	cur = (e or {}).get("current") or {}
	title = cur.get("title") or ((e or {}).get("proposed") or {}).get("title") or "?"
	return f"{title} — {entry_author_label(e)}"


def entry_search_haystack(e: dict, active: dict | None = None) -> str:
	"""Build a lowercase space-separated string of searchable fields for an entry."""
	if not e:
		return ""
	parts: list[str] = []
	for k in ("path", "uuid", "action"):
		v = e.get(k)
		if v:
			parts.append(str(v))
	diag = e.get("diagnosis") or {}
	if isinstance(diag, dict):
		c = diag.get("category")
		if c:
			parts.append(str(c))

	sources = [e.get("current"), e.get("proposed"), e.get("edited")]
	if active:
		sources.append(active)

	for src in sources:
		if not isinstance(src, dict):
			continue
		for field in ("title", "author", "isbn"):
			v = src.get(field)
			if v:
				parts.append(str(v))
		authors = src.get("authors")
		if isinstance(authors, list):
			for a in authors:
				if a:
					parts.append(str(a))
		genres = src.get("genres")
		if isinstance(genres, list):
			for g in genres:
				if g:
					parts.append(str(g))
		parts.extend(extract_series_values(src))

	return " ".join(parts).lower()


def entry_matches_search(needle: str, entry: dict, active: dict | None = None,
                         extra_hay: str = "") -> bool:
	"""Check whether *entry* matches search query *needle*.

	Matches case-insensitively against entry metadata including author, title,
	series (name, index, and compound pairs), path, category, and action.
	Supports exact substring match as well as multi-term queries where every word
	in *needle* must appear in the entry's searchable fields. *extra_hay* is
	concatenated into the haystack — the library scan passes the description
	there (review entries never carry it, a full manifest does).
	"""
	n = needle.strip().lower()
	if not n:
		return True
	hay = entry_search_haystack(entry, active=active)
	if extra_hay:
		hay = f"{hay} {extra_hay}".lower()
	if n in hay:
		return True
	words = n.split()
	return all(w in hay for w in words)


# `proposed` keys that carry no user decision about a metadata field (they are
# analyzer bookkeeping: provenance, LLM reasoning, the informational C13 move
# preview, and cover recovery that only fires for C11/MISSING_COVER diagnoses
# — a library-loaded entry has none, so a cover_url there does nothing).
_LIB_NOISE_KEYS = frozenset({"source", "reasoning", "location", "cover_url"})


def library_entry_changed(e: dict) -> bool:
	"""Did the user actually decide something about a library-loaded entry?

	Only such entries are written into review.yaml on save. A decision is an
	``action``, the ``verified`` mark, a note — or a proposed value that
	DIFFERS from ``current``. Equality is not a change: merely browsing a
	book merges its prefilled field values into ``proposed``
	(see ``_collect_current`` / ``compose_overlay``), and a viewed book must
	not silently turn into a review entry. The C14 series-split prefill
	(``library_entry_from_meta``) is likewise not a change — a search that
	sweeps in hundreds of "#N" books must not flood review.yaml with
	pending entries; the mass fix is analyze's pre-filled accept.
	"""
	if e.get("action"):
		return True
	if e.get("verified"):
		return True
	if e.get("notes"):
		return True
	prop = e.get("proposed") or {}
	if _is_pure_series_prefill(prop, e.get("current") or {}):
		return False
	cur = e.get("current") or {}
	for k, v in prop.items():
		if k in _LIB_NOISE_KEYS:
			continue
		if k not in cur or v != cur.get(k):
			return True
	return False


def _is_pure_series_prefill(prop: dict, cur: dict) -> bool:
	"""Is *prop* exactly the untouched C14 split of the current series?

	``{"series": bare, "series_index": N}`` matching
	``split_series_index(cur["series"])`` — i.e. nobody touched the fields
	since the index built the entry. Anything else (an edited value, a null
	delete, extra keys) is a real change.
	"""
	if set(prop) != {"series", "series_index"}:
		return False
	split = split_series_index(str(cur.get("series") or ""))
	return (
		split is not None
		and prop.get("series") == split[0]
		and str(prop.get("series_index")) == split[1]
	)


def entries_to_write(entries: list[dict], lib_uuids) -> list[dict]:
	"""Which entry dicts a GUI save writes into review.yaml.

	Review entries always; library-loaded ones (tracked by *lib_uuids*, a
	uuid-keyed collection) only when the user changed them (see
	:func:`library_entry_changed`) — an untouched book pulled in by a
	library search must not flood the review file.
	"""
	return [
		e for e in entries
		if e.get("uuid") not in lib_uuids or library_entry_changed(e)
	]


def library_entry_from_meta(meta, library: Path | str) -> dict:
	"""Build a review-shaped entry dict for a library book outside review.yaml.

	Shape-compatible with an analyze entry (id/uuid/path/current/proposed/
	action) but WITHOUT a diagnosis — no rule flagged the book, the user's
	search pulled it in. ``action`` starts pending; once the user decides or
	edits, the entry is written into review.yaml on save and ``bmf apply``
	processes it like any other (placement re-detects on the final metadata,
	so the missing diagnosis costs nothing there).

	A series whose ORDER is glued into the name ("Mark Stone #73", index
	empty) gets the deterministic C14 split pre-filled as ``proposed``
	(the same :func:`detectors.split_series_index` the analyzer uses), so
	the user accepts the obvious fix instead of retyping it per book. The
	action stays pending — bulk pre-accept is analyze's job, the GUI
	proposal just saves the typing.
	"""
	library = Path(library)
	try:
		rel = str(Path(meta.path).relative_to(library))
	except ValueError:
		rel = str(Path(meta.path))
	entry = {
		"id": meta.calibre_id,
		"uuid": meta.uuid,
		"path": rel,
		"current": _build_current(meta),
		"proposed": None,
		"action": None,
	}
	name, idx = meta.series_pair()
	split = split_series_index(name)
	if split is not None and (not idx or idx.strip() == split[1]):
		entry["proposed"] = {"series": split[0], "series_index": split[1]}
	return entry


def _library_extra_hay(meta) -> str:
	"""Haystack fields only the full manifest carries (lowercased).

	The library scan reads the book's own metadata.json, so — unlike the
	review-entry search — it can also match against the description,
	tags, publisher and subtitle. That matters in practice: a series book
	may carry an empty ``series`` and mention the series only in its
	annotation (measured: ~70 "Mark Stone" books, most under other authors'
	folders, several with the name ONLY in the description). ALL series
	entries are appended too: the entry half of the haystack carries only
	the FIRST series (``_build_current`` flat pair), so without this the
	second series of a multi-series book would be unsearchable.
	"""
	parts = [meta.description, meta.publisher, meta.subtitle]
	parts.extend(meta.tags or [])
	for item in meta.series or []:
		name, idx = series_entry_pair(item)
		if name:
			parts.append(name)
			if idx:
				parts.append(idx)
	return " ".join(str(x) for x in parts if x).lower()


def _library_book_folders(library: Path, workers: int) -> list[Path]:
	"""All book folders under *library*, the tree walk parallelized per
	top-level directory.

	``iter_book_folders`` alone costs tens of seconds on the real library
	(measured: 38 s for 5339 folders over NFS v3 — each directory entry
	probes for a metadata sidecar, one RPC round trip each). Splitting the
	walk by top-level folder and running the subtrees in the same pool as
	the reads cuts that to a few seconds. Exclusions are applied to the
	top level manually (iter_book_folders applies them only below it).
	"""
	try:
		tops = sorted(
			d for d in library.iterdir()
			if d.is_dir() and not _is_excluded(d.name)
		)
	except OSError:
		return []
	from concurrent.futures import ThreadPoolExecutor

	def _walk(top: Path) -> list[Path]:
		# A top-level directory may itself be a BOOK folder (a book sitting
		# directly in the library root) — iter_book_folders treats its
		# argument as a container, so test the top itself first and don't
		# descend into it (the same rule the walk applies below the root).
		if any((top / mf).is_file() for mf in _META_FILES):
			return [top]
		return list(iter_book_folders(top))

	with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
		parts = list(ex.map(_walk, tops))
	return [f for part in parts for f in part]


def build_library_index(
	library: Path | str, *, progress=None, workers: int = 12, cache=None,
) -> list[tuple[dict, str]]:
	"""One background sweep of the whole library → ``(entry, haystack)`` pairs.

	Called ONCE per GUI session (at startup, off the Tk thread — this is
	the fulltext index the "+ library" search queries instantly, so no
	search ever sweeps the library itself). Each pair holds a review-shaped
	entry (see :func:`library_entry_from_meta`, including the C14
	series-split prefill) and its PRE-LOWERED full-text haystack:
	:func:`entry_search_haystack` (author/title/series/path/...) plus the
	manifest-only fields (:func:`_library_extra_hay` — description,
	publisher, tags, subtitle).

	When *cache* (a :class:`book_meta_fix.library.Cache`) is given, unchanged
	folders are served from the SQLite cache and only misses are read via the
	canonical reader (json > opf > path, mojibake repair included) — the
	sweep used to re-read every metadata.json over NFS on each GUI start,
	which is the exact cost the cache exists to skip. The payload holds the
	same BookMeta the reader produces, so entries and autocomplete pools are
	identical either way; miss results are put back into the cache. A book
	without a uuid gets one minted and persisted here
	(:func:`writers.ensure_uuid`, the same lazy identity augmentation a
	cache-miss scan performs; after the first indexed run the library is
	fully keyed — the review workflow is uuid-keyed). Unreadable folders are
	skipped; cache errors degrade to direct reads, never kill the sweep.
	*progress* is called as ``progress(done, total)`` every ~100 folders
	(from the calling thread — marshal to Tk yourself). Returns pairs sorted
	by (author, title).
	"""
	library = Path(library)
	folders = _library_book_folders(library, workers)
	total = len(folders)

	def _one(folder: Path) -> tuple[dict, str] | None:
		meta = None
		if cache is not None:
			try:
				meta = cache.get(folder)
			except Exception:  # noqa: BLE001 - cache trouble must not kill the sweep
				meta = None
		if meta is None:
			try:
				meta = read_book_folder(folder)
			except Exception:  # noqa: BLE001 - unreadable folder is skipped, not fatal
				return None
			if meta.uuid is None:
				try:
					ensure_uuid(meta)
				except Exception:  # noqa: BLE001
					meta.uuid = str(uuid4())  # in-memory only; apply re-mints on disk
			if cache is not None:
				try:
					cache.put(meta)
				except Exception:  # noqa: BLE001 - a failed put only costs a re-read next time
					pass
		entry = library_entry_from_meta(meta, library)
		hay = f"{entry_search_haystack(entry)} {_library_extra_hay(meta)}".lower()
		return entry, hay

	out: list[tuple[dict, str]] = []
	done = 0
	next_report = 50
	from concurrent.futures import ThreadPoolExecutor

	with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
		for pair in ex.map(_one, folders):
			done += 1
			if progress is not None and done >= next_report:
				progress(done, total)
				next_report = done + 100
			if pair is not None:
				out.append(pair)
	if progress is not None and total:
		progress(total, total)
	out.sort(key=lambda p: ((p[0].get("current") or {}).get("author") or "",
	                        (p[0].get("current") or {}).get("title") or ""))
	return out


def search_library_index(
	index: list, needle: str, skip_paths: set[str], skip_uuids: set[str],
) -> list[tuple[dict, str]]:
	"""Instant multi-word search over :func:`build_library_index` output.

	Every word of *needle* must appear in a book's haystack (the same
	AND semantics as the list's search box). Books already covered by the
	current entry list are skipped (by path OR uuid). Returns SHALLOW
	COPIES of the stored entries plus their haystacks — the GUI mutates
	served entries (``action`` / ``proposed``) and the index must stay
	pristine for later searches. Empty needle → ``[]``.
	"""
	n = needle.strip().lower()
	if not n:
		return []
	words = n.split()
	out: list[tuple[dict, str]] = []
	for entry, hay in index:
		if str(Path(entry.get("path") or "")) in skip_paths:
			continue
		u = entry.get("uuid")
		if u and u in skip_uuids:
			continue
		if all(w in hay for w in words):
			out.append((dict(entry), hay))
	return out


# ---------------------------------------------------------------------------
# Small Tk helpers (only used when Tk is available; bodies reference tk lazily)
# ---------------------------------------------------------------------------


class _Tooltip:
	"""Tiny hover tooltip for any Tk widget.

	Why not ttk's built-in: ttk has no tooltip primitive. This is the standard
	Toplevel-on-<Enter> recipe, kept dependency-free. Bodies reference ``tk``
	only inside methods, so the class is safe to define even when the optional
	Tk import failed (it simply won't be instantiated without a Tk).
	"""

	def __init__(self, widget, text: str, delay: int = 400) -> None:
		self.widget = widget
		self.text = text
		self._delay = delay
		self._tip = None
		self._id = None
		widget.bind("<Enter>", self._schedule, add="+")
		widget.bind("<Leave>", self._hide, add="+")
		widget.bind("<Motion>", self._schedule, add="+")

	def _schedule(self, event=None) -> None:
		self._cancel()
		# While a button is held (e.g. dragging the preview grip), suppress
		# the tooltip entirely: this tip's default position is right BELOW
		# the widget — exactly where a drag is heading — so popping up
		# mid-drag hijacks the pointer area and reads as a jerky resize.
		if event is not None and getattr(event, "state", 0) & 0x0100:
			self._hide()
			return
		if self.text:
			self._id = self.widget.after(self._delay, self._show)

	def _cancel(self) -> None:
		if self._id is not None:
			try:
				self.widget.after_cancel(self._id)
			except Exception:  # noqa: BLE001
				pass
			self._id = None

	def _show(self) -> None:
		if self._tip is not None or not self.text:
			return
		x = self.widget.winfo_rootx() + 16
		y = self.widget.winfo_rooty() + max(self.widget.winfo_height(), 10) + 4
		tip = tk.Toplevel(self.widget)
		tip.wm_overrideredirect(True)
		try:
			tip.geometry(f"+{x}+{y}")
		except Exception:  # noqa: BLE001
			pass
		# ttk has no -padx/-pady; the padding option takes the pair instead.
		ttk.Label(tip, text=self.text, justify="left", background="#ffffe0",
		         relief="solid", borderwidth=1, padding=(6, 3)).pack()
		self._tip = tip

	def _hide(self, _event=None) -> None:
		self._cancel()
		if self._tip is not None:
			try:
				self._tip.destroy()
			except Exception:  # noqa: BLE001
				pass
			self._tip = None


class _Autocomplete:
	"""Dropdown autocomplete attached to a plain ttk.Entry.

	Keyboard-first, matching the editor's philosophy: the popup NEVER takes
	focus (the Entry keeps it — that is also why a ttk.Combobox was not used;
	swapping the widget class would disturb the Tab trap and the field
	widget bookkeeping). Up/Down move the popup selection, Return accepts
	the highlighted row, Escape closes. Tab accepts ONLY after a deliberate
	arrow pick (see :attr:`has_user_pick`) — tabbing away from a merely
	open popup closes it and keeps the typed text. The value pool is polled
	via ``values()`` on every keystroke, so it can back onto a mutable
	app-level set that grows as the user types in new names.
	"""

	MAX_SHOWN = 12
	# Keys that must not (re)open the popup while editing.
	_NAV_KEYS = {"Up", "Down", "Left", "Right", "Return", "Escape", "Tab",
	             "Home", "End", "Prior", "Next", "Shift_L", "Shift_R",
	             "Control_L", "Control_R", "Alt_L", "Alt_R"}

	def __init__(self, entry, values) -> None:
		# ``values`` is a zero-arg callable returning the current pool (list).
		self.entry = entry
		self.values = values
		self.popup = None
		self.listbox = None
		self._matches: list[str] = []
		# Has the user actually NAVIGATED the popup (arrows)? The auto-
		# highlighted first row is not a pick — Tab must not silently accept
		# it, or tabbing away from the field rewrites the typed value with
		# whatever the pool happens to start with (real complaint: fixing a
		# series name, Tab, and the field jumps to a longer suggestion that
		# merely shares the prefix).
		self._picked = False
		entry.bind("<KeyRelease>", self._on_key, add="+")
		entry.bind("<Down>", self._on_arrow, add="+")
		entry.bind("<Up>", self._on_arrow, add="+")
		entry.bind("<Return>", self._on_return, add="+")
		entry.bind("<Escape>", lambda _e: self.hide() or "break", add="+")
		entry.bind("<FocusOut>", self._on_focus_out, add="+")

	def _on_focus_out(self, _event=None) -> None:
		# Delayed teardown: clicking a suggestion first moves focus out of the
		# Entry, and the popup must survive long enough for the click's
		# ButtonRelease to accept the highlighted row.
		if self.popup is not None:
			try:
				self.entry.after(150, self.hide)
			except Exception:  # noqa: BLE001
				self.hide()

	@property
	def is_open(self) -> bool:
		return self.popup is not None

	@property
	def has_user_pick(self) -> bool:
		"""Did the user deliberately navigate the popup (arrow keys)?

		Tab accepts the highlighted row ONLY then; without a pick the popup
		is just closed, leaving the typed value intact. Return and a click
		accept unconditionally — they ARE the explicit confirmation.
		"""
		return self._picked

	def _on_key(self, event) -> None:
		if (event.keysym or "") in self._NAV_KEYS or event.state & 0x4:  # Ctrl held
			return
		self._refresh()

	def _refresh(self) -> None:
		text = self.entry.get().strip().lower()
		if not text:
			self.hide()
			return
		self._picked = False  # rebuilt list: the row-0 highlight is not a pick
		self._matches = [
			v for v in self.values()
			if v.lower().startswith(text) and v.lower() != text
		][: self.MAX_SHOWN]
		if not self._matches:
			self.hide()
			return
		if self.popup is None:
			self.popup = tk.Toplevel(self.entry)
			self.popup.overrideredirect(True)
			self.listbox = tk.Listbox(
				self.popup, activestyle="dotbox",
				height=min(len(self._matches), self.MAX_SHOWN))
			self.listbox.pack(fill="both", expand=True)
			self.listbox.bind("<ButtonRelease-1>", self._on_click)
		else:
			assert self.listbox is not None
			self.listbox.delete(0, "end")
			self.listbox.configure(height=min(len(self._matches), self.MAX_SHOWN))
		for v in self._matches:
			self.listbox.insert("end", v)
		self.listbox.activate(0)
		self.listbox.selection_clear(0, "end")
		self.listbox.selection_set(0)
		# Place the dropdown right under the Entry (best effort; clamp to screen).
		x = self.entry.winfo_rootx()
		y = self.entry.winfo_rooty() + self.entry.winfo_height() + 2
		w = max(self.entry.winfo_width(), 180)
		h = self.listbox.winfo_reqheight()
		sw = self.entry.winfo_screenwidth()
		sh = self.entry.winfo_screenheight()
		self.popup.wm_geometry(f"{w}x{h}+{min(max(x, 0), sw - w)}+{min(y, sh - h)}")

	def _on_arrow(self, event) -> str | None:
		if self.popup is None or self.listbox is None or not self._matches:
			return None
		self._picked = True  # a deliberate move through the suggestions
		delta = 1 if event.keysym == "Down" else -1
		n = len(self._matches)
		i = self.listbox.index("active")
		i = (i + delta) % n
		self.listbox.activate(i)
		self.listbox.selection_clear(0, "end")
		self.listbox.selection_set(i)
		self.listbox.see(i)
		return "break"  # keep the Entry cursor put while browsing suggestions

	def _on_return(self, _event) -> str | None:
		if self.is_open:
			self.accept()
			return "break"
		return None

	def _on_click(self, _event) -> None:
		self.accept()

	def accept(self) -> None:
		"""Write the highlighted suggestion into the Entry and close."""
		if self.listbox is None:
			return
		i = self.listbox.index("active")
		if 0 <= i < len(self._matches):
			self.entry.delete(0, "end")
			self.entry.insert(0, self._matches[i])
			self.entry.icursor("end")
		self.hide()

	def hide(self) -> None:
		self._picked = False
		if self.popup is not None:
			try:
				self.popup.destroy()
			except Exception:  # noqa: BLE001
				pass
			self.popup = None
			self.listbox = None
			self._matches = []


class _BookList:
	"""Virtualized canvas book list with a Treeview-like API.

	Ttk's Treeview can only render per-row images in the #0 tree column,
	which Tk pins to the LEFT edge — the wanted layout (label always on the
	left, cover thumbnail flush against the right edge) is impossible there.
	Verified empirically on Tk 8.6: a PhotoImage put into a data column's
	``values`` renders as its Tcl name (``pyimage1``), i.e. as text.

	Exposes just the surface the editor uses (insert/delete/get_children/
	selection_set/selection_get/selection_toggle/selection_extend/focus/
	see/exists/identify_row/bind/yview/configure), so the rest of the GUI
	keeps talking to ``self.tree`` unchanged. On top of the single focus
	row there is a MULTI-selection (Ctrl+click toggles, Shift+click extends
	from the anchor — the input of the bulk edit); rows selected beyond the
	focus draw with a lighter tint. Only the
	VISIBLE rows are drawn — the library holds ~5000 entries and thousands
	of canvas items would make both rebuilds and scrolling crawl; the
	scrollregion is virtual, derived from the row count.
	"""

	ROW_H = 54
	THUMB_W = 32
	THUMB_H = 48
	# Just the width of one coloured action glyph — the full word is no
	# longer drawn (the symbol frees the space for title/author).
	ACTION_W = 20
	PAD = 6
	HEADER_H = 24

	# Action → (glyph, colour): Tango palette, matching the selection blue.
	# Plain Unicode signs only — emoji (🗑, 📌) render as tofu/monochrome in
	# Tk's default DejaVu on this box.
	ACTION_GLYPHS = {
		"": ("·", "#888a85"),
		"accept": ("✔", "#4e9a06"),
		"delete": ("⌫", "#a40000"),
		"keep": ("◆", "#06989a"),
		"merge": ("⇥", "#75507b"),
	}
	AUTHOR_INDENT = 12

	def __init__(self, parent, style) -> None:
		self._rows: list[dict] = []
		self._by_iid: dict[str, dict] = {}
		self._selected: str | None = None
		# Multi-selection state: the selected iid list (focus included when
		# it is selected) and the anchor row Shift+click extends from.
		self._sel: list[str] = []
		self._anchor: str | None = None
		self._select_cb = None
		self._draw_pending = False
		self._pending_top: float | None = None
		self._font, self._bold, self._italic = self._resolve_fonts(style)
		self._bg, self._fg, self._sel_bg, self._sel_fg, self._sel_bg2, self._border = self._resolve_colors(style)
		self.canvas = tk.Canvas(
			parent, highlightthickness=0, background=self._bg, yscrollincrement=1,
		)
		self.canvas.bind("<Configure>", lambda _e: self._schedule_draw())
		self.canvas.bind("<Button-1>", self._on_click)
		self.canvas.bind("<MouseWheel>", self._on_wheel, add="+")
		self.canvas.bind("<Button-4>", self._on_wheel, add="+")
		self.canvas.bind("<Button-5>", self._on_wheel, add="+")

	@staticmethod
	def _resolve_fonts(style):
		import tkinter.font as tkfont
		try:
			name = style.lookup("Treeview", "font")
			font = tkfont.nametofont(name) if name else tkfont.nametofont("TkDefaultFont")
		except Exception:  # noqa: BLE001
			font = tkfont.nametofont("TkDefaultFont")
		bold = italic = font
		try:
			bold = font.copy()
			bold.configure(weight="bold")
			italic = font.copy()
			italic.configure(slant="italic")
		except Exception:  # noqa: BLE001
			pass
		return font, bold, italic

	@staticmethod
	def _mix_hex(c1: str, c2: str, t: float) -> str:
		"""Blend two ``#rrggbb`` colours (t=0 → c1, 1 → c2). *c1* on any parse
		failure — a theme may hand us a colour name instead of hex."""
		try:
			r1, g1, b1 = (int(c1[i:i + 2], 16) for i in (1, 3, 5))
			r2, g2, b2 = (int(c2[i:i + 2], 16) for i in (1, 3, 5))
		except (TypeError, ValueError):
			return c1
		return (f"#{round(r1 + (r2 - r1) * t):02x}"
		        f"{round(g1 + (g2 - g1) * t):02x}"
		        f"{round(b1 + (b2 - b1) * t):02x}")

	@classmethod
	def _resolve_colors(cls, style):
		try:
			bg = style.lookup("Treeview", "background") or "#ffffff"
			fg = style.lookup("Treeview", "foreground") or "#000000"
		except Exception:  # noqa: BLE001
			bg, fg = "#ffffff", "#000000"
		sel_bg, sel_fg = "#3465a4", "#ffffff"

		def _map_val(option, default):
			try:
				for statespec, value in style.map("Treeview", option):
					if "selected" in statespec and value:
						return value
			except Exception:  # noqa: BLE001
				pass
			return default

		sel_bg = _map_val("background", sel_bg)
		sel_fg = _map_val("foreground", sel_fg)
		# The multi-selection tint: the selection blue washed toward the row
		# background, so the focus row stays the strongest cue.
		sel_bg2 = cls._mix_hex(sel_bg, bg, 0.6)
		return bg, fg, sel_bg, sel_fg, sel_bg2, "#999999"

	# -- Treeview-like API --------------------------------------------------

	def insert(self, _parent, _index, *, iid=None, text="", values=(), image=None):
		# Two-line row: *text* is the TITLE (bold, first line); values are
		# (action, author, verified, series) — the author renders italic +
		# indented below with the series + order right-aligned beside it,
		# verified draws a blue ✓ under the action glyph.
		row = {
			"iid": str(iid), "title": text,
			"action": values[0] if values else "",
			"author": values[1] if len(values) > 1 else "",
			"verified": bool(values[2]) if len(values) > 2 else False,
			"series": values[3] if len(values) > 3 else "",
			"image": image,
		}
		self._rows.append(row)
		self._by_iid[row["iid"]] = row
		self._update_scrollregion()
		self._schedule_draw()

	def delete(self, *iids):
		if not iids:
			iids = tuple(r["iid"] for r in self._rows)
		# Bank the current view offset — the rows are about to be rebuilt
		# (refresh_list pattern) and the interim empty list would otherwise
		# clamp the canvas to the top. Closed by the batch's idle draw.
		if self._pending_top is None:
			self._pending_top = self.canvas.canvasy(0)
		for iid in iids:
			row = self._by_iid.pop(iid, None)
			if row is not None and row in self._rows:
				self._rows.remove(row)
		if self._selected not in self._by_iid:
			self._selected = None
		self._sel = [i for i in self._sel if i in self._by_iid]
		if self._anchor not in self._by_iid:
			self._anchor = None
		self._update_scrollregion()
		self._schedule_draw()

	def get_children(self, *_parent) -> tuple:
		return tuple(r["iid"] for r in self._rows)

	def exists(self, iid) -> bool:
		return iid in self._by_iid

	def selection_set(self, iid, *, silent: bool = False) -> None:
		"""Plain click: single selection, focus and anchor move to *iid*."""
		if iid not in self._by_iid:
			return
		self._sel = [iid]
		self._anchor = iid
		self._set_focus_iid(iid, silent=silent)

	def selection_toggle(self, iid, *, silent: bool = False) -> None:
		"""Ctrl+click: flip one row's membership; focus and anchor follow."""
		if iid not in self._by_iid:
			return
		if iid in self._sel:
			self._sel.remove(iid)
		else:
			self._sel.append(iid)
		self._anchor = iid
		self._set_focus_iid(iid, silent=silent)

	def selection_extend(self, iid, *, silent: bool = False) -> None:
		"""Shift+click: select the row range from the anchor to *iid*."""
		if iid not in self._by_iid:
			return
		a = self._row_index(self._anchor) if self._anchor in self._by_iid else None
		b = self._row_index(iid)
		if a is None or b is None:
			self.selection_set(iid, silent=silent)
			return
		lo, hi = min(a, b), max(a, b)
		self._sel = [r["iid"] for r in self._rows[lo:hi + 1]]
		self._set_focus_iid(iid, silent=silent)

	def selection_get(self) -> tuple:
		"""The selected iids (focus first, then the rest in pick order)."""
		if self._selected and self._selected in self._sel:
			return (self._selected, *(i for i in self._sel if i != self._selected))
		return tuple(self._sel)

	def preserve_selection(self, focus_iid: str, keep=()) -> None:
		"""After a full rebuild (refresh_list): keep the selection that is
		still VISIBLE.

		Rows hidden by the list filter are rebuilt out of the widget, so
		their iids leave the selection — what you SEE selected is what a
		bulk edit gets. *keep* is the pre-rebuild selection banked by the
		caller (delete() prunes against the interim empty widget). The
		focus row is restored silently: firing <<TreeviewSelect>> on a
		background refresh would reload the detail pane and wipe unsaved
		field edits.
		"""
		merged = list(keep) + [i for i in self._sel if i not in keep]
		self._sel = [i for i in merged if i in self._by_iid]
		if self._anchor not in self._by_iid:
			self._anchor = None
		# The focus is NOT force-added to the selection: a row the user
		# Ctrl+clicked OFF stays out — the focus only says which book the
		# detail pane shows.
		if focus_iid in self._by_iid:
			self._selected = focus_iid
		self._schedule_draw()

	def _row_index(self, iid) -> int | None:
		row = self._by_iid.get(iid)
		return self._rows.index(row) if row is not None else None

	def _set_focus_iid(self, iid, *, silent: bool = False) -> None:
		changed = iid != self._selected
		self._selected = iid
		if changed:
			self._schedule_draw()
			# Fire the select callback only on an actual CHANGE — like the
			# Treeview's <<TreeviewSelect>>, which stays quiet when the same
			# row is re-selected. ``silent`` skips the callback altogether:
			# used by refresh_list (re-selecting the preserved row after a
			# rebuild) and by _step (which loads the book explicitly) — a
			# callback there would reload the detail pane and wipe the
			# user's unsaved edits on every background thumbnail refresh.
		if changed and not silent and self._select_cb is not None:
			self._select_cb()

	def focus(self, iid=None):
		if iid is None:
			return self._selected or ""
		if iid in self._by_iid:
			self._selected = iid
			self._schedule_draw()

	def see(self, iid) -> None:
		row = self._by_iid.get(iid)
		if row is None:
			return
		y = self.HEADER_H + self._rows.index(row) * self.ROW_H
		total = self.HEADER_H + len(self._rows) * self.ROW_H
		view_h = self.canvas.winfo_height()
		top = self.canvas.canvasy(0)
		if y < top:
			self.canvas.yview_moveto(y / max(total, 1))
		elif y + self.ROW_H > top + view_h and total > view_h:
			self.canvas.yview_moveto((y + self.ROW_H - view_h) / max(total, 1))
		self._draw()

	def identify_row(self, y) -> str:
		"""Widget-y → iid ("" when outside rows) — Treeview's signature."""
		cy = self.canvas.canvasy(y) - self.HEADER_H
		i = int(cy // self.ROW_H)
		if cy >= 0 and 0 <= i < len(self._rows):
			return self._rows[i]["iid"]
		return ""

	def bind(self, sequence, func=None, add=None):
		if sequence == "<<TreeviewSelect>>":
			self._select_cb = func
			return
		if func is None and add is None:
			# Query mode — canvas.bind(seq, None, None) does NOT query (it
			# returns None on this tkinter), delegate a bare query instead.
			return self.canvas.bind(sequence)
		self.canvas.bind(sequence, func, add)

	def yview(self, *args):
		out = self.canvas.yview(*args)
		if args:
			self._draw()
		return out

	def yview_moveto(self, fraction) -> None:
		self.canvas.yview_moveto(fraction)
		self._draw()

	def configure(self, **kw):
		self.canvas.configure(**kw)

	# -- Internals ----------------------------------------------------------

	def _update_scrollregion(self) -> None:
		h = self.HEADER_H + len(self._rows) * self.ROW_H
		# A rebuild (refresh_list: delete-all + re-inserts) shrinks the
		# region to HEADER_H first, which makes Tk clamp the view to 0 —
		# after that canvasy(0) is useless for restoring. The offset is
		# therefore banked in _pending_top at delete() and re-applied after
		# every region change until the batch's idle draw closes it.
		top = self._pending_top if self._pending_top is not None else self.canvas.canvasy(0)
		self.canvas.configure(scrollregion=(0, 0, 2500, h))
		if top:
			# NB: a CANVAS yview_moveto fraction is relative to the TOTAL
			# content height (unlike a Text widget, where it is relative to
			# the scrollable span) — divide by h, not by (h - view height).
			self.canvas.yview_moveto(min(top / max(h, 1), 1.0))

	def _schedule_draw(self) -> None:
		# Coalesce: refresh_list re-inserts EVERY row; drawing per insert
		# would render the whole list N times over. One idle callback after
		# the burst = one repaint.
		if self._draw_pending:
			return
		self._draw_pending = True
		self.canvas.after_idle(self._draw_idle)

	def _draw_idle(self) -> None:
		self._draw_pending = False
		self._pending_top = None  # rebuild batch finished
		self._draw()

	def _on_click(self, event):
		iid = self.identify_row(event.y)
		if iid:
			# Ctrl toggles one row into the multi-selection, Shift extends
			# it from the anchor — the input side of the bulk edit.
			if event.state & 0x0001 and self._anchor is not None:  # Shift
				self.selection_extend(iid)
			elif event.state & 0x0004:  # Control
				self.selection_toggle(iid)
			else:
				self.selection_set(iid)
		return "break"

	def _on_wheel(self, event):
		if event.num == 4 or (getattr(event, "delta", 0) or 0) > 0:
			self.canvas.yview_scroll(-self.ROW_H, "units")
		else:
			self.canvas.yview_scroll(self.ROW_H, "units")
		self._draw()
		return "break"

	def _elide(self, text: str, avail: int, font=None) -> str:
		"""Trim *text* to *avail* px with an ellipsis (bisection)."""
		font = font or self._font
		if avail <= 4 or font.measure(text) <= avail:
			return text
		lo, hi = 0, len(text)
		while lo < hi:
			mid = (lo + hi) // 2
			if font.measure(text[:mid] + "…") <= avail:
				lo = mid + 1
			else:
				hi = mid
		return text[: max(lo - 1, 0)] + "…"

	def _draw(self) -> None:
		c = self.canvas
		c.delete("all")
		w = c.winfo_width()
		if w < 10:  # not laid out yet
			return
		ax = w - self.PAD - self.THUMB_W - 12 - self.ACTION_W  # action glyph x
		thumb_x = w - self.PAD - self.THUMB_W
		text_w = ax - 12 - self.PAD  # shared title/author width budget
		# Header (matches the old Treeview headings).
		c.create_text(self.PAD, self.HEADER_H // 2, anchor="w",
		              text=_("Title"), font=self._bold, fill=self._fg)
		c.create_line(0, self.HEADER_H, w, self.HEADER_H, fill=self._border)
		# Visible slice only — the virtualized part.
		first = max(0, int((c.canvasy(0) - self.HEADER_H) // self.ROW_H))
		last = min(len(self._rows),
		           int((c.canvasy(0) + c.winfo_height()) // self.ROW_H) + 2)
		for i in range(first, last):
			row = self._rows[i]
			y = self.HEADER_H + i * self.ROW_H
			cy = y + self.ROW_H // 2
			sel = row["iid"] == self._selected
			in_sel = not sel and row["iid"] in self._sel
			if sel or in_sel:
				c.create_rectangle(0, y, w, y + self.ROW_H,
				                   fill=self._sel_bg if sel else self._sel_bg2,
				                   outline="")
			fg = self._sel_fg if sel else self._fg
			# Line 1: title, bold, from the left edge. Line 2: author,
			# italic, indented — the thumbnail already fixes the row height,
			# so two lines fit at no cost. A present series + order shares
			# line 2 from the RIGHT (before the action column): the author
			# budget shrinks by its width, and the series is itself capped
			# at half the line so it can never erase the author.
			c.create_text(self.PAD, y + 6, anchor="nw",
			              text=self._elide(row["title"], text_w, self._bold),
			              font=self._bold, fill=fg)
			line2_x = self.PAD + self.AUTHOR_INDENT
			line2_end = ax - 10
			if row["series"]:
				s_text = self._elide(row["series"],
				                     (line2_end - line2_x) // 2, self._font)
				c.create_text(line2_end, y + self.ROW_H - 6, anchor="se",
				              text=s_text, font=self._font, fill=fg)
				line2_end -= self._font.measure(s_text) + 12
			c.create_text(line2_x, y + self.ROW_H - 6, anchor="sw",
			              text=self._elide(row["author"] or "—",
			                               line2_end - line2_x, self._italic),
			              font=self._italic, fill=fg)
			# Action: coloured glyph (colour stays even when selected — it
			# is the orientation cue, and the Tango colours read fine on
			# the selection blue). A verified book stacks a blue ✓ under
			# the action glyph (the two together say "decided AND OK").
			glyph, colour = self.ACTION_GLYPHS.get(row["action"],
			                                      self.ACTION_GLYPHS[""])
			if row.get("verified"):
				c.create_text(ax, cy - 9, anchor="w", text=glyph,
				              font=self._bold, fill=colour)
				c.create_text(ax, cy + 13, anchor="w", text="✓",
				              font=self._bold, fill="#204a87")
			else:
				c.create_text(ax, cy, anchor="w", text=glyph,
				              font=self._bold, fill=colour)
			if row["image"] is not None:
				c.create_image(thumb_x, y + (self.ROW_H - self.THUMB_H) // 2,
				               anchor="nw", image=row["image"])
			if i + 1 < len(self._rows):
				line_bg = self._sel_bg if sel else (self._sel_bg2 if in_sel else self._border)
				c.create_line(0, y + self.ROW_H, w, y + self.ROW_H, fill=line_bg)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_gui(cfg) -> None:
	"""Launch the review.yaml editor window. Blocks until the window closes."""
	if tk is None:  # pragma: no cover
		raise RuntimeError(
			"Tkinter is not available. Install the Tk bindings "
			'(Debian/Ubuntu: "sudo apt install python3-tk").'
		)
	app = ReviewEditorApp(cfg)
	app.root.mainloop()


# ---------------------------------------------------------------------------
# The editor
# ---------------------------------------------------------------------------


class ReviewEditorApp:
	"""The review.yaml editor window and all its behaviour."""

	# Action radio values; "pending" serialises to ``action: null``.
	ACTIONS = ["pending", "accept", "delete", "keep", "merge"]

	# Cover-preview slot (px): every preview cell in a row occupies the SAME
	# box whether the image is present, smaller, or missing, so the cells stay
	# aligned side by side. The width is synced per-row to the available pane
	# width (between MIN and W) — a purely fixed width overflows a narrow pane
	# and pack then squeezes the trailing cells out of shape.
	COVER_SLOT_W = 248
	COVER_SLOT_MIN = 140
	COVER_SLOT_H = 330

	def __init__(self, cfg) -> None:
		self.cfg = cfg
		self.review_path = Path(cfg.review_file)
		self.library = Path(cfg.library)

		self.entries: list[dict] = []
		self._cur = -1  # index into self.entries currently shown
		self._dirty = False
		self._alive = True
		self._loading = False  # suppress dirty while populating fields
		self._last_field_role: str | None = None  # focus persistence across books
		self._flash_after_id = None  # pending transient status-line message

		# Cover thumbnail caches (uuid -> image). PIL loaded off-thread;
		# PhotoImage created lazily in the main thread (Tk is not thread-safe).
		self._thumbs_pil: dict = {}
		self._thumbs_photo: dict = {}

		self.root = tk.Tk()
		self.root.title("bmf — review.yaml editor")
		self.root.geometry("1320x860")
		self.root.protocol("WM_DELETE_WINDOW", self.quit_app)

		if not self.review_path.is_file():
			messagebox.showerror("bmf gui", _("review file not found:\n{path}").format(path=self.review_path))
			self.root.destroy()
			return
		try:
			self.entries = _load_raw_entries(self.review_path)
		except Exception as e:  # noqa: BLE001
			messagebox.showerror("bmf gui", _("failed to parse {path}:\n{err}").format(path=self.review_path, err=e))
			self.root.destroy()
			return

		# Autocomplete pools for author/series: seeded synchronously from the
		# review entries themselves (instant), then widened by the startup
		# library index sweep (see _finish_lib_index). New names the user
		# types are learned back into the pools (see _vocab_learn).
		self._vocab_authors: set[str] = set()
		self._vocab_series: set[str] = set()
		self._acs: dict = {}  # Entry widget -> its _Autocomplete
		for e in self.entries:
			for src in (e.get("current") or {}, e.get("proposed") or {}):
				a = src.get("author")
				if isinstance(a, str) and a.strip():
					self._vocab_authors.add(a.strip())
				for x in src.get("authors") or []:
					if isinstance(x, str) and x.strip():
						self._vocab_authors.add(x.strip())
				s = src.get("series")
				items = [s] if isinstance(s, (str, dict)) else (s if isinstance(s, list) else [])
				for item in items:
					name = item.get("name") if isinstance(item, dict) else item
					if isinstance(name, str) and name.strip():
						self._vocab_series.add(name.strip())

		# Filter / search state.
		self._filter_action = tk.StringVar(value="all")
		self._filter_category = tk.StringVar(value="all")
		self._search = tk.StringVar()
		self._search.trace_add("write", lambda *_: self._on_search_changed())
		# "+ library" mode: the search also matches the WHOLE library (books
		# outside review.yaml merge into the list). One background sweep at
		# startup builds a fulltext index (_lib_index: (entry, haystack)
		# pairs) so every search is an instant in-memory filter — no NFS per
		# search. Merged entries (tracked in _lib_uuids: uuid → its haystack,
		# used by the list filter's exemption) stay in memory for the whole
		# session; only CHANGED ones are written into review.yaml on save.
		self._lib_mode = tk.BooleanVar(value=False)
		self._lib_mode.trace_add("write", lambda *_: self._on_lib_mode_changed())
		self._lib_uuids: dict = {}
		self._lib_index: list = []
		self._lib_index_done = False
		self._lib_indexing = False
		self._lib_search_after = None  # pending debounced serve (after id)

		# Action / notes / verified state.
		self._action_var = tk.StringVar(value="pending")
		self._notes_var = tk.StringVar()
		self._verified_var = tk.BooleanVar(value=False)
		self._action_var.trace_add("write", lambda *_: self._on_action_changed())
		self._notes_var.trace_add("write", lambda *_: self._mark_dirty())
		self._verified_var.trace_add("write", lambda *_: self._on_verified_changed())

		# Per-field widgets (checkbutton / RO label / copy btn / target Entry).
		self._fields: dict[str, dict] = {}
		self._field_entries: list = []  # target Entries in Tab-traversal order
		self._editable_widgets: list = []  # field entries (the Tab cycle)

		# Cover state.
		self._del_cover = tk.BooleanVar(value=False)
		self._del_bak = tk.BooleanVar(value=False)
		self._del_formats: dict[str, object] = {}  # format file path -> BooleanVar
		self._cover_photos: dict = {}  # keep PhotoImage refs alive
		self._cover_imgs: list = []
		self._cover_caps: list = []

		# Content state. The preview always loads the WHOLE book text,
		# progressively: stream_full_text() yields chunks as they are obtained
		# and the worker marshals each onto the Tk thread (the old
		# first-page/broader toggle is gone — a 30k window was the wrong tool
		# for reading). `_content_gen` invalidates in-flight streams when the
		# book or format changes mid-flight. The _format_var trace is what
		# makes a format-radio CLICK load that format (see _refresh_formats).
		self._format_var = tk.StringVar()
		self._format_var.trace_add("write", lambda *_: self._on_format_changed())
		self._content_gen = 0
		self._content_path: str | None = None
		self._content_pieces: list[str] = []  # chunks of the current stream
		self._format_files: list = []
		self._recode_var = tk.BooleanVar(value=False)
		self._recode_from = tk.StringVar(value="cp1250")
		self._recode_to = tk.StringVar(value="utf-8")
		self._content_raw = ""
		self._content_repaired: str | None = None
		self._help_win: tk.Toplevel | None = None  # category-help popup

		# List cover hover-popup state.
		self._big_thumbs: dict = {}  # uuid -> PIL or None (on-demand, capped)
		self._cover_popup = None
		self._cover_popup_photo = None
		self._hover_after = None
		self._hover_uuid: str | None = None

		self._setup_style()
		self._build_ui()
		self._install_tab_trap()
		self._bind_shortcuts()
		self.refresh_list()
		self._start_thumb_loader()
		self._start_lib_index()

		# Load the first book and focus its first field (start focus rule).
		if self.entries:
			# Highlight row 0 silently — _select_index below loads it anyway;
			# without this the list starts with NO visible indicator.
			self.tree.selection_set("0", silent=True)
			self._select_index(0, keep_focus=False)
			self.root.after(50, self._focus_first_field)

	# ------------------------------------------------------------------
	# UI construction
	# ------------------------------------------------------------------

	def _setup_style(self) -> None:
		"""Keep the platform's default ttk theme; sync the tk.Text palette to it.

		Deliberately platform-independent: no GTK/theme emulation, whatever
		look the platform's Tk picks is the look we use. The only styling is
		consistency — Tk's ``tk.Text`` widgets default to a hard white
		background that looks like a bright island next to the ttk widgets,
		so they borrow the ttk ``TEntry`` field colours.
		"""
		# master=self.root — a bare ttk.Style() would bind to the interpreter's
		# DEFAULT root (the first Tk created), styling the wrong window.
		self._style = ttk.Style(self.root)
		self._field_bg = self._style.lookup("TEntry", "fieldbackground") or "#ffffff"
		self._field_fg = self._style.lookup("TEntry", "foreground") or "#000000"
		self._form_bg = self._style.lookup("TFrame", "background") or "#d9d9d9"
		try:
			self.root.configure(background=self._form_bg)
		except Exception:  # noqa: BLE001
			pass

	def _style_text(self, widget):
		"""Apply the synced ttk palette to a ``tk.Text`` (returns it)."""
		try:
			widget.configure(background=self._field_bg, foreground=self._field_fg,
			                 highlightthickness=0, insertbackground=self._field_fg)
		except Exception:  # noqa: BLE001
			pass
		return widget

	def _build_ui(self) -> None:
		paned = ttk.Panedwindow(self.root, orient="horizontal")
		paned.pack(fill="both", expand=True)
		self._build_left_panel(paned)
		self._build_right_panel(paned)
		self._build_status_bar()

	def _build_left_panel(self, parent) -> None:
		frame = ttk.LabelFrame(parent, text=_("List"))
		# Filters row.
		filt = ttk.Frame(frame)
		filt.pack(fill="x", padx=6, pady=4)
		ttk.Label(filt, text=_("Action:")).pack(side="left")
		self._action_combo = ttk.Combobox(
			filt, textvariable=self._filter_action, state="readonly", width=9,
			values=["all", "pending", "accept", "delete", "keep", "merge", "verified"],
		)
		self._action_combo.pack(side="left", padx=(2, 8))
		self._action_combo.bind("<<ComboboxSelected>>", lambda *_: self.refresh_list())
		ttk.Label(filt, text=_("Category:")).pack(side="left")
		self._cat_combo = ttk.Combobox(
			filt, textvariable=self._filter_category, state="readonly", width=10,
		)
		self._cat_combo.pack(side="left", padx=(2, 8))
		self._cat_combo.bind("<<ComboboxSelected>>", lambda *_: self.refresh_list())
		ttk.Label(filt, text=_("Search:")).pack(side="left")
		self._search_entry = ttk.Entry(filt, textvariable=self._search, width=22)
		self._search_entry.pack(side="left", fill="x", expand=True)
		self._bind_select_all(self._search_entry)
		# "+ library": extend the search to the whole library (see
		# _on_lib_mode_changed) — books outside review.yaml join the list.
		self._lib_chk = ttk.Checkbutton(filt, text=_("+ library"), variable=self._lib_mode)
		self._lib_chk.pack(side="left", padx=(8, 0))
		_Tooltip(self._lib_chk, _(
			"Search the whole library too, not only review.yaml: books matching "
			"the search (author, title, series, folder path, annotation) that "
			"are not in review appear in the list — their header says 'not in "
			"review.yaml'. A book you change or decide is added into "
			"review.yaml on save (bmf apply then processes it); unchanged "
			"ones are not saved. A fulltext index of the library is built in "
			"the background at startup (progress in the status line); the "
			"search answers instantly once it is ready."))

		# Book list — canvas-rendered (_BookList), NOT ttk.Treeview: the
		# wanted row layout is label left + cover flush right, and Treeview
		# can only show per-row images in its leftmost #0 column (verified
		# empirically — see the _BookList docstring).
		tree_frame = ttk.Frame(frame)
		tree_frame.pack(fill="both", expand=True, padx=6, pady=(0, 4))
		self.tree = _BookList(tree_frame, self._style)
		self.tree.canvas.pack(side="left", fill="both", expand=True)
		vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
		self.tree.configure(yscrollcommand=vsb.set)
		vsb.pack(side="right", fill="y")
		self.tree.bind("<<TreeviewSelect>>", lambda *_: self._on_tree_select())
		# Hover popup: show a larger cover near the cursor when the pointer
		# lingers on a row that has a thumbnail.
		self.tree.bind("<Motion>", self._on_tree_motion, add="+")
		self.tree.bind("<Leave>", self._on_tree_leave, add="+")
		self.tree.bind("<Double-1>", self._on_tree_double, add="+")

		# Bulk edit of the multi-selection (Ctrl/Shift+click) — see bulk_edit.
		bulk = ttk.Frame(frame)
		bulk.pack(fill="x", padx=6, pady=(0, 4))
		self._bulk_btn = ttk.Button(bulk, text=_("Bulk edit (Ctrl+E)"), command=self.bulk_edit)
		self._bulk_btn.pack(side="left")
		_Tooltip(self._bulk_btn, _(
			"Set the author or series for all selected books at once.\n"
			"Select rows with Ctrl+click (toggle) and Shift+click (range);\n"
			"the value lands in each book's proposal (a pending book becomes\n"
			"accept, a decided one keeps its action)."))

		parent.add(frame, weight=1)

	def _build_right_panel(self, parent) -> None:
		frame = ttk.Frame(parent)
		self._detail_frame = frame
		# RESPONSIVE detail split: WIDE detail = the scrollable review form
		# LEFT + the content preview RIGHT across the full pane height
		# (screens are wider than tall — a tall reading pane is exactly what
		# the extra width is for); NARROW detail = the form on TOP + the
		# preview at the BOTTOM. ttk's Panedwindow -orient is READ-ONLY
		# ("dynamic oriention changes NYI" in the Tk source) and Tk cannot
		# reparent a widget, so switching orientation means DESTROYING the
		# Panedwindow and building a new one — which is only survivable
		# because the panes are NOT its children but its SIBLINGS (children
		# of this frame): everything built inside them (the whole form, the
		# streaming preview, half-typed edits) survives the switch. ttk's
		# content manager explicitly allows managing a sibling ("container
		# is a descendant of the window's parent"), and the Panedwindow
		# sits at (0,0) filling the frame, so pane coordinates line up.
		self._form_pane = ttk.Frame(frame)
		self._content_pane = ttk.Frame(frame)
		self._cols = None
		self._detail_orient = None

		# -- form column: the scrollable detail canvas --------------------
		form_pane = self._form_pane
		# Scrollable detail column: Canvas + inner frame + scrollbar.
		self.canvas = tk.Canvas(form_pane, highlightthickness=0, width=720)
		try:
			self.canvas.configure(
				background=self._style.lookup("TFrame", "background") or "#d9d9d9"
			)
		except Exception:  # noqa: BLE001
			pass
		vsb = ttk.Scrollbar(form_pane, orient="vertical", command=self.canvas.yview)
		self._form_vsb = vsb
		self.canvas.configure(yscrollcommand=vsb.set)
		vsb.pack(side="right", fill="y")
		self.canvas.pack(side="left", fill="both", expand=True)
		self._scroll_inner = ttk.Frame(self.canvas)
		self._inner_win = self.canvas.create_window((0, 0), window=self._scroll_inner, anchor="nw")
		self.canvas.configure(borderwidth=0)
		# Keep the inner frame as wide as the canvas (so widgets span it).
		self.canvas.bind("<Configure>", self._on_canvas_configure, add="+")
		self._scroll_inner.bind("<Configure>", self._on_inner_configure, add="+")

		self._header_lbl = ttk.Label(self._scroll_inner, text="", anchor="w", justify="left")
		self._header_lbl.pack(fill="x", padx=8, pady=(6, 2))
		# The book's folder path as a clickable link ("open in file manager"
		# without reaching for a terminal). Blue + underlined over the theme
		# default; the launch itself is delegated to the platform opener.
		self._path_link = ttk.Label(
			self._scroll_inner, text="", anchor="w", cursor="hand2",
			foreground="#1a5fb4", wraplength=1200,
		)
		self._path_link.pack(fill="x", padx=8, pady=(0, 2))
		self._path_link.bind("<Button-1>", lambda _e: (self.open_current_folder(), "break")[1])
		_Tooltip(self._path_link, _("Open the book's folder in the file manager"))
		try:
			import tkinter.font as tkfont
			# NB: keep a reference — tkinter's Font.__del__ DELETES the Tcl
			# font when the Python wrapper is GC'd (a local variable dies as
			# soon as this builder returns, silently un-underlining the link).
			self._link_font = tkfont.nametofont("TkDefaultFont").copy()
			self._link_font.configure(underline=True)
			self._path_link.configure(font=self._link_font)
		except Exception:  # noqa: BLE001
			pass
		self._build_problems_section()
		self._build_fields_section()
		self._build_merge_section()
		self._build_covers_section()

		# -- content column: the text preview ------------------------------
		self._build_content_section(self._content_pane)

		# The stacked (narrow) layout's ONE big scrollbar: spans the whole
		# detail column and CHAINS the two scroll areas — the form canvas
		# first, then the whole book text below it (see _chain_scroll). The
		# native per-widget scrollbars hide while it is out.
		self._chain_vsb = ttk.Scrollbar(frame, orient="vertical", command=self._chain_scroll)
		# Keep the chained thumb in sync with either area's size/content.
		self.canvas.bind("<Configure>", lambda _e: self._chain_update(), add="+")
		self._content_txt.bind("<Configure>", lambda _e: self._chain_update(), add="+")

		self._apply_detail_orient("horizontal")
		# Follow the actual width: a narrow window (or the outer list sash
		# dragged wide) restacks the preview below the form.
		frame.bind("<Configure>", self._on_detail_configure, add="+")
		parent.add(frame, weight=3)

	def _apply_detail_orient(self, orient: str) -> None:
		"""Rebuild the detail Panedwindow in the given orientation.

		The pane frames are SIBLINGS of the Panedwindow (see
		_build_right_panel), so this destroys and recreates only the thin
		splitter widget — the form, the preview and their state survive.
		The stacked layout additionally swaps the two native scrollbars for
		ONE big chained scrollbar (form first, then the whole text).
		"""
		if orient == self._detail_orient:
			return
		if self._chain_vsb is not None:
			self._chain_vsb.pack_forget()
		if self._cols is not None:
			self._cols.destroy()
		self._cols = ttk.Panedwindow(self._detail_frame, orient=orient)
		self._cols.add(self._form_pane, weight=3)
		self._cols.add(self._content_pane, weight=2)
		if orient == "vertical":
			# Claim the right edge BEFORE the split, so the one scrollbar
			# spans both stacked areas.
			self._chain_vsb.pack(side="right", fill="y")
			self._form_vsb.pack_forget()
			self._content_vsb.pack_forget()
			self.canvas.configure(yscrollcommand=self._chain_update)
			self._content_txt.configure(yscrollcommand=self._chain_update)
		else:
			# Repack with the scrollbars FIRST: pack allocates in order, so
			# a scrollbar appended AFTER its (wide-requesting) client gets
			# squeezed to nothing — the form canvas requests 720 px and
			# starves a trailing vsb whenever the pane is narrower.
			self.canvas.pack_forget()
			self._content_txt.pack_forget()
			self._form_vsb.pack(side="right", fill="y")
			self.canvas.pack(side="left", fill="both", expand=True)
			self._content_vsb.pack(side="right", fill="y")
			self._content_txt.pack(side="left", fill="both", expand=True)
			self.canvas.configure(yscrollcommand=self._form_vsb.set)
			self._content_txt.configure(yscrollcommand=self._content_vsb.set)
		self._cols.pack(fill="both", expand=True)
		# The fresh Panedwindow is the youngest child of the frame and would
		# stack above the panes; lift them back on top. The sashes stay
		# exposed — the panes never cover the sash gaps between them.
		self._form_pane.lift(self._cols)
		self._content_pane.lift(self._cols)
		self._detail_orient = orient
		self._chain_update()

	def _chain_state(self):
		"""Virtual pixel geometry of the stacked detail as ONE scroll flow.

		Returns (form_range, form_pos, text_range, text_pos, form_view,
		text_total) — the scrollable overflow of the form canvas and of the
		preview text plus the current positions, or None when unreachable.
		Both widgets' yview fractions span their WHOLE content (the canvas's
		scrollregion, the text's body), so each total height is estimated
		from the visible fraction — a uniform, widget-honest conversion
		(good enough for a thumb; wrapped line heights vary anyway).
		"""
		try:
			form_view = max(1, self.canvas.winfo_height())
			a0, a1 = self.canvas.yview()
			visible_a = max(1e-9, a1 - a0)
			form_total = form_view / visible_a
			a_range = max(0.0, form_total - form_view)
			a_pos = a0 * form_total
			f0, f1 = self._content_txt.yview()
			text_view = max(1, self._content_txt.winfo_height())
			visible_frac = max(1e-9, f1 - f0)
			text_total = text_view / visible_frac
			b_range = max(0.0, text_total - text_view)
			b_pos = f0 * text_total
		except Exception:  # noqa: BLE001
			return None
		return a_range, a_pos, b_range, b_pos, form_view, text_total

	def _chain_update(self, *_args) -> None:
		"""Sync the chained scrollbar's thumb from both areas' state."""
		if self._detail_orient != "vertical" or self._chain_vsb is None:
			return
		st = self._chain_state()
		if st is None:
			return
		a_range, a_pos, b_range, b_pos, form_view, text_total = st
		total = a_range + b_range
		if total <= 0:
			self._chain_vsb.set(0.0, 1.0)
			return
		pos = a_pos + b_pos
		# The thumb covers whichever area the viewport is currently "in".
		view = form_view if a_pos < a_range - 1 else (text_total - b_range)
		self._chain_vsb.set(pos / total, min(1.0, (pos + view) / total))

	def _chain_scroll(self, action: str, *args) -> None:
		"""The chained scrollbar's command: form FIRST, then the text.

		The virtual range concatenates both areas' overflow; a position
		inside the form part keeps the text at its top, a position past it
		pins the form to its end and drives the text.
		"""
		st = self._chain_state()
		if st is None:
			return
		a_range, a_pos, b_range, b_pos, form_view, text_total = st
		total = a_range + b_range
		if total <= 0:
			return
		if action == "moveto":
			target = max(0.0, min(1.0, float(args[0]))) * total
		elif action == "delta":
			# Arrow-head clicks: +/- lines. Approximate a line by the
			# form's scroll unit — the exact step is cosmetic here.
			target = a_pos + b_pos + float(args[0]) * 40
			target = max(0.0, min(total, target))
		else:  # "scale" — a trough jump: most of the active viewport
			view = form_view if a_pos < a_range - 1 else (text_total - b_range)
			step = view * 0.9 * (1 if float(args[0]) > 0 else -1)
			target = max(0.0, min(total, a_pos + b_pos + step))
		try:
			form_total = a_range + form_view
			if target <= a_range:
				# Both widgets' moveto fractions span their WHOLE content
				# (scrollregion / body), not just the overflow.
				self.canvas.yview_moveto(target / form_total if form_total else 0.0)
				if b_range > 0:
					self._content_txt.yview_moveto(0.0)
			else:
				if a_range > 0:
					self.canvas.yview_moveto(1.0)
				self._content_txt.yview_moveto((target - a_range) / text_total)
		except Exception:  # noqa: BLE001
			pass

	def _on_detail_configure(self, event) -> None:
		# width <= 1 is the pre-map Configure noise. The hysteresis keeps a
		# sash drag across the threshold from flapping between layouts.
		width = getattr(event, "width", 0)
		if width <= 1:
			return
		if self._detail_orient == "horizontal":
			if width < _DETAIL_NARROW_WIDTH:
				self._apply_detail_orient("vertical")
		elif width >= _DETAIL_NARROW_WIDTH + _DETAIL_HYSTERESIS:
			self._apply_detail_orient("horizontal")

	def _on_canvas_configure(self, event) -> None:
		try:
			self.canvas.itemconfigure(self._inner_win, width=event.width)
			# The path link wraps to the canvas width, not a fixed 1200 px
			# (which would clip once the detail goes narrow/stacked). The
			# link may not exist yet during early Configure events.
			link = getattr(self, "_path_link", None)
			if link is not None:
				link.configure(wraplength=max(80, event.width - 16))
			# Re-assert "fixed when it fits": a resize that makes the form fit
			# the viewport again must snap the view back to the top (no
			# lingering half-scrolled state on a fixed form).
			bbox = self.canvas.bbox("all")
			if bbox and (bbox[3] - bbox[1]) <= event.height:
				self.canvas.yview_moveto(0.0)
		except Exception:  # noqa: BLE001
			pass

	def _on_inner_configure(self, _event) -> None:
		try:
			self.canvas.configure(scrollregion=self.canvas.bbox("all"))
			# The form's height changed — the chained scrollbar's virtual
			# range must follow (no canvas <Configure> fires when only the
			# inner content grows).
			self._chain_update()
		except Exception:  # noqa: BLE001
			pass

	def _build_fields_section(self) -> None:
		box = ttk.LabelFrame(self._scroll_inner, text=_("Fields"))
		box.pack(fill="x", padx=8, pady=4)
		self._fields_frame = box
		# RO column source toggle: the sunken labels show either the book's
		# current (on-disk) values or the analyze-time proposals; the ➡ copies
		# whichever set is displayed into the editable Entry. Default is the
		# ORIGINAL set — the proposal is already prefilled in the Entries, so
		# the proposed view is only a fallback to restore an accidental edit.
		ro = ttk.Frame(box)
		ro.pack(fill="x", padx=6, pady=(4, 0))
		self._ro_mode = tk.StringVar(value="current")
		ttk.Label(ro, text=_("Read-only column:")).pack(side="left")
		ttk.Radiobutton(ro, text=_("Original"), value="current", variable=self._ro_mode,
			command=self._apply_ro_mode).pack(side="left", padx=8)
		ttk.Radiobutton(ro, text=_("Proposed"), value="proposed", variable=self._ro_mode,
			command=self._apply_ro_mode).pack(side="left")
		fields = ttk.Frame(box)
		fields.pack(fill="x", padx=6, pady=6)
		fields.columnconfigure(4, weight=1)
		for row, (role, label) in enumerate(FIELD_SPECS):
			current = tk.StringVar(value="")
			value = tk.StringVar(value="")

			def _on_focus(_e, r=role):
				self._last_field_role = r

			# Static field caption — used to live on the include checkbox;
			# the checkbox is gone, the label stays (space is plentiful).
			cap = ttk.Label(fields, text=label, anchor="w")
			lbl = ttk.Label(fields, textvariable=current, relief="sunken", anchor="w", width=32)
			# Arrow points RO -> edit (copy the displayed value into the target).
			copy_btn = ttk.Button(fields, text="➡", width=2, command=lambda r=role: self._copy_current(r))
			entry = ttk.Entry(fields, textvariable=value)
			entry.bind("<FocusIn>", _on_focus)
			self._bind_select_all(entry)
			cap.grid(row=row, column=0, padx=(0, 4), pady=1, sticky="w")
			lbl.grid(row=row, column=1, padx=2, pady=1, sticky="we")
			copy_btn.grid(row=row, column=2, padx=2, pady=1)
			# ∅ = "delete this field" toggle (wrong proposal, correct value
			# unknown → applied as empty). Sits right next to the ➡: both
			# act on the proposal — ➡ fills it from the RO set shown on the
			# left (Original / Proposed), ∅ empties it.
			del_btn = ttk.Button(
				fields, text="∅", width=2,
				command=lambda r=role: self.toggle_field_delete(r),
			)
			del_btn.grid(row=row, column=3, padx=(2, 0), pady=1)
			_Tooltip(del_btn, _(
				"∅ mark: the field is applied as EMPTY (a proposal that is wrong "
				"while the correct value is unknown). Click again to restore editing."))
			entry.grid(row=row, column=4, padx=(2, 0), pady=1, sticky="we")
			if role == "title":
				# Compact swap icon (full label lives in the tooltip so the
				# cannot overlap the title row, the prior bug).
				swap_btn = ttk.Button(fields, text="⇄", width=3, command=self.swap_fields)
				swap_btn.grid(row=row, column=5, padx=(4, 0), pady=1, sticky="w")
				_Tooltip(swap_btn, _("Swap author and title  (Ctrl+W)"))
			self._fields[role] = {
				"current": current, "value": value, "entry": entry,
				"cap": cap, "del_btn": del_btn,
				"cap_fg": cap.cget("foreground"),
				"cleared": False, "pre_delete": "",
				"cur_disp": "", "prop_disp": "",  # both RO sets, mode picks one
			}
			if role in ("author", "series"):
				# Autocomplete against the library-wide pool; learn the typed
				# value back when the user leaves the field (a brand-new name
				# must immediately complete elsewhere).
				pool = self._vocab_authors if role == "author" else self._vocab_series
				ac = _Autocomplete(entry, lambda p=pool: sorted(p))
				self._acs[entry] = ac
				entry.bind("<FocusOut>", lambda _e, r=role: self._vocab_learn(r), add="+")
			self._field_entries.append(entry)
			# Trace value -> dirty (but not during programmatic load).
			value.trace_add("write", lambda *_: self._mark_dirty())
		box.columnconfigure(0, weight=1)

		# Target folder (read-only): the C13 move proposal, if any. Apply
		# recomputes the destination from the final metadata, so this is a
		# preview, not an editable field.
		loc = ttk.Frame(box)
		loc.pack(fill="x", padx=6, pady=(0, 2))
		ttk.Label(loc, text=_("Target folder:")).pack(side="left")
		self._location_lbl = ttk.Label(loc, text="—", relief="sunken", anchor="w")
		self._location_lbl.pack(side="left", fill="x", expand=True, padx=(4, 0))

		# Action radios + verified checkbox + notes + nav.
		bottom = ttk.Frame(box)
		bottom.pack(fill="x", padx=6, pady=6)
		ttk.Label(bottom, text=_("Action:")).grid(row=0, column=0, sticky="w")
		rad = ttk.Frame(bottom)
		rad.grid(row=0, column=1, columnspan=6, sticky="w")
		for i, a in enumerate(self.ACTIONS):
			ttk.Radiobutton(rad, text=a, value=a, variable=self._action_var).grid(row=0, column=i, padx=2, sticky="w")
		self._verified_chk = ttk.Checkbutton(rad, text=_("Verified"), variable=self._verified_var)
		self._verified_chk.grid(row=0, column=len(self.ACTIONS), padx=(10, 2), sticky="w")
		_Tooltip(self._verified_chk, _(
			"Mark the book as OK: apply writes `verified: true` into metadata.json, "
			"later analyze runs skip the book entirely, and apply places it on the "
			"target path even if some problems remain. Ctrl+O toggles."))
		ttk.Label(bottom, text=_("Note:")).grid(row=1, column=0, sticky="w", pady=(4, 0))
		self._notes_entry = ttk.Entry(bottom, textvariable=self._notes_var)
		self._notes_entry.grid(row=1, column=1, columnspan=6, sticky="we", pady=(4, 0))
		self._bind_select_all(self._notes_entry)
		bottom.columnconfigure(1, weight=1)
		nav = ttk.Frame(box)
		nav.pack(fill="x", padx=6, pady=(2, 8))
		ttk.Button(nav, text=_("◀ Previous (PgUp)"), command=self.prev_book).pack(side="left")
		ttk.Button(nav, text=_("Save (Ctrl+S)"), command=self.save).pack(side="left", padx=20)
		ttk.Button(nav, text=_("Next (PgDn) ▶"), command=self.next_book).pack(side="right")

		# Tab cycle = ONLY the field entries (notes stays outside the trap).
		self._editable_widgets = list(self._field_entries)

	def _cover_cell(self, parent, title: str, var=None, tip: str | None = None,
	                check_enabled: bool = True):
		"""One fixed-size cover cell: a slot box + caption; ``(label, caption)``.

		The slot is a fixed-size ``tk.Frame`` with ``pack_propagate(False)``
		so a missing or undersized cover renders as an identically-sized box
		— the previews stay aligned side by side regardless of image size or
		absence. The selection checkbox (when *var* is given) overlays the
		slot's top-left corner; a disabled one (check_enabled=False) still
		shows, so an un-strippable format explains itself via its tooltip.
		Clicking anywhere on the cover toggles the checkbox — the tiny
		square alone is a hard target (a click on the checkbox itself is
		delivered to the checkbox widget, so this never double-toggles).
		"""
		cell = ttk.Frame(parent)
		cell.pack(side="left", padx=6)
		slot = tk.Frame(
			cell, width=self.COVER_SLOT_W, height=self.COVER_SLOT_H,
			relief="sunken", borderwidth=1, background=self._field_bg,
		)
		slot.pack_propagate(False)
		slot.pack()
		lbl = ttk.Label(slot, text=_("(loading…)"), anchor="center")
		lbl.pack(fill="both", expand=True)
		if var is not None:
			chk = ttk.Checkbutton(
				lbl, variable=var, state="normal" if check_enabled else "disabled"
			)
			chk.place(x=4, y=4, anchor="nw")
			if tip:
				_Tooltip(chk, tip)
			if check_enabled:
				lbl.bind("<Button-1>", lambda _e: (var.set(not var.get()), "break")[1])
				try:
					lbl.configure(cursor="hand2")  # pointable: whole cover is clickable
				except Exception:  # noqa: BLE001
					pass
		cap = ttk.Label(cell, text=title, anchor="center", wraplength=self.COVER_SLOT_W)
		cap.pack(fill="x")
		return lbl, cap

	def _sync_cover_slots(self, *_) -> None:
		"""Give every cover cell in a row the SAME width, fitted to the pane.

		The right pane's width varies (PanedWindow sash, window size), so a
		row of fixed-width slots can overflow it — and pack then squeezes the
		trailing cells (the "messed up previews, especially missing ones"
		bug). Each row's slots are therefore set to one common width:
		``clamp(available / n, MIN, W)`` — always equal, never overflowing.
		"""
		try:
			for row in (self._cover_row, self._fmt_cover_row):
				cells = [c for c in row.winfo_children() if c.winfo_class() == "TFrame"]
				if not cells:
					continue
				per = row.winfo_width() // len(cells) - 14
				per = max(min(per, self.COVER_SLOT_W), self.COVER_SLOT_MIN)
				for c in cells:
					for sub in c.winfo_children():
						if sub.winfo_class() == "Frame":  # the slot box
							sub.configure(width=per)
		except Exception:  # noqa: BLE001
			pass

	def _build_covers_section(self) -> None:
		box = ttk.LabelFrame(self._scroll_inner, text=_("Covers"))
		box.pack(fill="x", padx=8, pady=4)
		self._covers_frame = box
		# Selection checkboxes sit directly ON each cover (top-left overlay,
		# the customary selection spot); "Delete checked" then removes what
		# is checked. The recommended cover is a URL preview, not a file, so
		# it gets no checkbox.
		self._cover_row = ttk.Frame(box)
		self._cover_row.pack(fill="x", padx=6, pady=8)
		self._cover_imgs = []
		self._cover_caps = []
		for title, var, tip in (
			(_("Current"), self._del_cover, _("Delete cover.jpg (on Delete checked)")),
			(_(".bak backup"), self._del_bak, _("Delete cover.jpg.bak (on Delete checked)")),
			(_("Recommended"), None, None),
		):
			lbl, cap = self._cover_cell(self._cover_row, title, var, tip)
			self._cover_imgs.append(lbl)
			self._cover_caps.append(cap)
		# Embedded covers of the book's format files (calibre extraction) —
		# rebuilt per book in _apply_fmt_covers. Their checkbox strips the
		# cover EMBEDDED in the file (the invalid calibre placeholder) while
		# the ebook file itself stays; only EPUB supports that surgery.
		ttk.Label(box, text=_("Embedded covers per format (☐ = strip from the ebook):")).pack(anchor="w", padx=6)
		self._fmt_cover_row = ttk.Frame(box)
		self._fmt_cover_row.pack(fill="x", padx=6, pady=(2, 8))
		# Keep the slot widths equal and fitted on every pane resize.
		self._cover_row.bind("<Configure>", self._sync_cover_slots, add="+")
		self._fmt_cover_row.bind("<Configure>", self._sync_cover_slots, add="+")
		btns = ttk.Frame(box)
		btns.pack(fill="x", padx=6, pady=4)
		ttk.Button(btns, text=_("Keep (Ctrl+P)"), command=self.cover_keep).pack(side="left", padx=2)
		ttk.Button(btns, text=_("Restore .bak (Ctrl+B)"), command=self.cover_restore_bak).pack(side="left", padx=2)
		ttk.Button(btns, text=_("Apply new (Ctrl+N)"), command=self.cover_new).pack(side="left", padx=2)
		ttk.Button(btns, text=_("Delete checked (Ctrl+M)"), command=self.cover_delete_checked).pack(side="left", padx=10)

	def _build_content_section(self, parent) -> None:
		box = ttk.LabelFrame(parent, text=_("Content (whole book, loads progressively)"))
		box.pack(fill="both", expand=True, padx=8, pady=4)
		top = ttk.Frame(box)
		top.pack(fill="x", padx=6, pady=4)
		ttk.Label(top, text=_("Format:")).pack(side="left")
		self._format_holder = ttk.Frame(top)
		self._format_holder.pack(side="left", fill="x", expand=True, padx=6)
		rec = ttk.Frame(box)
		rec.pack(fill="x", padx=6, pady=(2, 4))
		self._recode_chk = ttk.Checkbutton(
			rec, text=_("↺ Recode (Ctrl+G)"), variable=self._recode_var,
			command=self._apply_content_text, state="disabled",
		)
		self._recode_chk.pack(side="left")
		# Manual codec experiment: „read as“ is the codec the text was
		# WRONGLY read through (the encode side), „actually is“ is what the
		# recovered bytes really are (nearly always utf-8); the preview
		# re-renders live, always as UTF-8 text, whatever the pair.
		ttk.Label(rec, text=_("  read as:")).pack(side="left")
		self._recode_from_box = ttk.Combobox(
			rec, textvariable=self._recode_from, values=list(ENCODING_CHOICES),
			width=13, state="readonly",
		)
		self._recode_from_box.pack(side="left", padx=(1, 4))
		ttk.Label(rec, text=_("actually is:")).pack(side="left")
		self._recode_to_box = ttk.Combobox(
			rec, textvariable=self._recode_to, values=list(ENCODING_CHOICES),
			width=13, state="readonly",
		)
		self._recode_to_box.pack(side="left", padx=(1, 4))
		swap_btn = ttk.Button(rec, text="⇄", width=3, command=self._swap_recode_codecs)
		swap_btn.pack(side="left")
		_recode_tip = _(
			"Double-encoding repair: „read as“ = the codec the text was originally "
			"mis-read through (typically cp1250); „actually is“ = the real encoding "
			"of the bytes (almost always utf-8). The preview is always UTF-8."
		)
		_Tooltip(swap_btn, _("Swap conversion direction (read as ↔ actually is)"))
		_Tooltip(self._recode_from_box, _recode_tip)
		_Tooltip(self._recode_to_box, _recode_tip)
		self._recode_hint = ttk.Label(rec, text="", foreground="#a00")
		self._recode_hint.pack(side="left", padx=8)
		# Clickable when it offers the swapped (working) direction — see
		# _recompute_recode / _on_recode_hint_click.
		self._recode_hint.bind("<Button-1>", self._on_recode_hint_click)
		self._recode_from.trace_add("write", lambda *_: self._recode_changed())
		self._recode_to.trace_add("write", lambda *_: self._recode_changed())
		tbody = ttk.Frame(box)
		tbody.pack(fill="both", expand=True, padx=6, pady=0)
		self._content_body = tbody
		self._content_txt = self._style_text(
			tk.Text(tbody, wrap="word", state="disabled")
		)
		self._content_txt.pack(side="left", fill="both", expand=True)
		self._content_vsb = ttk.Scrollbar(tbody, orient="vertical", command=self._content_txt.yview)
		self._content_txt.configure(yscrollcommand=self._content_vsb.set)
		self._content_vsb.pack(side="right", fill="y")
		self._attach_text_copy_menu(self._content_txt)
		# The preview lives in its own Panedwindow pane (full pane height —
		# the drag-to-resize grip is gone with the old scroll-column layout:
		# the pane's sash IS the resize knob now).
		return "break"

	def _copy_text_to_clipboard(self, txt, *, all_text: bool = False) -> None:
		"""Write the Text's selection (or whole content) into the clipboard.

		Done manually instead of ``event_generate("<<Copy>>")`` so the copy
		also works when the virtual event would nest inside another handler
		and never reaches the widget's class binding.
		"""
		rng = txt.tag_ranges("sel")
		text = txt.get("1.0", "end-1c") if (all_text or not rng) else txt.get(*rng)
		self.root.clipboard_clear()
		if text:
			self.root.clipboard_append(text)

	def _attach_text_copy_menu(self, txt) -> None:
		"""Right-click menu (copy selection / copy all) for a readonly Text.

		Both readonly views (Found problems, content preview) are disabled-state
		Text widgets, so mouse selection already works (Tk allows selecting in a
		disabled Text — only editing is blocked). The explicit Ctrl+C binding
		adds the copy shortcut X11's Text lacks; Ctrl+A selects everything.
		"""
		menu = tk.Menu(txt, tearoff=0)
		menu.add_command(
			label=_("Copy selection"), command=lambda: self._copy_text_to_clipboard(txt))
		menu.add_command(
			label=_("Copy all"), command=lambda: self._copy_text_to_clipboard(txt, all_text=True))

		def _menu(_event) -> str:
			try:
				menu.tk_popup(_event.x_root, _event.y_root)
			finally:
				menu.grab_release()
			return "break"

		def _copy(_event) -> str:
			self._copy_text_to_clipboard(txt)
			return "break"

		txt.bind("<Button-3>", _menu)
		txt.bind("<Control-c>", _copy)
		txt.bind("<Control-a>", _select_all_text)

	def _build_status_bar(self) -> None:
		self._status = tk.StringVar(value="")
		bar = ttk.Label(self.root, textvariable=self._status, relief="sunken", anchor="w")
		bar.pack(fill="x", side="bottom")

	# ------------------------------------------------------------------
	# Shortcuts
	# ------------------------------------------------------------------

	def _bind_select_all(self, entry) -> None:
		# X11's default <Control-a> in an Entry is "move to start", not select
		# all — rebind it so Ctrl+A works as users expect.
		entry.bind("<Control-a>", self._on_select_all, add="+")

	def _on_select_all(self, event) -> str:
		w = event.widget
		try:
			w.select_range(0, "end")
			w.icursor("end")
		except Exception:  # noqa: BLE001
			pass
		return "break"

	def _install_tab_trap(self) -> None:
		"""Make ``Tab``/``Shift-Tab`` cycle ONLY editable fields, everywhere.

		A custom bindtag (``_TAB_TRAP_TAG``) is bound to <Tab>/<Shift-Tab> and
		prepended to every focusable widget's bindtags. Because it sits FIRST in
		the bindtags order, our handler runs before Tk's default focus
		traversal and returns "break", so focus never escapes to a button, RO
		label or checkbox. (``bind_all`` would run last and lose the race.)
		"""
		self.root.bind_class(_TAB_TRAP_TAG, "<Tab>", self._on_tab)
		self.root.bind_class(_TAB_TRAP_TAG, "<Shift-Tab>", self._on_tab)
		# X11 delivers a REAL Shift+Tab press as keysym ISO_Left_Tab (not Tab
		# with a Shift modifier) — the same reason tk.tcl adds it to the
		# <<PrevWindow>> virtual event. Without this binding the trap silently
		# misses the key on Linux and focus falls back to Tk's default
		# traversal, which follows widget CREATION order (➡ of the row, then
		# the previous row's ∅) instead of jumping to the previous field.
		self.root.bind_class(_TAB_TRAP_TAG, "<ISO_Left_Tab>", self._on_tab)
		self._trap_subtree(self.root)

	def _trap_subtree(self, parent) -> None:
		"""Recursively prepend the Tab-trap tag to every focusable widget."""
		for w in parent.winfo_children():
			try:
				if w.winfo_class() in _FOCUSABLE_CLASSES:
					tags = w.bindtags()
					if _TAB_TRAP_TAG not in tags:
						w.bindtags((_TAB_TRAP_TAG,) + tags)
			except Exception:  # noqa: BLE001
				pass
			self._trap_subtree(w)

	def _bind_shortcuts(self) -> None:
		self.root.bind_all("<Control-Key>", self._on_ctrl_key, add="+")
		self.root.bind_all("<Next>", lambda _e: (self.next_book(), "break")[1], add="+")
		self.root.bind_all("<Prior>", lambda _e: (self.prev_book(), "break")[1], add="+")
		self.root.bind_all("<F1>", lambda _e: self._help_overlay(), add="+")
		# Wheel routing: the widget under the pointer scrolls first; the form
		# canvas only takes over at that widget's edge, and never when the
		# form fits (see _on_wheel / _scroll_canvas).
		for seq in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
			self.root.bind_all(seq, self._on_wheel, add="+")

	@staticmethod
	def _can_y_scroll(widget, up: bool) -> bool:
		"""Can *widget* (Text/Treeview) still scroll in direction *up*?"""
		try:
			first, last = widget.yview()
		except Exception:  # noqa: BLE001
			return False
		eps = 1e-9
		return first > eps if up else last < 1.0 - eps

	def _on_wheel(self, event) -> str | None:
		"""Route wheel events: widget under the pointer FIRST, form second.

		X11 delivers Button-4 (up) / Button-5 (down); Windows/macOS deliver
		``<MouseWheel>`` with a signed delta. Because this handler lives on the
		"all" bindtag (runs LAST), a Text/Treeview under the pointer has ALREADY
		scrolled itself via its class binding by the time we see the event. We
		only step in when that widget is stuck at its edge (or the pointer is
		over a non-scrollable part of the form) — then, and only then, does the
		form canvas scroll. The left list is not part of the form: over it, the
		list scrolls itself and the form never moves.
		"""
		if event.num == 4:
			up = True
		elif event.num == 5:
			up = False
		else:
			up = (event.delta or 0) > 0
		node = event.widget
		# A Tcl-internal widget (e.g. the ttk.Combobox popdown of the
		# Action/Category filter) has no Python wrapper: tkinter's
		# _substitute then leaves event.widget as the raw pathname STRING,
		# not a widget. Not ours to route — the popdown's own class binding
		# (which runs before "all") has already scrolled it, and the form
		# must stay put while a dropdown is open.
		if isinstance(node, str):
			return None
		while node is not None and node is not self.root:
			if node is self.canvas:
				if not self._scroll_canvas(up):
					# Stacked layout: a wheel that falls off the form's
					# bottom (or a form that fits) continues into the book
					# text below it — the chained flow.
					if self._detail_orient == "vertical" and not up:
						try:
							self._content_txt.yview_scroll(1, "units")
						except Exception:  # noqa: BLE001
							pass
				return "break"
			# The book list scrolls itself (its own wheel binding) — the
			# form stays put. NB: _BookList is a COMPOSITION, the actual
			# event widget is its .canvas.
			if node is self.tree or node is self.tree.canvas:
				return "break"
			if node.winfo_class() == "Text":
				if self._can_y_scroll(node, up):
					return "break"  # the Text class binding already scrolled it
				if node is self._content_txt and self._detail_orient == "vertical" and not up:
					return "break"  # chained page end — nothing below the text
				# stuck at the Text's edge -> the form takes over
				self._scroll_canvas(up)
				return "break"
			node = node.master
		return None

	def _scroll_canvas(self, up: bool) -> bool:
		"""Scroll the form column — a hard no-op while the form fits.

		The form must sit FIXED at the top when its content fits the viewport;
		it only starts scrolling once it actually overflows the canvas.
		Returns whether the canvas actually scrolled (False also at its
		edges — the stacked layout chains the wheel on from there).
		"""
		try:
			bbox = self.canvas.bbox("all")
			if not bbox:
				return False
			if (bbox[3] - bbox[1]) - self.canvas.winfo_height() <= 0:
				return False  # fits -> fixed
			first, last = self.canvas.yview()
			if up and first <= 0.0:
				return False  # already at the top
			if not up and last >= 1.0:
				return False  # already at the bottom
			self.canvas.yview_scroll(-1 if up else 1, "units")
			return True
		except Exception:  # noqa: BLE001
			return False

	def _on_tab(self, event) -> str | None:
		# Only the field entries are trapped; from any other widget (notes,
		# buttons, the list) Tab falls through to Tk's default traversal.
		w = self.focus_get_safe()
		if w not in self._editable_widgets:
			return None
		# An open autocomplete dropdown: Tab accepts the highlighted row
		# ONLY after a deliberate arrow pick — an untouched popup (the user
		# merely typed their own value) is just closed, otherwise tabbing
		# away would silently rewrite the field with the first suggestion
		# that happens to share the prefix.
		ac = self._acs.get(w)
		if ac is not None and ac.is_open:
			if ac.has_user_pick:
				ac.accept()
			else:
				ac.hide()
		# Direction: the Shift modifier (Windows/macOS <Shift-Tab>) or the X11
		# keysym itself (ISO_Left_Tab arrives as its own keysym).
		shift = bool(event.state & 0x1) or (event.keysym or "") == "ISO_Left_Tab"
		self._cycle_editable(forward=not shift)
		return "break"

	def _on_ctrl_key(self, event) -> str | None:
		k = (event.keysym or "").lower()
		# A modal child holding the grab (the bulk-edit dialog) owns the
		# keyboard — main-window shortcuts must not fire underneath it.
		try:
			grabbed = self.root.grab_current()
		except Exception:  # noqa: BLE001
			grabbed = None
		if grabbed is not None and grabbed is not self.root:
			return None
		# Shift variants of single-book shortcuts act on the whole
		# multi-selection ("same key + Shift = all selected books"). They are
		# matched BEFORE the passthrough set — Ctrl+Shift+A must not land in
		# Ctrl+A's select-all passthrough — and unknown Shift combos still
		# fall through untouched (Ctrl+Shift+C/V/X keep their native meaning).
		if event.state & 0x0001:  # ShiftMask
			shift_dispatch = {
				"a": self.bulk_accept,
				"o": self.bulk_toggle_verified,
				"m": self.bulk_delete_covers,
				"d": self.bulk_clear_action,
				"r": self.remove_from_review,
			}
			handler = shift_dispatch.get(k)
			if handler is not None:
				handler()
				return "break"
			return None
		if k in _PASSTHROUGH:
			return None  # keep native copy/paste/cut/undo/select-all
		dispatch = {
			"return": self.act_accept, "d": self.act_delete, "k": self.act_keep,
			"o": self.toggle_verified,
			"0": self.act_clear, "s": self.save, "q": self.quit_app,
			"w": self.swap_fields, "f": self.copy_current_to_focused,
			"e": self.bulk_edit, "j": self.merge_selected,
			"r": self.act_merge,
			"n": self.cover_new,
			"b": self.cover_restore_bak, "p": self.cover_keep, "m": self.cover_delete_checked,
			"g": self.content_recode_toggle,
		}
		handler = dispatch.get(k)
		if handler is not None:
			handler()
			return "break"
		return None

	def _active_field_entries(self) -> list:
		"""Field Entries editable right now (∅-marked ones are disabled)."""
		return [f["entry"] for f in self._fields.values() if not f["cleared"]]

	def _cycle_editable(self, *, forward: bool) -> None:
		# ∅-marked fields are disabled and cannot take focus — Tab must skip
		# them, not get stuck trying to focus one.
		ws = self._active_field_entries()
		if not ws:
			return
		w = self.focus_get_safe()
		if w not in ws:
			target = ws[0] if forward else ws[-1]
		else:
			i = ws.index(w)
			step = 1 if forward else -1
			target = ws[(i + step) % len(ws)]
		target.focus_set()
		# Tabbing into a field selects its whole content: the common case is
		# overwriting the value, and X11 would otherwise leave the cursor at
		# position 0 with nothing selected. Mouse clicks still place the
		# cursor normally — this path only runs from Tab/Shift-Tab.
		try:
			cls = target.winfo_class()
			if cls in ("Entry", "TEntry", "TCombobox"):
				target.select_range(0, "end")
				target.icursor("end")
			else:  # Text (notes)
				target.tag_add("sel", "1.0", "end")
		except Exception:  # noqa: BLE001
			pass
		self._see_widget(target)

	def focus_get_safe(self):
		try:
			return self.focus_get()
		except Exception:  # noqa: BLE001
			return None

	def focus_get(self):  # type: ignore[override]
		return self.root.focus_get()

	# ------------------------------------------------------------------
	# List / filtering
	# ------------------------------------------------------------------

	def _all_categories(self) -> list[str]:
		cats = []
		seen = set()
		for e in self.entries:
			c = (e.get("diagnosis") or {}).get("category")
			if c and c not in seen:
				seen.add(c)
				cats.append(c)
		return cats

	def refresh_list(self) -> None:
		# Refresh the category filter options (covers newly loaded data).
		cats = ["all"] + self._all_categories()
		self._cat_combo.configure(values=cats)
		if self._filter_category.get() not in cats:
			self._filter_category.set("all")

		sel_iid = self.tree.focus()
		sel_iids = self.tree.selection_get()  # banked BEFORE the rebuild
		self.tree.delete(*self.tree.get_children())
		idxs = self._filtered_indices()
		for i in idxs:
			e = self.entries[i]
			uuid = e.get("uuid")
			# NB: ``image`` MUST be omitted (not passed as None) — passing
			# image=None corrupts ttk's option parsing and the next option's
			# value (here the ``values`` list) is misread as an option name.
			kw = dict(iid=str(i), text=self._entry_title(e),
			          values=(self._action_label(e), entry_author_label(e),
			                  bool(e.get("verified")), entry_series_label(e)))
			img = self._thumb_photo_for(uuid)
			if img is not None:
				kw["image"] = img
			self.tree.insert("", "end", **kw)
		# Preserve selection (no `see` here: a periodic refresh — e.g. the
		# thumbnail loader re-inserting rows — must keep the SCROLL position,
		# not yank the view to the selected row; navigation scrolls via
		# _step's explicit see()). SILENT re-select: firing <<TreeviewSelect>>
		# here would reload the detail pane of the book the user may be
		# editing — wiping unsaved field edits and re-running the cover /
		# content loaders on every background refresh. The multi-selection
		# survives the rebuild too, minus rows the filter hides — the bulk
		# edit must act on what the user SEES selected.
		if sel_iid and self.tree.exists(sel_iid):
			self.tree.preserve_selection(sel_iid, sel_iids)
		self._set_status()

	def _filtered_indices(self) -> list[int]:
		act = self._filter_action.get()
		cat = self._filter_category.get()
		needle = self._search.get().strip().lower()
		out = []
		for i, e in enumerate(self.entries):
			ea = e.get("action")
			if act == "verified":
				# Orthogonal to the action: shows books carrying the OK mark
				# regardless of their pending/accept/delete/keep state.
				if not e.get("verified"):
					continue
			elif act != "all" and (ea or "pending") != act:
				continue
			ec = (e.get("diagnosis") or {}).get("category")
			if cat != "all" and ec != cat:
				continue
			if needle:
				# A library-loaded entry is exempt from the ENTRY haystack —
				# the index matched it against the FULL manifest (description
				# included), which the entry itself does not carry. The exact
				# index haystack is kept per uuid, so the exemption hides a
				# stale superset match when the needle narrows.
				u = e.get("uuid")
				lib_hay = self._lib_uuids.get(u) if u else None
				if lib_hay is not None:
					words = needle.lower().split()
					if not all(w in lib_hay for w in words):
						continue
				else:
					active_fields = None
					if i == self._cur and hasattr(self, "_fields") and self._fields:
						active_fields = {r: f["value"].get() for r, f in self._fields.items() if not f.get("cleared")}
					if not entry_matches_search(needle, e, active=active_fields):
						continue
			out.append(i)
		# Display order, NOT storage order: series blocks first (name, then
		# order inside the series), the rest by author/title. Every consumer
		# of this method sees the same order — the list build, the j/k
		# stepping, the position counter — so they cannot disagree. The
		# iids stay the ORIGINAL entry indices; self.entries (and thus the
		# save order) is untouched.
		out.sort(key=lambda i: entry_sort_key(self.entries[i]))
		return out


	@staticmethod
	def _entry_title(e: dict) -> str:
		cur = e.get("current") or {}
		return cur.get("title") or "—"

	@staticmethod
	def _action_label(e: dict) -> str:
		# Raw action name — _BookList maps it to a coloured glyph.
		return e.get("action") or ""

	def _on_tree_select(self) -> None:
		iid = self.tree.focus()
		if not iid or not iid.lstrip("-").isdigit():
			return
		idx = int(iid)
		if 0 <= idx < len(self.entries):
			self._select_index(idx, keep_focus=True)

	# ------------------------------------------------------------------
	# Library search ("+ library" mode)
	# ------------------------------------------------------------------

	def _on_search_changed(self, *_args) -> None:
		"""Search box changed: refilter the list and (in library mode)
		schedule the debounced index serve."""
		self.refresh_list()
		self._schedule_lib_search()

	def _on_lib_mode_changed(self) -> None:
		"""The ``+ library`` checkbox was toggled.

		ON with a filled search serves it from the in-memory index right
		away; ON with an empty search only hints — merging all ~5k books
		would be meaningless, the whole point is the filter. OFF does
		nothing to the list: merged entries stay for the rest of the
		session (an accidental toggle — Tab falls through to the checkbox
		from non-field widgets, Space then activates it — must not throw
		the work away), and searches are instant anyway.
		"""
		if self._lib_mode.get():
			if self._search.get().strip():
				self._apply_lib_search()
			else:
				self._flash(_("type a search to load matching library books"))

	def _schedule_lib_search(self) -> None:
		"""Debounce serving behind the search box (400 ms after the last
		keystroke).

		The serve itself is an instant in-memory filter, but each settled
		PARTIAL needle still merges its (superset) matches into the list —
		typing through "mar" → "mark" → "mark stone" must not merge three
		waves while the keys are still moving.
		"""
		if self._lib_search_after is not None:
			try:
				self.root.after_cancel(self._lib_search_after)
			except Exception:  # noqa: BLE001
				pass
			self._lib_search_after = None
		if not self._lib_mode.get() or not self._search.get().strip():
			return
		self._lib_search_after = self.root.after(400, self._apply_lib_search)

	def _apply_lib_search(self) -> None:
		"""Merge index matches of the current needle into the list (Tk thread).

		Additive only — nothing already in the list is ever removed: library
		additions live in the editor's memory for the whole session, and
		only CHANGED ones are written to review.yaml on save
		(entries_to_write). If the startup index is still building, the
		query waits (the progress line shows it) and is re-applied
		automatically when the index lands.
		"""
		self._lib_search_after = None
		if not self._alive or not self._lib_mode.get():
			return
		needle = self._search.get().strip()
		if not needle:
			return
		if not self._lib_index_done:
			self._flash(_("indexing library…"))
			return  # _finish_lib_index re-applies the pending query
		skip_paths = {str(Path(e.get("path") or "")) for e in self.entries if e.get("path")}
		skip_uuids = {e.get("uuid") for e in self.entries if e.get("uuid")}
		found = search_library_index(self._lib_index, needle, skip_paths, skip_uuids)
		added = 0
		for e, hay in found:
			self.entries.append(e)  # append: review-entry indices stay stable
			if e.get("uuid"):
				# The haystack feeds the list filter's exemption (the entry
				# itself carries no description, which the index matched on).
				self._lib_uuids[e["uuid"]] = hay
			added += 1
		self.refresh_list()
		if added:
			self._start_thumb_loader()  # thumbnails for the new rows
		self._flash(_("library: +{n} books").format(n=added))

	# ------------------------------------------------------------------
	# List cover hover popup
	# ------------------------------------------------------------------

	def _entry_by_uuid(self, uuid: str):
		for e in self.entries:
			if e.get("uuid") == uuid:
				return e
		return None

	def _on_tree_motion(self, event) -> None:
		iid = self.tree.identify_row(event.y)
		uuid = None
		if iid and iid.lstrip("-").isdigit():
			idx = int(iid)
			if 0 <= idx < len(self.entries):
				uuid = self.entries[idx].get("uuid")
		if uuid != self._hover_uuid:
			self._cancel_hover()
			self._hide_cover_popup()
			self._hover_uuid = uuid
			# Only pop up when the row actually has a small thumbnail loaded.
			if uuid and self._thumbs_pil.get(uuid):
				self._hover_after = self.root.after(
					300, lambda u=uuid: self._show_cover_popup(u)
				)

	def _on_tree_leave(self, _event) -> None:
		self._cancel_hover()
		self._hide_cover_popup()
		self._hover_uuid = None

	# ------------------------------------------------------------------
	# Open folder in file manager
	# ------------------------------------------------------------------

	def open_current_folder(self) -> None:
		"""Open the current book's folder (click on the header path link)."""
		self._open_entry_folder(self.entries[self._cur])

	def _open_entry_folder(self, e) -> None:
		path = e.get("path") or ""
		folder = (self.library / path) if path else self.library
		err = open_folder_in_manager(folder)
		self._flash(err or _("opened: {folder}").format(folder=folder))

	def _on_tree_double(self, event) -> str:
		"""Double-click a list row = open that book's folder."""
		iid = self.tree.identify_row(event.y)
		if iid and iid.lstrip("-").isdigit():
			idx = int(iid)
			if 0 <= idx < len(self.entries):
				self._open_entry_folder(self.entries[idx])
		return ""

	def _cancel_hover(self) -> None:
		if self._hover_after is not None:
			try:
				self.root.after_cancel(self._hover_after)
			except Exception:  # noqa: BLE001
				pass
			self._hover_after = None

	def _show_cover_popup(self, uuid: str) -> None:
		self._hide_cover_popup()
		if not self._alive or not uuid:
			return
		e = self._entry_by_uuid(uuid)
		if not e:
			return
		cp, _ = cover_paths(self.library, e.get("path", ""))
		if not cp.is_file():
			return
		pil = self._big_thumbs.get(uuid)
		if pil is None and uuid not in self._big_thumbs:
			pil = load_thumb(cp, 300, 400)
			self._big_thumbs[uuid] = pil
			# Keep the big-thumb cache bounded (drop the oldest entry).
			while len(self._big_thumbs) > 64:
				k = next(iter(self._big_thumbs))
				self._big_thumbs.pop(k, None)
		if pil is None:
			return
		try:
			photo = ImageTk.PhotoImage(pil)
		except Exception:  # noqa: BLE001
			return
		self._cover_popup_photo = photo  # prevent GC
		popup = tk.Toplevel(self.root)
		popup.wm_overrideredirect(True)
		x = self.root.winfo_pointerx() + 18
		y = self.root.winfo_pointery() + 18
		try:
			popup.geometry(f"+{x}+{y}")
		except Exception:  # noqa: BLE001
			pass
		ttk.Label(popup, image=photo, borderwidth=2, relief="solid").pack()
		self._cover_popup = popup

	def _hide_cover_popup(self) -> None:
		if self._cover_popup is not None:
			try:
				self._cover_popup.destroy()
			except Exception:  # noqa: BLE001
				pass
			self._cover_popup = None
			self._cover_popup_photo = None

	# ------------------------------------------------------------------
	# Book load / collect (in-memory model)
	# ------------------------------------------------------------------

	def _select_index(self, idx: int, *, keep_focus: bool) -> None:
		if not (0 <= idx < len(self.entries)):
			return
		if self._cur == idx:
			# Already showing this book. A reload would overwrite the target
			# fields from the (stale) entry dict — silently discarding the
			# user's unsaved edits; there is nothing new to load anyway.
			return
		# Persist the currently-shown entry's edits before switching.
		if 0 <= self._cur < len(self.entries) and self._cur != idx:
			self._collect_current()
		self._cur = idx
		self._load_book(idx)
		if keep_focus:
			self._focus_restore()

	def _collect_current(self) -> None:
		if not (0 <= self._cur < len(self.entries)):
			return
		e = self.entries[self._cur]
		e["action"] = action_value(self._action_var.get())
		# The OK mark is a separate decision channel — stored only when set,
		# so untouched entries stay byte-identical to analyze output.
		if self._verified_var.get():
			e["verified"] = True
		else:
			e.pop("verified", None)
		# The user's field values are merged INTO the proposal (there is no
		# separate edited block): typed values overwrite, ∅ marks become
		# nulls, untouched/empty fields keep the existing proposal key.
		cleared = {r for r, f in self._fields.items() if f["cleared"]}
		overlay = compose_overlay(
			{r: f["value"].get() for r, f in self._fields.items()}, cleared)
		if overlay:
			e["proposed"] = {**(e.get("proposed") or {}), **overlay}
		# A newly typed author/series becomes part of the vocabulary — the
		# very next book the user edits must be able to complete against it.
		for r in ("author", "series"):
			self._vocab_learn(r)
		notes = self._notes_var.get().strip()
		e["notes"] = notes or None

	def _vocab_learn(self, role: str) -> None:
		"""Add the field's current value into its autocomplete pool (if any)."""
		f = self._fields.get(role)
		if not f:
			return
		v = f["value"].get().strip()
		if v:
			(self._vocab_authors if role == "author" else self._vocab_series).add(v)

	def _load_book(self, idx: int) -> None:
		e = self.entries[idx]
		for ac in self._acs.values():
			ac.hide()  # a dropdown must not survive into the next book
		self._loading = True
		try:
			# Header. The path lives on its own clickable line (open folder).
			# The diagnoses are NOT crammed in here anymore — they render in
			# their own "Found problems" section (all of them, sorted by
			# severity), which replaced the old "primary + (+N more)" line
			# that could hide the load-bearing problem behind the counter.
			uuid = e.get("uuid") or "—"
			path = e.get("path") or ""
			# A library-loaded book (the "+ library" search) is not in
			# review.yaml yet — say so where the diagnosis line would carry
			# the context for review entries.
			lib_note = ""
			if e.get("uuid") and e["uuid"] in self._lib_uuids:
				lib_note = "\n" + _(
					"not in review.yaml — deciding or editing it adds it to review on save")
			self._header_lbl.configure(
				text=_("Entry {i}/{n}   uuid: {uuid}").format(
					i=idx + 1, n=len(self.entries), uuid=uuid) + lib_note,
			)
			self._path_link.configure(text=path or _("(no path)"))
			# Fields. Entries prefill proposed > current; the RO column
			# holds both sets and shows the one picked by the mode toggle.
			# (User edits live in `proposed` itself — collect merges them.)
			cur = e.get("current") or {}
			prop = e.get("proposed") or {}
			for role, f in self._fields.items():
				f["cur_disp"] = self._display_value(cur.get(role))
				f["prop_disp"] = self._display_value(prop.get(role)) or f["cur_disp"]
				if role in prop:
					target = prop[role]
				else:
					target = cur.get(role)
				# proposed[field]: null is the saved ∅ mark — restore the
				# deleted STATE (disabled entry, nothing to restore), not the
				# null as a displayed value.
				is_cleared = role in prop and prop[role] is None
				self._field_cleared_ui(f, is_cleared)
				if is_cleared:
					f["value"].set("")
					f["pre_delete"] = ""
				else:
					f["value"].set(self._display_value(target))
			self._apply_ro_mode()
			# Target folder preview (C13 move proposal; "—" when the book
			# already sits where its metadata say it belongs).
			self._location_lbl.configure(
				text=(e.get("proposed") or {}).get("location") or "—")
			# Action / notes / verified.
			self._action_var.set(e.get("action") or "pending")
			self._notes_var.set(e.get("notes") or "")
			self._verified_var.set(bool(e.get("verified")))
		finally:
			self._loading = False
		# Covers + content for the new book.
		self._load_problems(e)
		self._load_merge_panel(e)
		self._refresh_covers()
		self._refresh_formats()
		# Scroll the detail column back to the top for the new book.
		try:
			self.canvas.yview_moveto(0.0)
		except Exception:  # noqa: BLE001
			pass

	@staticmethod
	def _display_value(v) -> str:
		if v is None:
			return ""
		if isinstance(v, list):
			return ", ".join(str(x) for x in v)
		return str(v)

	def _apply_ro_mode(self) -> None:
		"""Show the picked RO set (original vs proposed) in the sunken labels."""
		key = "cur_disp" if self._ro_mode.get() == "current" else "prop_disp"
		for f in self._fields.values():
			f["current"].set(f[key])

	# ------------------------------------------------------------------
	# Focus / scroll helpers
	# ------------------------------------------------------------------

	def _see_widget(self, w) -> None:
		"""Scroll the detail canvas so widget *w* is visible (best effort)."""
		canvas = getattr(self, "canvas", None)
		inner = getattr(self, "_scroll_inner", None)
		if canvas is None or inner is None:
			return
		try:
			if not w.winfo_ismapped():
				return
			inner_top = inner.winfo_rooty()
			w_top = w.winfo_rooty() - inner_top
			w_bot = w_top + max(w.winfo_height(), 20)
			inner_h = max(inner.winfo_height(), 1)
			first, last = canvas.yview()
			view_top = first * inner_h
			view_bot = last * inner_h
			if w_top < view_top:
				canvas.yview_moveto(max(w_top - 8, 0) / inner_h)
			elif w_bot > view_bot:
				span = (last - first) * inner_h
				canvas.yview_moveto(min((w_bot - span + 8) / inner_h, 1.0))
		except Exception:  # noqa: BLE001
			pass

	def _focus_first_field(self) -> None:
		ws = self._active_field_entries()
		if ws:
			ws[0].focus_set()
			self._see_widget(ws[0])

	def _focus_restore(self) -> None:
		role = self._last_field_role
		f = self._fields.get(role)
		if f and not f["cleared"]:
			w = f["entry"]
			w.focus_set()
			self._see_widget(w)
		else:
			self._focus_first_field()  # cleared/unknown role → first editable

	def focus_search(self) -> None:
		self._search_entry.focus_set()
		self._search_entry.select_range(0, "end")
		self._search_entry.icursor("end")

	# ------------------------------------------------------------------
	# Actions / field ops
	# ------------------------------------------------------------------

	def _mark_dirty(self) -> None:
		if not self._loading:
			self._dirty = True
			self._set_status()

	def _on_action_changed(self, *_args) -> None:
		"""The action radio / shortcut changed: push it into the entry NOW.

		The list row's glyph and the action/search filters read the ENTRY dict,
		not the radio var — deferring the write to _collect_current (book
		switch / save) left the row showing its old action until some unrelated
		refresh_list happened to run, which read as "the accept never landed".
		Writing immediately + refreshing keeps the list in sync; refresh_list
		also re-applies the action filter, so a book decided under e.g. a
		"pending" filter drops out of the view at once (exactly the case
		_step's not-in-filter fallback anticipates).
		"""
		if self._loading:
			return
		self._mark_dirty()
		if not (0 <= self._cur < len(self.entries)):
			return
		self.entries[self._cur]["action"] = action_value(self._action_var.get())
		self.refresh_list()

	def set_action(self, a: str) -> None:
		if 0 <= self._cur < len(self.entries):
			self._action_var.set(a)

	def act_accept(self): self.set_action("accept")
	def act_delete(self): self.set_action("delete")
	def act_keep(self): self.set_action("keep")
	def act_clear(self): self.set_action("pending")

	def toggle_verified(self) -> None:
		"""Ctrl+O: flip the OK mark on the current book."""
		self._verified_var.set(not self._verified_var.get())

	def _on_verified_changed(self, *_args) -> None:
		"""Mirror _on_action_changed: push the OK mark into the entry dict
		immediately so the list glyph/filter stay in sync with the checkbox."""
		if self._loading:
			return
		self._mark_dirty()
		if not (0 <= self._cur < len(self.entries)):
			return
		if self._verified_var.get():
			self.entries[self._cur]["verified"] = True
		else:
			self.entries[self._cur].pop("verified", None)
		self.refresh_list()

	def swap_fields(self) -> None:
		"""Swap the author/title TARGET values (a C1 helper — the analyzer
		also proposes the swap itself; this is the manual nudge for when its
		proposal needs correcting)."""
		# A ∅-marked field is disabled and its text discarded at collect —
		# restore both sides first so the swap has text to work with.
		for r in ("author", "title"):
			f = self._fields[r]
			if f["cleared"]:
				self._field_cleared_ui(f, False)
				f["value"].set(f.get("pre_delete") or "")
		av = self._fields["author"]["value"]
		tv = self._fields["title"]["value"]
		a, t = av.get(), tv.get()
		av.set(t)
		tv.set(a)

	def _field_cleared_ui(self, f: dict, cleared: bool) -> None:
		"""Paint/strip the ∅ state on one field row (no value handling)."""
		f["cleared"] = cleared
		f["entry"].configure(state="disabled" if cleared else "normal")
		f["cap"].configure(foreground="#a40000" if cleared else f["cap_fg"])
		f["del_btn"].configure(text="↺" if cleared else "∅")

	def toggle_field_delete(self, role: str) -> None:
		"""∅ button: mark the field to be applied as EMPTY at apply time.

		For a proposal that is wrong while the correct value is unknown —
		keeping the wrong value would be worse than having none. The mark is
		stored as ``proposed[field]: null`` (compose_overlay merges it) and
		``bmf apply`` clears the stored value (_apply_fields). The disabled
		empty Entry is the visible reminder; ↺ restores the pre-delete text.
		"""
		f = self._fields.get(role)
		if f is None:
			return
		if not f["cleared"]:
			f["pre_delete"] = f["value"].get()
			f["value"].set("")
			self._field_cleared_ui(f, True)
			# A ∅ mark is a decision ("this value goes, correct one unknown")
			# — an undecided book would be skipped by apply entirely.
			if self._action_var.get() == "pending":
				self.set_action("accept")
		else:
			self._field_cleared_ui(f, False)
			f["value"].set(f.get("pre_delete") or "")

	# ------------------------------------------------------------------
	# Problems section + C19 merge panel
	# ------------------------------------------------------------------

	def _build_problems_section(self) -> None:
		"""The frame for ALL of the entry's diagnoses (most important first).

		The review header used to carry only the primary diagnosis plus
		"(+N more)" — which hid the load-bearing problem behind the counter.
		The body is ONE disabled Text widget, not a stack of labels: the lines
		are mouse-selectable and copyable (a disabled Tk Text still selects —
		only editing is blocked), and each diagnosis CODE is underlined and
		clickable, opening its detailed description (see catalog.CATEGORY_HELP).
		It is styled FLAT on the form background (no field look, no border,
		no scrollbar) and its height always equals the number of wrapped
		rows — it reads like the labels it replaced, the outer form canvas
		is the only scroller.
		"""
		self._problems_frame = ttk.LabelFrame(self._scroll_inner, text=_("Found problems"))
		self._problems_frame.pack(fill="x", padx=8, pady=4)
		txt = tk.Text(
			self._problems_frame, wrap="word", height=1, state="disabled",
			relief="flat", borderwidth=0, highlightthickness=0,
			background=self._form_bg, foreground=self._field_fg,
			selectbackground="#b3d7ff", selectforeground="#000000",
			cursor="arrow",
		)
		txt.pack(fill="x", expand=True, padx=(10, 8), pady=(4, 6))
		self._problems_txt = txt
		self._attach_text_copy_menu(txt)
		# Click on a code (not a drag-selection) opens its help popup.
		txt.bind("<ButtonRelease-1>", self._on_problems_click, add="+")
		# Wrapped continuation lines of a reason indent under the reason
		# column (code 14 + confidence 7 chars of the Text font).
		try:
			from tkinter.font import Font

			self._problems_indent = int(Font(font=txt.cget("font")).measure("0")) * 21
		except Exception:  # noqa: BLE001
			self._problems_indent = 170
		# Height must follow the content — but wrapped rows are only known
		# once the widget has its real width, so recount on every resize too.
		txt.bind("<Configure>", self._sync_problems_height, add="+")

	def _sync_problems_height(self, _event=None) -> None:
		"""Set the problems Text height to its wrapped row count.

		At load time the widget may still be unmapped (width 1) and every
		line counts as many wrapped rows; the <Configure> that comes with the
		_real_ width recounts. Setting height triggers another Configure —
		guarded by the != check, so the resize storm settles immediately.
		"""
		txt = self._problems_txt
		try:
			if txt.winfo_width() <= 10:
				return
			n = txt.count("1.0", "end-1c", "displaylines")
			rows = int(n[0]) if n else 1
		except Exception:  # noqa: BLE001
			return
		rows = max(1, rows)
		if int(txt.cget("height")) != rows:
			txt.configure(height=rows)

	def _on_problems_click(self, event) -> str | None:
		"""ButtonRelease over the problems list — plain click on a CODE opens
		its help popup; a drag (selection present) selects instead."""
		txt = self._problems_txt
		try:
			if txt.tag_ranges("sel"):
				return None  # the user dragged a selection — keep it
			idx = txt.index(f"@{event.x},{event.y}")
		except Exception:  # noqa: BLE001
			return None
		for tag in txt.tag_names():
			if not tag.startswith("code_"):
				continue
			rng = txt.tag_ranges(tag)
			for i in range(0, len(rng), 2):
				# index comparison via compare() — a lexical compare of
				# "1.10" vs "1.2" would order wrongly.
				if txt.compare(idx, ">=", rng[i]) and txt.compare(idx, "<", rng[i + 1]):
					self._show_category_help(txt.get(rng[i], rng[i + 1]))
					return None
		return None

	def _load_problems(self, e: dict) -> None:
		txt = self._problems_txt
		txt.configure(state="normal")
		txt.delete("1.0", "end")
		for tag in txt.tag_names():
			# tag_names() carries the built-ins (sel, …) — only ours are removed.
			if tag.startswith(("code_", "reason_")):
				txt.tag_delete(tag)
		diags = e.get("diagnoses") or ([e["diagnosis"]] if e.get("diagnosis") else [])
		for i, d in enumerate(sort_diagnoses(diags)):
			code = str(d.get("category") or "—")
			conf = str(d.get("confidence") or "").upper()
			colour = {"HIGH": "#a40000", "MEDIUM": "#b26800"}.get(conf, "#555753")
			reason = str(d.get("reason") or "")
			start = txt.index("end-1c")
			txt.insert("end", f"{code:<14}{conf:<7}{reason}\n")
			code_tag = f"code_{i}"
			txt.tag_add(code_tag, start, f"{start} + {len(code)}c")
			txt.tag_configure(code_tag, foreground=colour, underline=True)
			txt.tag_bind(code_tag, "<Enter>", lambda _ev: txt.configure(cursor="hand2"))
			txt.tag_bind(code_tag, "<Leave>", lambda _ev: txt.configure(cursor=""))
			reason_tag = f"reason_{i}"
			txt.tag_add(reason_tag, f"{start} + 21c", f"{start} lineend")
			txt.tag_configure(reason_tag, lmargin2=self._problems_indent)
		txt.configure(state="disabled")
		self._sync_problems_height()

	def _show_category_help(self, code: str) -> None:
		"""Popup with the detailed description of a diagnosis code.

		Non-modal (the review continues behind it), one instance at a time —
		clicking another code replaces the window. The body is the same
		readonly-selectable Text recipe as the problems list, so the
		description can be copied too.
		"""
		from .catalog import category_help

		if self._help_win is not None and self._help_win.winfo_exists():
			self._help_win.destroy()
		pair = category_help(code)
		if pair is None:
			title, detail = _("no description for this code"), ""
		else:
			title, detail = pair
		win = tk.Toplevel(self.root)
		self._help_win = win
		win.title(f"{code} — {title}")
		win.transient(self.root)
		body = self._style_text(tk.Text(win, wrap="word", width=74, state="disabled"))
		body.configure(state="normal")
		body.insert("1.0", f"{code} — {title}\n\n{detail}\n")
		body.configure(state="disabled")
		body.pack(fill="both", expand=True, padx=10, pady=(10, 2))
		try:
			# Size the window to its content: displaylines needs the real
			# wrap width, which only exists once the popup geometry settled.
			win.update_idletasks()
			n = body.count("1.0", "end-1c", "displaylines")
			body.configure(height=max(2, min(int(n[0]), 22)) if n else 12)
		except Exception:  # noqa: BLE001
			pass
		ttk.Label(win, text=_("select the text to copy it (Ctrl+C) — close with Esc"),
		          foreground="#555753").pack(anchor="w", padx=10, pady=(0, 8))
		self._attach_text_copy_menu(body)
		win.bind("<Escape>", lambda _ev: win.destroy())
		win.geometry(f"+{self.root.winfo_x() + 90}+{self.root.winfo_y() + 140}")
		win.focus_set()

	def _build_merge_section(self) -> None:
		"""The C19 merge frame — NOT packed here: :meth:`_load_merge_panel`
		packs it only for entries carrying ``proposed.merge_into``, so the
		scroll column stays honest for every other book."""
		self._merge_frame = ttk.LabelFrame(self._scroll_inner, text=_("Merge into the same-work folder (C19)"))
		self._merge_ctx: dict | None = None

	def _load_merge_panel(self, e: dict) -> None:
		self._merge_ctx = None
		merge_into = (e.get("proposed") or {}).get("merge_into")
		if not merge_into:
			self._merge_frame.pack_forget()
			return
		survivor_folder = self.library / str(merge_into)
		loser_folder = self.library / (e.get("path") or "")
		for w in self._merge_frame.winfo_children():
			w.destroy()
		self._merge_frame.pack(fill="x", padx=8, pady=4, before=self._covers_frame)
		if not survivor_folder.is_dir():
			ttk.Label(self._merge_frame, foreground="#a40000", text=_(
				"merge target not found on disk: {path}").format(path=merge_into),
			).pack(anchor="w", padx=6, pady=6)
			return
		if not loser_folder.is_dir():
			ttk.Label(self._merge_frame, foreground="#a40000", text=_(
				"this book's folder is missing on disk: {path}").format(path=e.get("path")),
			).pack(anchor="w", padx=6, pady=6)
			return
		survivor_meta = read_book_folder(survivor_folder)
		loser_meta = read_book_folder(loser_folder)
		self._merge_ctx = {
			"entry": e, "merge_into": str(merge_into),
			"survivor_meta": survivor_meta, "loser_meta": loser_meta,
			"survivor_folder": survivor_folder, "loser_folder": loser_folder,
		}
		self._render_merge_panel()

	def _merge_pick_side(self, field: str) -> str:
		"""Which radio side an override currently expresses ("s"/"l")."""
		e = self._merge_ctx["entry"]
		prop = e.get("proposed") or {}
		if field in prop:
			# an explicit pick; a null pick is the ∅ "keep the survivor's
			# (empty) state" mark → survivor side
			return "l" if prop[field] is not None else "s"
		# automatic: the survivor's value wins; the loser's fills a gap
		sv = merge_field_value(None, self._merge_ctx["survivor_meta"], field)
		return "s" if not _merge_value_empty(sv) else "l"

	def _merge_pick_field(self, field: str, side: str) -> None:
		"""One field source pick: writes into ``proposed`` (the same surface
		the apply branch consumes over the automatic merge result)."""
		ctx = self._merge_ctx
		e = ctx["entry"]
		prop = dict(e.get("proposed") or {})
		if side == "loser":
			val = merge_field_value(e, ctx["loser_meta"], field)
			if _merge_value_empty(val):
				prop[field] = None  # explicit empty — same ∅ semantics
			elif field == "series":
				name, idx = val if isinstance(val, tuple) else (val, "")
				prop["series"] = name or None
				if idx:
					prop["series_index"] = str(idx)
				else:
					prop.pop("series_index", None)
			else:
				prop[field] = list(val) if isinstance(val, list) else val
		else:  # survivor
			sv = merge_field_value(None, ctx["survivor_meta"], field)
			if _merge_value_empty(sv):
				prop[field] = None  # keep it empty — do not pull the loser's
			else:
				prop.pop(field, None)  # automatic already yields the survivor's value
				if field == "series":
					prop.pop("series_index", None)
		e["proposed"] = prop
		self._mark_dirty()
		self._render_merge_panel()

	def _merge_pick_cover(self, choice: str) -> None:
		ctx = self._merge_ctx
		e = ctx["entry"]
		prop = dict(e.get("proposed") or {})
		if choice == "auto":
			prop.pop("cover_source", None)
		else:
			prop["cover_source"] = choice  # survivor | loser | loser_epub
		e["proposed"] = prop
		self._mark_dirty()
		self._render_merge_panel()

	def _merge_reset(self) -> None:
		"""Drop every explicit pick — back to the pure automatic merge."""
		e = self._merge_ctx["entry"]
		skip = {f for f, _ in MERGE_FIELDS} | {"cover_source"}
		e["proposed"] = {
			k: v for k, v in (e.get("proposed") or {}).items() if k not in skip
		}
		self._mark_dirty()
		self._render_merge_panel()

	def _render_merge_panel(self) -> None:
		ctx = self._merge_ctx
		if ctx is None:
			return
		e = ctx["entry"]
		prop = e.get("proposed") or {}
		box = ttk.Frame(self._merge_frame)
		box.pack(fill="x", padx=6, pady=4)

		# Context: the survivor, why it survives, and the rest of the cluster.
		from .isbn import canonicalize

		s_isbn = canonicalize(ctx["survivor_meta"].isbn or "") if ctx["survivor_meta"].isbn else ""
		l_isbn = canonicalize(ctx["loser_meta"].isbn or "") if ctx["loser_meta"].isbn else ""
		why = _("valid ISBN") if (s_isbn and not l_isbn) else _("lowest calibre id")
		head = ttk.Frame(box)
		head.pack(fill="x", pady=(0, 2))
		ttk.Label(head, text=_("survivor:")).pack(side="left")
		survivor_link = ttk.Label(head, text=ctx["merge_into"], foreground="#1a5fb4",
		                          cursor="hand2")
		survivor_link.pack(side="left", padx=(4, 8))
		survivor_link.bind("<Button-1>", lambda _e: open_folder_in_manager(ctx["survivor_folder"]))
		_Tooltip(survivor_link, _("Open the survivor's folder in the file manager"))
		ttk.Label(head, text=_("(picked by: {why})").format(why=why)).pack(side="left")
		others = [
			x.get("path") for x in self.entries
			if x is not e and (x.get("proposed") or {}).get("merge_into") == ctx["merge_into"]
		]
		if others:
			ttk.Label(box, text=_("also merging into the same survivor: {paths}").format(
				paths=", ".join(str(p) for p in others)),
			).pack(anchor="w", pady=(0, 4))

		# Field comparison + source picks (only the differing rows).
		grid = ttk.Frame(box)
		grid.pack(fill="x", pady=2)
		grid.columnconfigure(3, weight=1)
		ttk.Label(grid, text=_("field")).grid(row=0, column=0, sticky="w")
		ttk.Label(grid, text=_("survivor")).grid(row=0, column=1, sticky="w", padx=(8, 8))
		ttk.Label(grid, text=_("this book")).grid(row=0, column=2, sticky="w", padx=(0, 8))
		ttk.Label(grid, text=_("result")).grid(row=0, column=3, sticky="w")
		ttk.Label(grid, text=_("source")).grid(row=0, column=4, sticky="w", padx=(8, 0))
		for r, row in enumerate(merge_projection_rows(
				ctx["survivor_meta"], ctx["loser_meta"], e), start=1):
			fg = "#1a5fb4" if row["picked"] else None
			ttk.Label(grid, text=row["label"]).grid(row=r, column=0, sticky="w")
			ttk.Label(grid, text=row["s_text"], relief="sunken", anchor="w",
			          width=26).grid(row=r, column=1, sticky="we", padx=(8, 4), pady=1)
			ttk.Label(grid, text=row["l_text"], relief="sunken", anchor="w",
			          width=26).grid(row=r, column=2, sticky="we", padx=(0, 4), pady=1)
			ttk.Label(grid, text=row["e_text"], anchor="w", foreground=fg,
			          font=self._link_font if row["picked"] else None,
			          ).grid(row=r, column=3, sticky="w")
			var = tk.StringVar(value=self._merge_pick_side(row["field"]))
			rad = ttk.Frame(grid)
			rad.grid(row=r, column=4, sticky="w", padx=(8, 0))
			ttk.Radiobutton(rad, text="⬅", value="s", variable=var, width=3,
			                command=lambda f=row["field"]: self._merge_pick_field(f, "survivor"),
			                ).pack(side="left")
			ttk.Radiobutton(rad, text="➡", value="l", variable=var, width=3,
			                command=lambda f=row["field"]: self._merge_pick_field(f, "loser"),
			                ).pack(side="left")
			_Tooltip(rad, _("result source: survivor (⬅) or this book (➡); "
			                "the pick lands in proposed and apply writes it "
			                "over the automatic result"))

		# Cover row: thumbnails of both sides + the source choice.
		cvr = ttk.Frame(box)
		cvr.pack(fill="x", pady=(6, 2))
		ttk.Label(cvr, text=_("cover:")).pack(side="left", anchor="n")

		s_cover = cover_paths(self.library, ctx["merge_into"])[0]
		l_cover = cover_paths(self.library, e.get("path", ""))[0]
		choices = [("auto", _("automatic")), ("survivor", _("survivor's"))]
		cover_var = tk.StringVar(value=(prop.get("cover_source") or "auto"))
		for side, path in ((_("survivor's"), s_cover), (_("this book's"), l_cover)):
			cell = ttk.Frame(cvr)
			cell.pack(side="left", padx=6)
			try:
				pil = load_thumb(path, 96, 128) if path.is_file() else None
			except Exception:  # noqa: BLE001
				pil = None
			if pil is not None:
				photo = ImageTk.PhotoImage(pil)
				self._cover_photos[f"_merge_{side}"] = photo  # GC pin
				ttk.Label(cell, image=photo, relief="sunken").pack()
			else:
				ttk.Label(cell, text=_("no cover.jpg"), relief="sunken",
				          width=10, anchor="center").pack()
			ttk.Label(cell, text=side).pack()
		has_embedded = False
		try:
			primary = ctx["loser_folder"] / ctx["loser_meta"].primary_file \
				if ctx["loser_meta"].primary_file else None
			if primary is not None and primary.suffix.lower() == ".epub" and primary.is_file():
				has_embedded = epub_cover_image(primary) is not None
		except Exception:  # noqa: BLE001
			has_embedded = False
		choices.append(("loser", _("this book's cover.jpg")))
		if has_embedded:
			choices.append(("loser_epub", _("this book's embedded (EPUB)")))
		pick = ttk.Frame(cvr)
		pick.pack(side="left", padx=10, anchor="n")
		for value, label in choices:
			ttk.Radiobutton(pick, text=label, value=value, variable=cover_var,
			                command=lambda v=value: self._merge_pick_cover(v),
			                ).pack(anchor="w")

		# File fate + reset.
		for line in merge_file_plan(ctx["loser_folder"], ctx["survivor_folder"],
		                            prop.get("cover_source")):
			ttk.Label(box, text=line, foreground="#555753").pack(anchor="w")
		ttk.Button(box, text=_("Reset to automatic"), command=self._merge_reset,
		           ).pack(anchor="w", pady=(6, 0))

	def act_merge(self) -> None:
		"""Ctrl+R: approve the C19 merge (fold this folder into merge_into)."""
		self.set_action("merge")

	# ------------------------------------------------------------------
	# Bulk edit (multi-selection)
	# ------------------------------------------------------------------

	def _bulk_selection_indices(self) -> list[int]:
		"""Entry indices of the list's multi-selection (sorted, deduplicated)."""
		out = set()
		for iid in self.tree.selection_get():
			if iid.lstrip("-").isdigit():
				i = int(iid)
				if 0 <= i < len(self.entries):
					out.add(i)
		return sorted(out)

	def bulk_edit(self) -> None:
		"""Ctrl+E: set author / series for every SELECTED list row at once.

		The multi-selection (Ctrl+click toggle, Shift+click range) lives in
		the list; the detail pane keeps showing the focus row. The dialog's
		value merges into each selected entry's ``proposed``
		(:func:`apply_bulk_field`); pending entries become ``accept`` — a
		proposal without a decision is skipped by apply. Save (Ctrl+S)
		writes review.yaml as usual. The dialog holds a grab while open, so
		the main window's shortcuts stay inert under it.
		"""
		idxs = self._bulk_selection_indices()
		if not idxs:
			self._flash(_("select books first (Ctrl+click, Shift+click)"))
			return
		# Bank the current form FIRST: its values would otherwise win over
		# the bulk value at the next collect (the form still holds the old
		# field text until the reload below).
		self._collect_current()
		win = tk.Toplevel(self.root)
		win.title(_("Bulk edit"))
		win.transient(self.root)
		win.resizable(False, False)
		ttk.Label(win, text=_("Apply to {n} selected books:").format(n=len(idxs)),
		          anchor="w").pack(fill="x", padx=10, pady=(10, 4))
		field_var = tk.StringVar(value="author")
		frm = ttk.Frame(win)
		frm.pack(fill="x", padx=10)
		ttk.Radiobutton(frm, text=_("Author"), value="author",
		                variable=field_var).pack(side="left", padx=(0, 12))
		ttk.Radiobutton(frm, text=_("Series"), value="series",
		                variable=field_var).pack(side="left")
		value_var = tk.StringVar()
		entry = ttk.Entry(win, textvariable=value_var, width=44)
		entry.pack(fill="x", padx=10, pady=4)
		# The same autocomplete as the field itself — the pool follows the
		# radio, so switching to Series completes against series names.
		_Autocomplete(entry, lambda: sorted(
			self._vocab_authors if field_var.get() == "author" else self._vocab_series))
		del_var = tk.BooleanVar(value=False)
		ttk.Checkbutton(win, text=_("∅ apply as EMPTY (delete the field)"),
		                variable=del_var).pack(anchor="w", padx=10, pady=2)
		hint = ttk.Label(win, text="", foreground="#a00")
		hint.pack(anchor="w", padx=10)

		def _apply(_event=None):
			v = value_var.get().strip()
			if not v and not del_var.get():
				hint.configure(text=_("enter a value or tick ∅"))
				return
			field = field_var.get()
			n = apply_bulk_field(self.entries, idxs, field, v, delete=del_var.get())
			if v and not del_var.get():
				(self._vocab_authors if field == "author" else self._vocab_series).add(v)
			self._mark_dirty()
			# The current book's form may hold one of the just-overwritten
			# values — reload it from the merged proposal.
			if 0 <= self._cur < len(self.entries) and self._cur in idxs:
				self._load_book(self._cur)
			self.refresh_list()
			self._flash(_("bulk edit: {n} books").format(n=n))
			win.destroy()
			return "break"

		btns = ttk.Frame(win)
		btns.pack(fill="x", padx=10, pady=10)
		ttk.Button(btns, text=_("Apply"), command=_apply).pack(side="left", padx=2)
		ttk.Button(btns, text=_("Cancel"), command=win.destroy).pack(side="left", padx=2)
		# Return accepts the value (an open autocomplete popup consumes it
		# first — its own Return binding runs on the Entry, closer in the
		# bindtag chain than the toplevel).
		win.bind("<Return>", _apply)
		win.bind("<Escape>", lambda _e: (win.destroy(), "break")[1])
		# Centre over the main window, then hand the keyboard to the dialog.
		self.root.update_idletasks()
		win.update_idletasks()
		x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
		y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_height()) // 2
		win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
		win.grab_set()
		entry.focus_set()

	def _modal_over_main(self, win, focus=None) -> None:
		"""Centre *win* over the main window, grab it, focus *focus*."""
		self.root.update_idletasks()
		win.update_idletasks()
		x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
		y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_height()) // 2
		win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
		win.grab_set()
		if focus is not None:
			focus.focus_set()

	def bulk_accept(self) -> None:
		"""Ctrl+Shift+A: accept every SELECTED list row at once.

		An explicit selection IS the decision — unlike a bulk field edit
		(which only decides pending entries) this overrides any previous
		decision on the selected books. Save (Ctrl+S) writes review.yaml as
		usual; rows hidden by an active action filter drop out of the view,
		exactly like a single-book accept.
		"""
		idxs = self._bulk_selection_indices()
		if not idxs:
			self._flash(_("select books first (Ctrl+click, Shift+click)"))
			return
		self._collect_current()
		n = apply_bulk_action(self.entries, idxs, "accept")
		self._mark_dirty()
		if 0 <= self._cur < len(self.entries) and self._cur in idxs:
			self._load_book(self._cur)
		self.refresh_list()
		self._flash(_("bulk accept: {n} books").format(n=n))

	def bulk_toggle_verified(self) -> None:
		"""Ctrl+Shift+O: flip the OK mark on every SELECTED list row.

		Toggle semantics like the single-book Ctrl+O: sets the mark, and
		clears it when EVERY selected book already carries it.
		"""
		idxs = self._bulk_selection_indices()
		if not idxs:
			self._flash(_("select books first (Ctrl+click, Shift+click)"))
			return
		self._collect_current()
		sel = [self.entries[i] for i in idxs if 0 <= i < len(self.entries)]
		value = not all(e.get("verified") for e in sel)
		n = apply_bulk_verified(self.entries, idxs, value)
		self._mark_dirty()
		if 0 <= self._cur < len(self.entries) and self._cur in idxs:
			self._load_book(self._cur)
		self.refresh_list()
		self._flash((_("bulk verified: {n} books") if value
		             else _("bulk verify cleared: {n} books")).format(n=n))

	def bulk_clear_action(self) -> None:
		"""Ctrl+Shift+D: return every SELECTED list row to pending (action null).

		The mass VETO for pre-filled proposals — C17 file deletions arrive as
		``action: delete``: filter the list by the delete state, select what
		should survive, one keystroke un-decides it (the mirror of
		Ctrl+Shift+A's bulk accept).
		"""
		idxs = self._bulk_selection_indices()
		if not idxs:
			self._flash(_("select books first (Ctrl+click, Shift+click)"))
			return
		self._collect_current()
		n = apply_bulk_action(self.entries, idxs, None)
		self._mark_dirty()
		if 0 <= self._cur < len(self.entries) and self._cur in idxs:
			self._load_book(self._cur)
		self.refresh_list()
		self._flash(_("bulk decision cleared: {n} books").format(n=n))

	def remove_from_review(self) -> None:
		"""Ctrl+Shift+R: drop every SELECTED entry from review.yaml entirely.

		Neither delete nor accept — the books simply leave the review file
		(nothing happens on disk; a later analyze/clean re-flags a book whose
		problem persists). Saved IMMEDIATELY like a merge: the file and the
		in-memory state must agree, or the next apply would act on entries
		the user believes are gone.
		"""
		idxs = self._bulk_selection_indices()
		if not idxs:
			self._flash(_("select books first (Ctrl+click, Shift+click)"))
			return
		self._collect_current()
		sel = [self.entries[i] for i in idxs if 0 <= i < len(self.entries)]
		if not sel:
			return
		preview = "\n".join(
			str((e.get("current") or {}).get("title") or e.get("path") or "?")
			for e in sel[:10]
		)
		if len(sel) > 10:
			preview += f"\n… (+{len(sel) - 10})"
		if not messagebox.askyesno(
			_("Remove from review"),
			_("Remove {n} entries from review.yaml?\n"
			  "No files are touched; a later analyze re-flags a book whose problem persists.\n\n{preview}").format(n=len(sel), preview=preview),
		):
			return
		dropped_uuids = {e["uuid"] for e in sel if e.get("uuid")}
		drop_idxs = set(idxs)
		self.entries = [e for i, e in enumerate(self.entries) if i not in drop_idxs]
		# The in-memory library index must not re-serve the removed entries
		# through the "+ library" search (same pruning as a merge; the SQLite
		# cache rows stay — unlike a merge, nothing changed on disk).
		for u in dropped_uuids:
			self._lib_uuids.pop(u, None)
			self._thumbs_pil.pop(u, None)
			self._thumbs_photo.pop(u, None)
			self._big_thumbs.pop(u, None)
		if dropped_uuids:
			self._lib_index = [(e, hay) for e, hay in self._lib_index
			                   if e.get("uuid") not in dropped_uuids]
		if self._do_save():
			self._dirty = False
		if not self.entries:
			self.refresh_list()
			self._flash(_("review is empty"))
			return
		new_cur = min(min(idxs), len(self.entries) - 1)
		self.refresh_list()
		self.tree.selection_set(str(new_cur), silent=True)
		self.tree.see(str(new_cur))
		self._cur = new_cur
		self._load_book(new_cur)
		self._flash(_("removed {n} entries from review").format(n=len(sel)))

	def bulk_delete_covers(self) -> None:
		"""Ctrl+Shift+M: delete the covers of every SELECTED list row at once.

		A dialog mirroring the per-book Delete-checked button: the sidecar
		cover.jpg (default on), its .bak, and the covers EMBEDDED in the EPUB
		files. Immediate file operations exactly like the single-book path —
		the embedded strip rewrites the ebooks, so it asks once with the file
		list first. A proposed cover_url is dropped from the touched entries
		so the next apply cannot re-download what was just removed.
		"""
		idxs = self._bulk_selection_indices()
		if not idxs:
			self._flash(_("select books first (Ctrl+click, Shift+click)"))
			return
		self._collect_current()
		win = tk.Toplevel(self.root)
		win.title(_("Bulk cover delete"))
		win.transient(self.root)
		win.resizable(False, False)
		ttk.Label(win, text=_("Delete covers of {n} selected books:").format(n=len(idxs)),
		          anchor="w").pack(fill="x", padx=10, pady=(10, 4))
		cover_var = tk.BooleanVar(value=True)
		bak_var = tk.BooleanVar(value=False)
		emb_var = tk.BooleanVar(value=False)
		ttk.Checkbutton(win, text=_("cover.jpg (current cover)"),
		                variable=cover_var).pack(anchor="w", padx=14)
		ttk.Checkbutton(win, text=_("cover.jpg.bak (backup cover)"),
		                variable=bak_var).pack(anchor="w", padx=14)
		ttk.Checkbutton(win, text=_("covers embedded in the ebook files (EPUB only)"),
		                variable=emb_var).pack(anchor="w", padx=14)
		hint = ttk.Label(win, text="", foreground="#a00")
		hint.pack(anchor="w", padx=10)

		def _apply(_event=None):
			if not (cover_var.get() or bak_var.get() or emb_var.get()):
				hint.configure(text=_("tick at least one option"))
				return
			fmt_files: list[Path] = []
			if emb_var.get():
				for i in idxs:
					if 0 <= i < len(self.entries):
						fmt_files.extend(list_format_files(
							self.library / self.entries[i].get("path", "")))
				if fmt_files and not messagebox.askyesno("bmf gui",
						_("Strip the embedded cover from these ebooks?\n"
						  "(the ebook files themselves stay)\n\n{files}").format(
						files="\n".join(f"  • {p.name}" for p in fmt_files)),
						parent=win):
					return
			removed, stripped = execute_bulk_cover_delete(
				self.entries, idxs, self.library,
				covers=cover_var.get(), baks=bak_var.get(), embedded=emb_var.get())
			win.destroy()
			self._mark_dirty()
			self._reload_thumbs(idxs)
			if 0 <= self._cur < len(self.entries) and self._cur in idxs:
				self._refresh_covers()
				self._refresh_formats()  # the format radios / embedded covers changed too
			self.refresh_list()
			msg = _("deleted {n}").format(n=removed) if (cover_var.get() or bak_var.get()) else ""
			if emb_var.get():
				part = _("covers stripped {stripped}/{total}").format(
					stripped=stripped, total=len(fmt_files))
				if stripped < len(fmt_files):
					part += _(" (EPUB only)")
				msg = f"{msg}; {part}" if msg else part
			self._flash(msg)
			return "break"

		btns = ttk.Frame(win)
		btns.pack(fill="x", padx=10, pady=10)
		ttk.Button(btns, text=_("Delete"), command=_apply).pack(side="left", padx=2)
		ttk.Button(btns, text=_("Cancel"), command=win.destroy).pack(side="left", padx=2)
		win.bind("<Return>", _apply)
		win.bind("<Escape>", lambda _e: (win.destroy(), "break")[1])
		self._modal_over_main(win)

	def merge_selected(self) -> None:
		"""Ctrl+J: merge the SELECTED books into one folder / review entry.

		The placement merge (apply) only fires when two folders happen to
		collide at the same pattern target; this is the explicit "these are
		the same work, join them" command. The dialog picks the SURVIVOR
		(default: the focused row — the detail pane shows it) and shows a
		per-FIELD grid: every metadata field of every selected book, one
		radio per cell, so the merged book keeps exactly the values the user
		picks from either side (:func:`merge_field_value` — a decided
		proposal counts as that book's value). A same-work warning
		(``mover.same_book``) flags a probably-wrong merge without blocking
		it — here the user decides, unlike the automatic placement merge.
		"""
		idxs = self._bulk_selection_indices()
		if len(idxs) < 2:
			self._flash(_("select at least two books to merge"))
			return
		self._collect_current()
		# Metas for the same-work warning and the field grid — a handful of
		# metadata.json reads, cheap next to the merge itself (execute_merge
		# re-reads anyway: each merged write replaces the survivor's metadata
		# on disk).
		metas = {}
		for i in idxs:
			try:
				metas[i] = read_book_folder(self.library / self.entries[i].get("path", ""))
			except Exception:  # noqa: BLE001
				metas[i] = None
		focus_iid = self.tree.focus()
		focus_idx = int(focus_iid) if (focus_iid or "").lstrip("-").isdigit() else -1
		if focus_idx not in idxs:
			focus_idx = idxs[0]
		win = tk.Toplevel(self.root)
		win.title(_("Merge books"))
		win.transient(self.root)
		win.resizable(False, False)
		ttk.Label(win, text=_("Merge {n} books into one — pick the survivor:").format(n=len(idxs)),
		          anchor="w").pack(fill="x", padx=10, pady=(10, 4))
		choice = tk.StringVar(value=str(focus_idx))
		for i in idxs:
			ttk.Radiobutton(win, text=entry_label(self.entries[i]), value=str(i),
			                variable=choice).pack(anchor="w", padx=14)
		warn = ttk.Label(win, text="", foreground="#a40000", wraplength=560, justify="left")
		warn.pack(anchor="w", padx=10, pady=(4, 0))

		def _check(*_args) -> None:
			w = int(choice.get())
			wm = metas.get(w)
			bad = [entry_label(self.entries[i]) for i in idxs
			       if i != w and wm is not None and metas.get(i) is not None
			       and not same_book(wm, metas[i])]
			warn.configure(text=(
				_("⚠ does not look like the same work:\n{labels}").format(
					labels="\n".join(bad)) if bad else ""))

		choice.trace_add("write", _check)
		_check()

		# --- per-field grid: rows = fields, columns = books, one radio per cell
		raw: dict[str, dict[int, object]] = {}
		for field, _label in MERGE_FIELDS:
			raw[field] = {i: (merge_field_value(self.entries[i], metas[i], field)
			                  if metas.get(i) is not None else None)
			              for i in idxs}
		ttk.Label(win, text=_("Field-by-field: pick which book's value the merged book keeps"),
		          anchor="w").pack(fill="x", padx=10, pady=(8, 2))
		grid = ttk.Frame(win)
		grid.pack(fill="x", padx=10)
		ttk.Label(grid, text="").grid(row=0, column=0, sticky="w")
		for c, i in enumerate(idxs):
			ttk.Label(grid, foreground="#555",
			          text=Path(self.entries[i].get("path", "")).name).grid(
				row=0, column=c + 1, sticky="w", padx=8)
		row_vars: dict[str, tk.StringVar] = {}
		for r, (field, label) in enumerate(MERGE_FIELDS, start=1):
			ttk.Label(grid, text=label).grid(row=r, column=0, sticky="w")
			# Default: the survivor's value when it has one, else the first
			# non-empty cell — the untouched grid reproduces the automatic
			# merge's gap-filling, so confirming as-is loses nothing.
			if not _merge_value_empty(raw[field].get(focus_idx)):
				default = focus_idx
			else:
				default = next((i for i in idxs
				                if not _merge_value_empty(raw[field].get(i))), focus_idx)
			var = tk.StringVar(value=str(default))
			row_vars[field] = var
			for c, i in enumerate(idxs):
				ttk.Radiobutton(grid, text=merge_cell_text(field, raw[field][i]),
				                value=str(i), variable=var).grid(
					row=r, column=c + 1, sticky="w", padx=8)
		ttk.Label(win, text=_(
			"Defaults follow the survivor; a field the survivor lacks takes the first "
			"value found. ∅ = keep the field empty. Picked values are written to the "
			"merged book and override a conflicting proposal."),
			wraplength=560, justify="left", foreground="#555").pack(
			fill="x", padx=10, pady=(6, 0))
		ttk.Label(win, text=_(
			"The survivor keeps its folder and review entry; the other books' files "
			"move in and their folders are removed. review.yaml is saved right after "
			"the merge."), wraplength=560, justify="left", foreground="#555").pack(
			fill="x", padx=10, pady=(4, 0))

		def _apply(_event=None):
			w = int(choice.get())
			# parent=win: without it the confirm attaches to the ROOT and this
			# grabbed dialog covers it — the merge looks hung until the hidden
			# box is found and dismissed.
			if not messagebox.askyesno(
					"bmf gui", _("Merge {n} books into {label}?").format(
						n=len(idxs) - 1, label=entry_label(self.entries[w])),
					parent=win):
				return
			losers = [i for i in idxs if i != w]
			values = {field: raw[field][int(var.get())]
			          for field, var in row_vars.items()
			          if not all(_merge_value_empty(raw[field].get(i)) for i in idxs)}
			winner_path = str(self.library / self.entries[w].get("path", ""))
			loser_paths = [str(self.library / self.entries[i].get("path", ""))
			               for i in losers]
			outcome = execute_merge(self.entries, w, losers, self.library,
			                        values=values)
			win.destroy()
			self._after_merge(outcome, winner_path, loser_paths)
			return "break"

		btns = ttk.Frame(win)
		btns.pack(fill="x", padx=10, pady=10)
		ttk.Button(btns, text=_("Merge"), command=_apply).pack(side="left", padx=2)
		ttk.Button(btns, text=_("Cancel"), command=win.destroy).pack(side="left", padx=2)
		win.bind("<Return>", _apply)
		win.bind("<Escape>", lambda _e: (win.destroy(), "break")[1])
		self._modal_over_main(win)

	def _after_merge(self, outcome: MergeOutcome, winner_path: str,
	                 loser_paths: list[str]) -> None:
		"""Post-merge cleanup: library index, thumbnails, selection, save.

		The losers' folders are gone on disk, so review.yaml (and the
		in-memory library index) must agree RIGHT NOW, not at the next
		Ctrl+S — a stale entry would fail the next apply with "folder not
		found", and the "+ library" search would re-serve a ghost.
		"""
		if not outcome.merged_count and not outcome.failures:
			self._flash(_("nothing merged"))
			return
		for u in outcome.dropped_uuids:
			self._lib_uuids.pop(u, None)
			self._thumbs_pil.pop(u, None)
			self._thumbs_photo.pop(u, None)
			self._big_thumbs.pop(u, None)
		if outcome.dropped_uuids:
			drop = set(outcome.dropped_uuids)
			self._lib_index = [(e, hay) for e, hay in self._lib_index
			                   if e.get("uuid") not in drop]
		# Best-effort cache drop, matching apply's merge (a stale row for a
		# removed folder is only litter, but no reason to keep it).
		try:
			cache = Cache(self.cfg.cache_db)
			try:
				cache.invalidate_many([*loser_paths, winner_path])
			finally:
				cache.close()
		except Exception:  # noqa: BLE001
			log.debug("cache invalidation after merge failed", exc_info=True)
		new_cur = next((i for i, e in enumerate(self.entries) if e is outcome.winner), 0)
		if self._do_save():
			self._dirty = False
		self.refresh_list()
		self.tree.selection_set(str(new_cur), silent=True)
		self.tree.see(str(new_cur))
		# _load_book alone does NOT move _cur (that is _select_index's job,
		# with the collect-guard we do not want here) — and _refresh_covers
		# indexes entries[_cur], so a stale _cur would crash after the list
		# just shrank.
		self._cur = new_cur
		self._load_book(new_cur)
		self._reload_thumbs([new_cur])  # the survivor may have gained a cover
		if outcome.failures:
			messagebox.showwarning("bmf gui", _("could not merge:\n{list}").format(
				list="\n".join(outcome.failures)))
		self._flash(_("merged {n} books, {m} files moved").format(
			n=outcome.merged_count, m=outcome.moved_files))

	def _copy_current(self, role: str) -> None:
		# Copies whatever the RO label currently DISPLAYS (mode-dependent).
		self._fields[role]["value"].set(self._fields[role]["current"].get())

	def copy_current_to_focused(self) -> None:
		w = self.focus_get_safe()
		for role, f in self._fields.items():
			if f["entry"] is w:
				self._copy_current(role)
				return

	# ------------------------------------------------------------------
	# Navigation
	# ------------------------------------------------------------------

	def _step(self, delta: int) -> None:
		idxs = self._filtered_indices()
		if not idxs:
			return
		try:
			pos = idxs.index(self._cur)
			nxt = idxs[(pos + delta) % len(idxs)]
		except ValueError:
			# The current book is not in the filtered view (typically its
			# action just changed under an active "Akce:" filter and a
			# refresh dropped the row). Continue from where it WOULD sit —
			# the old fallback (start of list for PgDn / end for PgUp) is
			# the "indicator jumped back to the first book" jump.
			pos = bisect.bisect_left(idxs, self._cur) - (1 if delta < 0 else 0)
			nxt = idxs[pos % len(idxs)]
		# Silent selection + the one explicit load below: going through the
		# select callback here as well would load the book twice (callback
		# runs _select_index, then _step's own call reloads).
		self.tree.selection_set(str(nxt), silent=True)
		self.tree.see(str(nxt))
		self._select_index(nxt, keep_focus=True)

	def next_book(self): self._step(1)
	def prev_book(self): self._step(-1)

	# ------------------------------------------------------------------
	# Save / quit
	# ------------------------------------------------------------------

	def save(self) -> None:
		self._collect_current()
		if not self._do_save():
			return
		self._dirty = False
		# Non-intrusive confirmation (the modal dialog interrupted the flow):
		# the status line carries it for a few seconds, then reverts.
		self._flash(_("saved → {path}").format(path=self.review_path), seconds=4)

	def _do_save(self) -> bool:
		# Library-loaded entries ride along only when the user changed them —
		# untouched books pulled in by a search are view-only.
		text = render_review_text(entries_to_write(self.entries, self._lib_uuids))
		tmp = self.review_path.with_suffix(self.review_path.suffix + ".tmp")
		bak = self.review_path.with_suffix(self.review_path.suffix + ".bak")
		try:
			tmp.write_text(text, encoding="utf-8")
			if self.review_path.is_file():
				shutil.copy2(self.review_path, bak)
			os.replace(tmp, self.review_path)
		except OSError as e:
			messagebox.showerror("bmf gui", _("save failed: {err}").format(err=e))
			return False
		return True

	def quit_app(self) -> None:
		if self._dirty:
			choice = messagebox.askyesnocancel("bmf gui", _("Save changes before quitting?"))
			if choice is None:
				return
			if choice:
				self._collect_current()
				if not self._do_save():
					return
		self._alive = False
		self._hide_cover_popup()
		self.root.destroy()

	# ------------------------------------------------------------------
	# Covers
	# ------------------------------------------------------------------

	def _refresh_covers(self) -> None:
		e = self.entries[self._cur]
		idx = self._cur
		cover_path, bak_path = cover_paths(self.library, e.get("path", ""))
		url = (e.get("proposed") or {}).get("cover_url")
		files = list_format_files(self.library / e.get("path", ""))

		def work():
			# Phase 1 — the sidecar covers (fast, local file reads).
			cur = load_thumb(cover_path, 240, 320) if cover_path.is_file() else None
			bak = load_thumb(bak_path, 240, 320) if bak_path.is_file() else None
			rec = None
			if url:
				rec = load_thumb(fetch_url_bytes(url) or b"", 240, 320)
			info = analyze_cover(cover_path) if cover_path.is_file() else None
			if self._alive:
				self._after(lambda: self._apply_covers(idx, cur, bak, rec, info, bool(url)))
			# Phase 2 — each format's EMBEDDED cover (a calibre subprocess per
			# file, so it can take seconds; painted separately so the base
			# covers are not held hostage by it).
			fmt_covers = []
			for f in files:
				if not self._alive:
					return
				pil = embedded_cover_thumb(f, 240, 320)
				if pil is not None:
					fmt_covers.append((f, pil))
			if self._alive:
				self._after(lambda: self._apply_fmt_covers(idx, fmt_covers))

		threading.Thread(target=work, daemon=True).start()

	def _fit_photo(self, pil, lbl):
		"""PhotoImage for *pil*, downscaled to the cover slot's current width.

		The slot width follows the pane (see _sync_cover_slots); a 240px
		thumbnail in a narrower slot would otherwise be clipped at the sides.
		Returns None on any failure (caller shows the placeholder text).
		"""
		try:
			img = pil
			w = lbl.master.winfo_width() - 10
			if 0 < w < pil.width:
				img = pil.copy()
				img.thumbnail((w, self.COVER_SLOT_H))
			return ImageTk.PhotoImage(img)
		except Exception:  # noqa: BLE001
			return None

	def _apply_covers(self, idx, cur, bak, rec, info, has_url) -> None:
		if not self._alive or self._cur != idx:
			return  # user already switched to another book — drop stale paint
		self._cover_photos.clear()
		imgs = [cur, bak, rec]
		caps = []
		caps.append(_("generated") if (info and info.is_generated) else (_("ok") if cur else _("missing")))
		caps.append(_(".bak backup"))
		caps.append(_("recommended") if has_url else _("no URL"))
		for lbl, _cap, pil in zip(self._cover_imgs, self._cover_caps, imgs, strict=False):
			if pil is not None:
				photo = self._fit_photo(pil, lbl)
				if photo is not None:
					self._cover_photos[id(pil)] = photo
					lbl.configure(image=photo, text="")
					continue
			lbl.configure(image="", text=_("(no preview)"))
		for _cap_lbl, text in zip(self._cover_caps, caps, strict=False):
			_cap_lbl.configure(text=text)

	def _apply_fmt_covers(self, idx, fmt_covers) -> None:
		"""Paint the per-format embedded covers (phase 2 of _refresh_covers)."""
		if not self._alive or self._cur != idx:
			return
		for child in self._fmt_cover_row.winfo_children():
			child.destroy()
		self._del_formats = {}
		if not fmt_covers:
			ttk.Label(self._fmt_cover_row, text=_("(none / calibre unavailable)")).pack(side="left")
			return
		for path, pil in fmt_covers:
			var = tk.BooleanVar(value=False)
			is_epub = path.suffix.lower() == ".epub"
			lbl, _cap = self._cover_cell(
				self._fmt_cover_row, path.name, var,
				(
					_("Strip the embedded cover from {name} (the ebook file stays)").format(name=path.name)
					if is_epub else
					_("The embedded cover cannot be stripped from {ext} — EPUB only").format(
						ext=path.suffix or _("file"))
				),
				check_enabled=is_epub,
			)
			photo = self._fit_photo(pil, lbl)
			if photo is None:
				continue
			self._cover_photos[f"fmt:{path}"] = photo
			lbl.configure(image=photo, text="")
			self._del_formats[str(path)] = var
		# Tab must never land on the (dynamically created) checkboxes.
		self._trap_subtree(self._fmt_cover_row)
		# The rebuilt row must re-fit its slot widths (n may have changed).
		self.root.after_idle(self._sync_cover_slots)

	def cover_new(self) -> None:
		e = self.entries[self._cur]
		url = (e.get("proposed") or {}).get("cover_url")
		if not url:
			self._flash(_("no recommended cover (cover_url)"))
			return
		cover_path, _bak_path = cover_paths(self.library, e.get("path", ""))
		if download_cover(url, cover_path):
			self._flash(_("cover applied"))
		else:
			self._flash(_("cover download failed"))
		self._refresh_covers()
		self._reload_list_thumb()

	def cover_restore_bak(self) -> None:
		e = self.entries[self._cur]
		cover_path, bak_path = cover_paths(self.library, e.get("path", ""))
		if restore_bak_cover(cover_path, bak_path):
			self._flash(_("restored from .bak"))
		else:
			self._flash(_(".bak does not exist"))
		self._refresh_covers()
		self._reload_list_thumb()

	def cover_keep(self) -> None:
		self._flash(_("kept"))

	def cover_delete_checked(self) -> None:
		e = self.entries[self._cur]
		cover_path, bak_path = cover_paths(self.library, e.get("path", ""))
		paths = []
		if self._del_cover.get():
			paths.append(cover_path)
		if self._del_bak.get():
			paths.append(bak_path)
		# Checked format covers strip the EMBEDDED cover out of the ebook file
		# — the file itself stays (cleaning calibre placeholders, not books).
		fmt_paths = [Path(p) for p, v in self._del_formats.items() if v.get()]
		if not paths and not fmt_paths:
			self._flash(_("nothing checked"))
			return
		# The strip rewrites the ebook in place (sidecar covers are plain
		# deletions, recoverable via .bak/enrichers) — confirm first.
		if fmt_paths and not messagebox.askyesno(
			"bmf gui",
			_("Strip the embedded cover from these ebooks?\n"
			  "(the ebook files themselves stay)\n\n{files}").format(
				files="\n".join(f"  • {p.name}" for p in fmt_paths)),
		):
			return
		n = delete_covers(paths)
		stripped = sum(1 for p in fmt_paths if strip_cover_from_book(p))
		self._del_cover.set(False)
		self._del_bak.set(False)
		for v in self._del_formats.values():
			v.set(False)
		msg = _("deleted {n}").format(n=n) if paths else ""
		if fmt_paths:
			part = _("covers stripped {stripped}/{total}").format(stripped=stripped, total=len(fmt_paths))
			if stripped < len(fmt_paths):
				part += _(" (EPUB only)")
			msg = f"{msg}; {part}" if msg else part
		self._flash(msg)
		self._refresh_covers()
		self._refresh_formats()  # the format radios / content changed too
		self._reload_list_thumb()  # a deleted/replaced cover.jpg must leave the list too

	# ------------------------------------------------------------------
	# Content / formats
	# ------------------------------------------------------------------

	def _refresh_formats(self) -> None:
		e = self.entries[self._cur]
		folder = self.library / e.get("path", "")
		for child in self._format_holder.winfo_children():
			child.destroy()
		# Invalidate any in-flight content stream of the previous book — its
		# chunks must never paint into this book's view.
		self._content_gen += 1
		files = list_format_files(folder)
		self._format_files = files
		if not files:
			ttk.Label(self._format_holder, text=_("(no formats / folder not found)")).pack(side="left")
			self._content_raw = ""
			self._content_repaired = None
			self._recode_var.set(False)
			self._recode_chk.configure(state="disabled")
			self._recode_hint.configure(text="", cursor="")
			self._set_content_text("")
			return
		first = str(files[0])
		for f in files:
			ttk.Radiobutton(
				self._format_holder, text=f.name, value=str(f), variable=self._format_var,
			).pack(side="left", padx=4)
		# Format radios are rebuilt per book, so re-trap them for Tab.
		self._trap_subtree(self._format_holder)
		# The _format_var trace loads the content (a radio click and this
		# programmatic set take the same path — historically the radios had no
		# command at all and a click never reloaded the preview).
		self._format_var.set(first)

	def _on_format_changed(self, *_args) -> None:
		"""A format radio was clicked (or the var set for a new book) — load it."""
		fp = self._format_var.get()
		if fp and fp != getattr(self, "_content_path", None):
			self._content_path = fp
			self._load_content(fp)

	def _load_content(self, file_path: str) -> None:
		"""Load the WHOLE book text, progressively.

		One worker thread per format switch; ``_content_gen`` invalidates stale
		ones — a stale worker STOPS PULLING its stream (the pipe child is
		reaped via the generator's finally) and any chunk that already crossed
		is dropped at the Tk-thread gate, so paging quickly through books
		never interleaves two books' texts. Chunks land as they are extracted
		(stream_full_text yields per EPUB member / pipe chunk), so a slow NFS
		read or a converter render shows its head immediately instead of
		"loading…" until the end. The mojibake detection + recode defaults run
		ONCE on the complete text (per-chunk detection would flicker hints on
		partial evidence).
		"""
		self._content_gen += 1
		gen = self._content_gen
		self._content_path = file_path
		self._content_pieces = []
		self._content_raw = ""
		self._content_repaired = None
		self._recode_var.set(False)
		self._recode_chk.configure(state="disabled")
		self._recode_hint.configure(text="", cursor="")
		self._set_content_text(_("(loading…)"))

		def work():
			delivered = False
			stream = stream_full_text(file_path)
			try:
				for chunk in stream:
					if not self._alive or gen != self._content_gen:
						return  # stale: stop pulling; close() below reaps the pipe
					delivered = True
					self._after(lambda c=chunk: self._content_append(gen, c))
			except Exception:  # noqa: BLE001 — the extract() fallback below decides what shows
				log.debug("content stream failed for %s", file_path, exc_info=True)
			finally:
				# Generators take GeneratorExit here (their finally reaps the
				# pipe child); a duck-typed test double may have no close().
				close = getattr(stream, "close", None)
				if close is not None:
					close()
			if not delivered and self._alive:
				# Nothing streamed (unknown format, unreadable file): fall back
				# to extract() so its error message still reaches the user.
				try:
					meta = extract(file_path)
				except Exception:  # noqa: BLE001
					meta = None
				self._after(lambda: self._content_fallback(gen, meta))
			if self._alive:
				self._after(lambda: self._content_done(gen))

		threading.Thread(target=work, daemon=True).start()

	def _content_append(self, gen: int, chunk: str) -> None:
		"""Tk-thread: append one streamed chunk (stale generations dropped)."""
		if gen != self._content_gen or not self._alive:
			return
		if not self._content_pieces:
			# First chunk: drop the "(loading…)" placeholder.
			self._content_txt.configure(state="normal")
			self._content_txt.delete("1.0", "end")
		self._content_pieces.append(chunk)
		self._content_txt.insert("end", chunk)
		self._content_txt.configure(state="disabled")

	def _content_fallback(self, gen: int, meta) -> None:
		"""Tk-thread: the empty-stream fallback — extract()'s widest window.

		Serves the windows (broader/first-page) a format without a real stream
		produced, or surfaces the extraction error verbatim.
		"""
		if gen != self._content_gen or not self._alive:
			return
		raw = ""
		if meta is not None:
			raw = meta.broader_text or meta.first_page_text or ""
			if not raw and meta.error:
				raw = _("(extraction failed: {err})").format(err=meta.error)
		if raw:
			self._content_append(gen, raw)

	def _content_done(self, gen: int) -> None:
		"""Tk-thread: the stream finished — freeze the raw text, detect mojibake."""
		if gen != self._content_gen or not self._alive:
			return
		raw = "".join(self._content_pieces)
		if not raw:
			raw = _("(no text)")
		self._content_raw = raw
		if len(raw) >= FULL_TEXT_LIMIT:
			self._content_txt.configure(state="normal")
			self._content_txt.insert(
				"end", _("  [… truncated at {limit} characters]").format(limit=f"{FULL_TEXT_LIMIT:,}"))
			self._content_txt.configure(state="disabled")
		# Detect double-encoding (utf-8 mis-decoded twice): default the z/do
		# selectors to the usual CZ suspect. A clean book keeps the user's
		# last pair, so manual experimenting works even when the detector
		# saw nothing. The TOGGLE itself is never auto-checked — showing the
		# repaired text is the user's decision (they tick / press Ctrl+G);
		# auto-ticking it kept flipping the preview as books were paged
		# through, which read as the preview "looping" on its own.
		if detect_double_decode(raw):
			self._recode_hint.configure(text=_("⚠ double encoding detected"))
			self._recode_from.set("cp1250")
			self._recode_to.set("utf-8")
			self._recompute_recode()
		# Two-layer mojibake (wild sample: cp1250 CZ text mis-read as cp1251,
		# re-saved utf-8, mis-read as cp1250, re-saved utf-8): a single z/do
		# pair only reaches the Cyrillic middle layer. repair_chain searches
		# the second layer; its result REPLACES the pair preview (available
		# for when the user ticks the toggle) until the user touches the
		# codecs (the var traces re-take over manually). Again: no
		# auto-ticking — the user opts in to seeing the repaired text.
		chain = repair_chain(raw)
		if chain is not None:
			repaired, desc = chain
			if repaired != (self._content_repaired or ""):
				self._content_repaired = repaired
				self._recode_chk.configure(state="normal")
				self._recode_hint.configure(text=_("⚠ multiple recoding layers ({desc})").format(desc=desc))
		self._apply_content_text()

	def _recompute_recode(self) -> None:
		"""Recompute the transformed text from the current z/do pair.

		The toggle is enabled only when the pair yields an actual change; a
		failing pair is reported in the hint — the user is experimenting, so
		telling them a combination cannot run is the point. A failure usually
		means the direction is INVERTED (utf-8 → cp1250 instead of cp1250 →
		utf-8): cp1250 has 5 undefined byte positions, and common Czech chars
		hit them when wrongly encoded to UTF-8 first (Á → C3 81, ‘ → E2 80 98).
		So when the swapped pair converts, the hint offers it as a click.
		"""
		frm, to = self._recode_from.get(), self._recode_to.get()
		self._content_repaired = recode(self._content_raw, frm, to)
		self._recode_hint.configure(cursor="")
		if self._content_repaired is None:
			self._recode_chk.configure(state="disabled")
			self._recode_var.set(False)
			if self._content_raw:
				reason = recode_failure_reason(self._content_raw, frm, to) or _("unknown reason")
				msg = _("⚠ {frm} → {to} failed: {reason}").format(frm=frm, to=to, reason=reason)
				if frm != to:
					swapped = recode(self._content_raw, to, frm)
					if swapped is not None and swapped != self._content_raw:
						msg += _(" — the reverse ({to} → {frm}) works, click here").format(to=to, frm=frm)
						self._recode_hint.configure(cursor="hand2")
				self._recode_hint.configure(text=msg)
		elif self._content_repaired == self._content_raw:
			self._recode_chk.configure(state="disabled")
			self._recode_var.set(False)
		else:
			self._recode_chk.configure(state="normal")

	def _swap_recode_codecs(self) -> None:
		"""Swap the z/do pair; the StringVar traces re-preview live."""
		frm, to = self._recode_from.get(), self._recode_to.get()
		self._recode_from.set(to)
		self._recode_to.set(frm)

	def _on_recode_hint_click(self, _event=None) -> None:
		# Active only when the hint offers the swapped direction (cursor=hand2).
		if str(self._recode_hint.cget("cursor")) != "hand2":
			return
		self._swap_recode_codecs()

	def _recode_changed(self, *_args) -> None:
		"""A codec was picked (z/do) — live-preview the result from the top.

		NB: no auto-checking here either. This trace fires for PROGRAMMATIC
		pair defaults too (the detector in _content_done sets cp1250/utf-8),
		so an auto-check in this path is exactly the "toggle keeps turning
		itself on while paging books" loop — the checkbox is the user's.
		"""
		if not self._content_raw:
			return
		self._recompute_recode()
		if self._content_repaired is not None and self._content_repaired != self._content_raw:
			self._recode_hint.configure(
				text=_("{frm} → {to} ✓").format(frm=self._recode_from.get(), to=self._recode_to.get()))
		self._apply_content_text()
		try:
			self._content_txt.yview_moveto(0.0)  # re-read from the top
		except Exception:  # noqa: BLE001
			pass

	def _apply_content_text(self) -> None:
		repaired = self._content_repaired if (self._recode_var.get() and self._content_repaired) else None
		self._set_content_text(repaired if repaired is not None else self._content_raw)

	def content_recode_toggle(self) -> None:
		if not self._content_repaired:
			self._flash(_("no double encoding to repair"))
			return
		self._recode_var.set(not self._recode_var.get())
		self._apply_content_text()

	def _set_content_text(self, text: str) -> None:
		self._content_txt.configure(state="normal")
		self._content_txt.delete("1.0", "end")
		self._content_txt.insert("1.0", text)
		self._content_txt.configure(state="disabled")

	# ------------------------------------------------------------------
	# Thumbnail loader (left panel)
	# ------------------------------------------------------------------

	def _start_lib_index(self) -> None:
		"""Build the library fulltext index in ONE background sweep (startup).

		Answers both "why doesn't it start right away" and the per-search
		NFS cost: the sweep begins the moment the window opens (the GUI is
		usable the whole time; progress shows in the status line), and every
		"+ library" search is then an instant in-memory filter — no 40-second
		rescan per query. The same sweep replaces the old separate vocab
		loader: the indexed entries feed the autocomplete pools (better
		quality, too — read_book_folder repairs mojibake before the names
		land in the pool). A query made before the index is ready waits and
		is re-applied automatically when it lands.
		"""
		if self._lib_indexing:
			return
		self._lib_indexing = True

		def work():
			# Serve the sweep from the SQLite cache where possible (a cache-miss
			# sweep re-reads every metadata.json over NFS — minutes on the real
			# library). Best-effort: an unopenable cache degrades to direct reads.
			cache = None
			try:
				cache = Cache(self.cfg.cache_db)
			except Exception:  # noqa: BLE001 - CacheError or worse: index without cache
				cache = None
			try:
				index = build_library_index(self.library, cache=cache, progress=self._lib_index_progress)
			finally:
				if cache is not None:
					try:
						cache.close()
					except Exception:  # noqa: BLE001
						pass
			if self._alive:
				self._after(lambda: self._finish_lib_index(index))

		threading.Thread(target=work, daemon=True).start()

	def _lib_index_progress(self, done: int, total: int) -> None:
		"""Index progress for the status line (worker thread → Tk via _after)."""
		self._after(lambda: self._flash(
			_("indexing library… {done}/{total}").format(done=done, total=total)))

	def _finish_lib_index(self, index: list) -> None:
		"""Store the index, widen the autocomplete pools, answer pending
		queries (Tk thread, via _after)."""
		self._lib_indexing = False
		if not self._alive:
			return
		self._lib_index = index
		self._lib_index_done = True
		for entry, _hay in index:
			c = entry.get("current") or {}
			a = c.get("author")
			if isinstance(a, str) and a.strip():
				self._vocab_authors.add(a.strip())
			for x in c.get("authors") or []:
				if isinstance(x, str) and x.strip():
					self._vocab_authors.add(x.strip())
			s = c.get("series")
			if isinstance(s, str) and s.strip():
				self._vocab_series.add(s.strip())
		self._flash(_("library indexed: {n} books").format(n=len(index)))
		if self._lib_mode.get() and self._search.get().strip():
			self._apply_lib_search()  # a query made while indexing now answers

	def _start_thumb_loader(self) -> None:
		# Re-entrant: a library scan merging new rows wants their thumbnails
		# too. One loader at a time — the live `for` picks up entries
		# appended while it walks, so a second thread adds nothing but races.
		if getattr(self, "_thumb_loading", False):
			return
		self._thumb_loading = True

		def work():
			try:
				for i, e in enumerate(self.entries):
					if not self._alive:
						return
					uuid = e.get("uuid")
					if uuid in self._thumbs_pil:
						continue
					cp, _ = cover_paths(self.library, e.get("path", ""))
					self._thumbs_pil[uuid] = load_thumb(cp, 32, 48) if cp.is_file() else None
					if (i + 1) % 25 == 0 and self._alive:
						self._after(self.refresh_list)
				if self._alive:
					self._after(self.refresh_list)
			finally:
				self._thumb_loading = False

		threading.Thread(target=work, daemon=True).start()

	def _reload_list_thumb(self) -> None:
		"""Reload the CURRENT book's list thumbnail after a cover change.

		The row thumbs are loaded once at startup (_start_thumb_loader) and
		then served from the _thumbs_pil/_thumbs_photo caches forever — a
		deleted/replaced cover.jpg would keep painting the stale image in the
		list (the detail pane refreshes, the list did not). Drop every cache
		entry for the book (small thumb, PhotoImage, hover big thumb) and load
		the fresh one off-thread — the same discipline as the startup loader —
		then refresh_list repaints the row (missing file → no thumbnail).
		"""
		if not (0 <= self._cur < len(self.entries)):
			return
		e = self.entries[self._cur]
		uuid = e.get("uuid")
		cp, _ = cover_paths(self.library, e.get("path", ""))

		def work():
			pil = load_thumb(cp, 32, 48) if cp.is_file() else None
			if self._alive:
				def apply():
					self._thumbs_pil[uuid] = pil
					self._thumbs_photo.pop(uuid, None)
					self._big_thumbs.pop(uuid, None)
					self.refresh_list()
				self._after(apply)

		threading.Thread(target=work, daemon=True).start()

	def _reload_thumbs(self, idxs) -> None:
		"""Reload several books' list thumbnails after a bulk cover change.

		The multi-row twin of :meth:`_reload_list_thumb`: the caches are
		dropped synchronously (an interim refresh_list must not paint the
		stale image), the fresh thumbs load in ONE background sweep (a missing
		cover → no thumbnail) and a single refresh_list repaints the rows
		when they land.
		"""
		jobs = []
		for i in idxs:
			if not (0 <= i < len(self.entries)):
				continue
			e = self.entries[i]
			uuid = e.get("uuid")
			self._thumbs_pil.pop(uuid, None)
			self._thumbs_photo.pop(uuid, None)
			self._big_thumbs.pop(uuid, None)
			cp, _ = cover_paths(self.library, e.get("path", ""))
			jobs.append((uuid, cp))

		def work():
			loaded = [(uuid, load_thumb(cp, 32, 48) if cp.is_file() else None)
			          for uuid, cp in jobs]
			if self._alive:
				def apply():
					for uuid, pil in loaded:
						self._thumbs_pil[uuid] = pil
					self.refresh_list()
				self._after(apply)

		threading.Thread(target=work, daemon=True).start()

	def _thumb_photo_for(self, uuid):
		if uuid in self._thumbs_photo:
			return self._thumbs_photo[uuid]
		pil = self._thumbs_pil.get(uuid)
		if pil is None:
			return None
		try:
			photo = ImageTk.PhotoImage(pil)
		except Exception:  # noqa: BLE001
			return None
		self._thumbs_photo[uuid] = photo
		return photo

	# ------------------------------------------------------------------
	# Misc
	# ------------------------------------------------------------------

	def _after(self, func) -> None:
		"""Schedule *func* on the Tk main thread from a worker.

		Swallows the ``RuntimeError`` Tk raises if the main loop has already
		been teared down (window closing while a cover/content/thumb worker is
		still mid-flight) — a late update has nothing to paint onto and must
		not crash the daemon thread.
		"""
		try:
			self.root.after(0, func)
		except RuntimeError:
			pass

	def _flash(self, msg: str, seconds: float | None = None) -> None:
		"""Show *msg* in the status line; optionally auto-clear after *seconds*."""
		self._set_status(extra=msg)
		if self._flash_after_id is not None:
			try:
				self.root.after_cancel(self._flash_after_id)
			except Exception:  # noqa: BLE001
				pass
			self._flash_after_id = None
		if seconds:
			self._flash_after_id = self.root.after(int(seconds * 1000), self._clear_flash)

	def _clear_flash(self) -> None:
		self._flash_after_id = None
		self._set_status()

	def _set_status(self, extra: str = "") -> None:
		"""Position/counters follow the FILTERED list, not all entries.

		PgUp/PgDn already walk the filtered set (``_step`` uses
		``_filtered_indices``), so the position and the denominator must both
		come from the filter too — otherwise "3/120" while the user looks at a
		17-book category is a lie. When a filter/search narrows the view, the
		unfiltered total is appended in parentheses so it stays visible.
		"""
		idxs = self._filtered_indices()
		total = len(self.entries)
		if 0 <= self._cur < total and self._cur in idxs:
			base = f"{idxs.index(self._cur) + 1}/{len(idxs)}"
		elif 0 <= self._cur < total:
			base = f"–/{len(idxs)}"  # current book filtered out of the view
		else:
			base = _("{n} entries").format(n=len(idxs))
		if len(idxs) != total:
			base += _(" (of {total})").format(total=total)
		dirty = " *" if self._dirty else ""
		base += dirty
		if extra:
			base += f"   —   {extra}"
		self._status.set(base)

	def _help_overlay(self) -> None:
		win = tk.Toplevel(self.root)
		win.title(_("Keyboard shortcuts (F1)"))
		txt = tk.Text(win, width=54, height=30, wrap="word")
		txt.pack(fill="both", expand=True)
		shortcuts = [
			("PgDn / PgUp", _("next / previous book")),
			("Tab / Shift+Tab", _("next / previous edit field (fields only)")),
			("Ctrl+A", _("select all in the field")),
			("Ctrl+Enter", "accept"),
			("Ctrl+W", _("swap the author↔title field values (C1 helper)")),
			("Ctrl/Shift+click", _("list: select multiple books (toggle one / extend a range)")),
			("Ctrl+E", _("bulk edit: set the author or series for all selected books at once")),
			("Ctrl+Shift+A", _("bulk: accept all selected books")),
			("Ctrl+Shift+O", _("bulk: verified on/off for all selected books")),
			("Ctrl+Shift+M", _("bulk: delete covers of all selected books")),
			("Ctrl+Shift+D", _("bulk: clear the decision (→ pending) for all selected books — the mass veto for pre-filled delete/accept")),
			("Ctrl+Shift+R", _("bulk: remove the selected books from review.yaml (no files touched)")),
			("Ctrl+J", _("merge the selected books into one (files move to the survivor)")),
			("∅ / ↺", _("field button: apply the field as EMPTY (wrong proposal, correct value unknown)")),
			("Ctrl+D", "delete"),
			("Ctrl+R", _("merge — fold this folder into the same-work survivor (proposed.merge_into)")),
			("Ctrl+K", _("keep (applies like accept; the entry stays in review)")),
			("Ctrl+O", _("verified — mark the book OK: apply stores the flag in metadata.json, analyze skips the book, apply places it on the target path")),
			("Ctrl+0", _("clear → pending")),
			("+ library", _("checkbox above the list: the search also loads matching books from the whole library, not only review.yaml; a changed book is added to review.yaml on save")),
			("Ctrl+S", _("save")),
			("Ctrl+Q", _("quit")),
			("Ctrl+F", _("RO column → target (focused field)")),
			("Ctrl+N", _("cover: apply new")),
			("Ctrl+B", _("cover: restore .bak")),
			("Ctrl+P", _("cover: keep")),
			("Ctrl+M", _("cover: delete checked cover/.bak, strip embedded covers")),
			("Ctrl+C", _("copy the text selected with the mouse in the Found-problems list / content preview / detail popups")),
			("Click a problem code", _("open the detailed description of that diagnosis (C1…C20, MISSING_*, …)")),
			("Ctrl+G", _("content: recode („read as“ = the wrong read, „actually is“ = the real encoding; result always UTF-8)")),
			("↑ ↓ / Enter", _("author & series: autocomplete from the library (arrows pick, Enter inserts; Tab inserts only after an arrow pick — otherwise it leaves your text)")),
			("", _("(click on a cover = ☑; click on path / double-click in the list = open folder)")),
			("", _("(wheel: widget under the mouse, at its edge the form; cover in the list → hover popup)")),
		]
		for k, d in shortcuts:
			txt.insert("end", f"{k:<18} {d}\n")
		txt.configure(state="disabled")
