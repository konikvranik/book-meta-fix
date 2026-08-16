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
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from .covers import (
	analyze_cover,
	download_cover,
	epub_cover_image,
	extract_cover_from_book,
	strip_cover_from_book,
)
from .encoding import detect_double_decode, recode, recode_failure_reason, repair_chain
from .extractors import extract
from .i18n import _
from .library import iter_book_folders
from .readers import EBOOK_EXTS
from .review import _header, _load_raw_entries, _render_entry

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
	tk = None  # type: ignore[assignment]


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
	cover/content work; unreadable folders are skipped. Series names are
	pulled from the ABS ``[{"name", ...}]`` shape (plain strings tolerated).
	"""
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
		if isinstance(s, str):
			s = [s]
		for item in s:
			name = item.get("name") if isinstance(item, dict) else item
			if isinstance(name, str) and name.strip():
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
	cover yields None, which the UI renders as a placeholder.
	"""
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
		tk.Label(tip, text=self.text, justify="left", background="#ffffe0",
		         relief="solid", borderwidth=1, padx=6, pady=3).pack()
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
	widget bookkeeping). Up/Down move the popup selection, Return/Tab accept,
	Escape closes. The value pool is polled via ``values()`` on every
	keystroke, so it can back onto a mutable app-level set that grows as the
	user types in new names.
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

	def _on_key(self, event) -> None:
		if (event.keysym or "") in self._NAV_KEYS or event.state & 0x4:  # Ctrl held
			return
		self._refresh()

	def _refresh(self) -> None:
		text = self.entry.get().strip().lower()
		if not text:
			self.hide()
			return
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
	selection_set/focus/see/exists/identify_row/bind/yview/configure), so
	the rest of the GUI keeps talking to ``self.tree`` unchanged. Only the
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
	}
	AUTHOR_INDENT = 12

	def __init__(self, parent, style) -> None:
		self._rows: list[dict] = []
		self._by_iid: dict[str, dict] = {}
		self._selected: str | None = None
		self._select_cb = None
		self._draw_pending = False
		self._pending_top: float | None = None
		self._font, self._bold, self._italic = self._resolve_fonts(style)
		self._bg, self._fg, self._sel_bg, self._sel_fg, self._border = self._resolve_colors(style)
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
	def _resolve_colors(style):
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
		return bg, fg, sel_bg, sel_fg, "#999999"

	# -- Treeview-like API --------------------------------------------------

	def insert(self, _parent, _index, *, iid=None, text="", values=(), image=None):
		# Two-line row: *text* is the TITLE (bold, first line); values are
		# (action, author) — the author renders italic + indented below.
		row = {
			"iid": str(iid), "title": text,
			"action": values[0] if values else "",
			"author": values[1] if len(values) > 1 else "",
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
		self._update_scrollregion()
		self._schedule_draw()

	def get_children(self, *_parent) -> tuple:
		return tuple(r["iid"] for r in self._rows)

	def exists(self, iid) -> bool:
		return iid in self._by_iid

	def selection_set(self, iid, *, silent: bool = False) -> None:
		if iid not in self._by_iid:
			return
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
			if sel:
				c.create_rectangle(0, y, w, y + self.ROW_H,
				                   fill=self._sel_bg, outline="")
			fg = self._sel_fg if sel else self._fg
			# Line 1: title, bold, from the left edge. Line 2: author,
			# italic, indented — the thumbnail already fixes the row height,
			# so two lines fit at no cost.
			c.create_text(self.PAD, y + 6, anchor="nw",
			              text=self._elide(row["title"], text_w, self._bold),
			              font=self._bold, fill=fg)
			c.create_text(self.PAD + self.AUTHOR_INDENT, y + self.ROW_H - 6,
			              anchor="sw",
			              text=self._elide(row["author"] or "—",
			                               text_w - self.AUTHOR_INDENT, self._italic),
			              font=self._italic, fill=fg)
			# Action: coloured glyph (colour stays even when selected — it
			# is the orientation cue, and the Tango colours read fine on
			# the selection blue).
			glyph, colour = self.ACTION_GLYPHS.get(row["action"],
			                                      self.ACTION_GLYPHS[""])
			c.create_text(ax, cy, anchor="w", text=glyph,
			              font=self._bold, fill=colour)
			if row["image"] is not None:
				c.create_image(thumb_x, y + (self.ROW_H - self.THUMB_H) // 2,
				               anchor="nw", image=row["image"])
			if i + 1 < len(self._rows):
				c.create_line(0, y + self.ROW_H, w, y + self.ROW_H,
				              fill=self._border if not sel else self._sel_bg)


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
	ACTIONS = ["pending", "accept", "delete", "keep"]

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
		# review entries themselves (instant), then widened off-thread by a
		# full-library scan (see _start_vocab_loader). New names the user
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
				items = [s] if isinstance(s, str) else (s if isinstance(s, list) else [])
				for item in items:
					name = item.get("name") if isinstance(item, dict) else item
					if isinstance(name, str) and name.strip():
						self._vocab_series.add(name.strip())

		# Filter / search state.
		self._filter_action = tk.StringVar(value="all")
		self._filter_category = tk.StringVar(value="all")
		self._search = tk.StringVar()
		self._search.trace_add("write", lambda *_: self.refresh_list())

		# Action / notes state.
		self._action_var = tk.StringVar(value="pending")
		self._notes_var = tk.StringVar()
		self._action_var.trace_add("write", lambda *_: self._on_action_changed())
		self._notes_var.trace_add("write", lambda *_: self._mark_dirty())

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

		# Content state.
		self._format_var = tk.StringVar()
		self._view_var = tk.StringVar(value="first")
		self._content_cache: dict[str, object] = {}  # file path -> ExtractedMeta
		self._format_files: list = []
		self._recode_var = tk.BooleanVar(value=False)
		self._recode_from = tk.StringVar(value="cp1250")
		self._recode_to = tk.StringVar(value="utf-8")
		self._content_raw = ""
		self._content_repaired: str | None = None

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
		self._start_vocab_loader()

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
		try:
			self.root.configure(
				background=self._style.lookup("TFrame", "background") or "#d9d9d9"
			)
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
			values=["all", "pending", "accept", "delete", "keep"],
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

		parent.add(frame, weight=1)

	def _build_right_panel(self, parent) -> None:
		frame = ttk.Frame(parent)
		# Scrollable detail column: Canvas + inner frame + scrollbar.
		self.canvas = tk.Canvas(frame, highlightthickness=0)
		try:
			self.canvas.configure(
				background=self._style.lookup("TFrame", "background") or "#d9d9d9"
			)
		except Exception:  # noqa: BLE001
			pass
		vsb = ttk.Scrollbar(frame, orient="vertical", command=self.canvas.yview)
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
		self._build_fields_section()
		self._build_covers_section()
		self._build_content_section()
		parent.add(frame, weight=3)

	def _on_canvas_configure(self, event) -> None:
		try:
			self.canvas.itemconfigure(self._inner_win, width=event.width)
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
		except Exception:  # noqa: BLE001
			pass

	def _build_fields_section(self) -> None:
		box = ttk.LabelFrame(self._scroll_inner, text=_("Fields"))
		box.pack(fill="x", padx=8, pady=4)
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

		# Action radios + notes + nav.
		bottom = ttk.Frame(box)
		bottom.pack(fill="x", padx=6, pady=6)
		ttk.Label(bottom, text=_("Action:")).grid(row=0, column=0, sticky="w")
		rad = ttk.Frame(bottom)
		rad.grid(row=0, column=1, columnspan=6, sticky="w")
		for i, a in enumerate(self.ACTIONS):
			ttk.Radiobutton(rad, text=a, value=a, variable=self._action_var).grid(row=0, column=i, padx=2, sticky="w")
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

	def _build_content_section(self) -> None:
		box = ttk.LabelFrame(self._scroll_inner, text=_("Content"))
		box.pack(fill="both", expand=True, padx=8, pady=4)
		top = ttk.Frame(box)
		top.pack(fill="x", padx=6, pady=4)
		ttk.Label(top, text=_("Format:")).pack(side="left")
		self._format_holder = ttk.Frame(top)
		self._format_holder.pack(side="left", fill="x", expand=True, padx=6)
		view = ttk.Frame(box)
		view.pack(fill="x", padx=6)
		ttk.Label(view, text=_("View:")).pack(side="left")
		ttk.Radiobutton(view, text=_("first page"), value="first", variable=self._view_var).pack(side="left", padx=4)
		ttk.Radiobutton(view, text=_("broader text"), value="broader", variable=self._view_var).pack(side="left", padx=4)
		self._view_var.trace_add("write", lambda *_: self._apply_content())
		rec = ttk.Frame(box)
		rec.pack(fill="x", padx=6, pady=(2, 4))
		self._recode_chk = ttk.Checkbutton(
			rec, text=_("↻ Recode (Ctrl+G)"), variable=self._recode_var,
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
			"Double-encoding repair: “read as” = the codec the text was originally "
			"mis-read through (typically cp1250); “actually is” = the real encoding "
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
		body = ttk.Frame(box)
		body.pack(fill="both", expand=True, padx=6, pady=0)
		self._content_body = body
		self._content_txt = self._style_text(
			tk.Text(body, wrap="word", state="disabled", height=12)
		)
		self._content_txt.pack(side="left", fill="both", expand=True)
		vsb = ttk.Scrollbar(body, orient="vertical", command=self._content_txt.yview)
		self._content_txt.configure(yscrollcommand=vsb.set)
		vsb.pack(side="right", fill="y")
		# Pixel-continuous height: a Text's own height option is quantized to
		# whole LINES by Tk (that was the choppy one-line-at-a-time drag), so
		# propagation is turned off and body's -height — pixels — rules; the
		# Text and scrollbar just fill it. Horizontal sizing is unaffected
		# (body is packed fill="both").
		body.pack_propagate(False)
		try:
			ls = max(1, int(self.root.tk.call(
				"font", "metrics", self._content_txt.cget("font"), "-linespace",
			)))
		except Exception:  # noqa: BLE001
			ls = 16  # typical 10pt line; resize still works
		self._content_linespace = ls
		self._preview_h_default = 12 * ls + 8  # ~the old 12-line height
		body.configure(height=self._preview_h_default)
		# Drag-to-resize grip flush with the preview's bottom edge. Inside the
		# scrollable column the Text's height IN LINES is the only geometry
		# knob that matters (the inner frame grows, _on_inner_configure
		# re-derives the scrollregion), so a plain drag handle suffices — no
		# PanedWindow. Highlight ring instead of a fill so the bar reads as an
		# edge, both on light and dark themes.
		try:
			grip_bg = self._style.lookup("TFrame", "background") or "#d9d9d9"
		except Exception:  # noqa: BLE001
			grip_bg = "#d9d9d9"
		self._content_grip = tk.Frame(
			box, height=7, cursor="sb_v_double_arrow", background=grip_bg,
			highlightthickness=1, highlightbackground="#999999",
		)
		self._content_grip.pack(fill="x", padx=6, pady=(2, 6))
		self._content_grip.bind("<Button-1>", self._start_preview_resize)
		self._content_grip.bind("<B1-Motion>", self._drag_preview_resize)
		self._content_grip.bind(
			"<Double-Button-1>", lambda _e: self._set_preview_height(self._preview_h_default),
		)
		self._grip_tip = _Tooltip(
			self._content_grip,
			_("Drag up/down to change the preview height (double-click = default height)"),
		)

	def _set_preview_height(self, px: int) -> None:
		"""Set the content preview height in pixels, clamped to sane bounds.

		The bounds are expressed in lines (3–80) so they track the actual
		font, but the value itself is raw pixels — no line quantization.
		Deliberately does NOT touch the canvas scrollregion here: this runs
		inside motion events, where ``bbox("all")`` still reflects the OLD
		geometry (layout happens at idle), so setting it synchronously means
		TWO clashing updates per mouse move — the visible jerk. The inner
		frame's <Configure> binding (_on_inner_configure) recomputes it once,
		after the real layout lands.
		"""
		ls = getattr(self, "_content_linespace", 16)
		px = max(3 * ls, min(80 * ls, int(px)))
		self._content_body.configure(height=px)

	def _start_preview_resize(self, event) -> str | None:
		# Defaults via getattr: B1-Motion cannot arrive without a Button-1 on
		# the same widget (implicit pointer grab), these are pure paranoia.
		self._resize_y0 = event.y_root
		# Snap the request to the CURRENT allocation: when the window is
		# large enough that the parcel exceeds the request, a drag would feel
		# dead until the request passes the allocation (and then jump).
		# Snapping is visually a no-op and makes every pixel 1:1 from here on.
		self._resize_h0 = self._content_body.winfo_height()
		self._set_preview_height(self._resize_h0)
		return "break"

	def _drag_preview_resize(self, event) -> str | None:
		dy = event.y_root - getattr(self, "_resize_y0", event.y_root)
		h0 = getattr(self, "_resize_h0", self._content_body.winfo_height())
		# Absolute (not incremental) mapping from the press anchor: rounding
		# cannot accumulate jitter across motion events — and in pixels there
		# is nothing left to quantize.
		self._set_preview_height(h0 + dy)
		return "break"

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
		while node is not None and node is not self.root:
			if node is self.canvas:
				self._scroll_canvas(up)
				return "break"
			# The book list scrolls itself (its own wheel binding) — the
			# form stays put. NB: _BookList is a COMPOSITION, the actual
			# event widget is its .canvas.
			if node is self.tree or node is self.tree.canvas:
				return "break"
			if node.winfo_class() == "Text":
				if self._can_y_scroll(node, up):
					return "break"  # the Text class binding already scrolled it
				# stuck at the Text's edge -> the form takes over
				self._scroll_canvas(up)
				return "break"
			node = node.master
		return None

	def _scroll_canvas(self, up: bool) -> None:
		"""Scroll the form column — a hard no-op while the form fits.

		The form must sit FIXED at the top when its content fits the viewport;
		it only starts scrolling once it actually overflows the canvas.
		"""
		try:
			bbox = self.canvas.bbox("all")
			if not bbox:
				return
			if (bbox[3] - bbox[1]) - self.canvas.winfo_height() <= 0:
				return  # fits -> fixed
			self.canvas.yview_scroll(-1 if up else 1, "units")
		except Exception:  # noqa: BLE001
			pass

	def _on_tab(self, event) -> str | None:
		# Only the field entries are trapped; from any other widget (notes,
		# buttons, the list) Tab falls through to Tk's default traversal.
		w = self.focus_get_safe()
		if w not in self._editable_widgets:
			return None
		# Tab with an autocomplete dropdown open first accepts the highlighted
		# suggestion, THEN moves on — the usual "pick and continue" flow.
		ac = self._acs.get(w)
		if ac is not None and ac.is_open:
			ac.accept()
		# Direction: the Shift modifier (Windows/macOS <Shift-Tab>) or the X11
		# keysym itself (ISO_Left_Tab arrives as its own keysym).
		shift = bool(event.state & 0x1) or (event.keysym or "") == "ISO_Left_Tab"
		self._cycle_editable(forward=not shift)
		return "break"

	def _on_ctrl_key(self, event) -> str | None:
		k = (event.keysym or "").lower()
		if k in _PASSTHROUGH:
			return None  # keep native copy/paste/cut/undo/select-all
		dispatch = {
			"return": self.act_accept, "d": self.act_delete, "k": self.act_keep,
			"0": self.act_clear, "s": self.save, "q": self.quit_app,
			"w": self.swap_fields, "f": self.copy_current_to_focused,
			"n": self.cover_new,
			"b": self.cover_restore_bak, "p": self.cover_keep, "m": self.cover_delete_checked,
			"t": self.content_toggle_view, "g": self.content_recode_toggle,
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
		self.tree.delete(*self.tree.get_children())
		idxs = self._filtered_indices()
		for i in idxs:
			e = self.entries[i]
			uuid = e.get("uuid")
			# NB: ``image`` MUST be omitted (not passed as None) — passing
			# image=None corrupts ttk's option parsing and the next option's
			# value (here the ``values`` list) is misread as an option name.
			kw = dict(iid=str(i), text=self._entry_title(e),
			          values=(self._action_label(e), self._entry_author(e)))
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
		# content loaders on every background refresh.
		if sel_iid and self.tree.exists(sel_iid):
			self.tree.selection_set(sel_iid, silent=True)
		self._set_status()

	def _filtered_indices(self) -> list[int]:
		act = self._filter_action.get()
		cat = self._filter_category.get()
		needle = self._search.get().strip().lower()
		out = []
		for i, e in enumerate(self.entries):
			ea = e.get("action")
			if act != "all" and (ea or "pending") != act:
				continue
			ec = (e.get("diagnosis") or {}).get("category")
			if cat != "all" and ec != cat:
				continue
			if needle:
				cur = e.get("current") or {}
				hay = " ".join(str(x) for x in [e.get("path"), cur.get("author"), cur.get("title"), ea, ec] if x).lower()
				if needle not in hay:
					continue
			out.append(i)
		return out

	@staticmethod
	def _entry_title(e: dict) -> str:
		cur = e.get("current") or {}
		return cur.get("title") or "—"

	@staticmethod
	def _entry_author(e: dict) -> str:
		cur = e.get("current") or {}
		return cur.get("author") or ""

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
		tk.Label(popup, image=photo, borderwidth=2, relief="solid").pack()
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
			diag = e.get("diagnosis") or {}
			uuid = e.get("uuid") or "—"
			path = e.get("path") or ""
			all_d = e.get("diagnoses") or [diag]
			extra = _("  (+{n} more)").format(n=len(all_d) - 1) if len(all_d) > 1 else ""
			self._header_lbl.configure(
				text=_("Entry {i}/{n}   uuid: {uuid}\n"
				       "diagnosis: {cat} – {reason} [{conf}]{extra}").format(
					i=idx + 1, n=len(self.entries), uuid=uuid,
					cat=diag.get("category", "—"), reason=diag.get("reason", ""),
					conf=diag.get("confidence", "—"), extra=extra),
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
			# Action / notes.
			self._action_var.set(e.get("action") or "pending")
			self._notes_var.set(e.get("notes") or "")
		finally:
			self._loading = False
		# Covers + content for the new book.
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
		text = render_review_text(self.entries)
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
		self._content_cache.clear()
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
		self._format_var.set(first)
		self._load_content(first)

	def _load_content(self, file_path: str) -> None:
		self._set_content_text(_("(loading…)"))

		def work():
			meta = self._content_cache.get(file_path)
			if meta is None:
				meta = extract(file_path)
				self._content_cache[file_path] = meta
			if self._alive:
				self._after(self._apply_content)

		threading.Thread(target=work, daemon=True).start()

	def _apply_content(self) -> None:
		fp = self._format_var.get()
		meta = self._content_cache.get(fp)
		if meta is None:
			self._content_raw = ""
			self._content_repaired = None
			self._recode_chk.configure(state="disabled")
			self._recode_hint.configure(text="", cursor="")
			self._set_content_text("")
			return
		view = self._view_var.get()
		raw = (meta.broader_text if view == "broader" else meta.first_page_text) or ""
		if not raw and meta.error:
			raw = _("(extraction failed: {err})").format(err=meta.error)
		elif not raw:
			raw = _("(no text)")
		self._content_raw = raw
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
		"""A codec was picked (z/do) — live-preview the result from page one.

		NB: no auto-checking here either. This trace fires for PROGRAMMATIC
		pair defaults too (the detector in _apply_content sets cp1250/utf-8),
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
			self._content_txt.yview_moveto(0.0)  # first-page preview
		except Exception:  # noqa: BLE001
			pass

	def _apply_content_text(self) -> None:
		repaired = self._content_repaired if (self._recode_var.get() and self._content_repaired) else None
		self._set_content_text(repaired if repaired is not None else self._content_raw)

	def content_toggle_view(self) -> None:
		self._view_var.set("broader" if self._view_var.get() == "first" else "first")
		self._apply_content()

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

	def _start_vocab_loader(self) -> None:
		"""Widen the autocomplete pools with a full-library scan (off-thread).

		~5k tiny metadata.json reads — a second or two on disk cache, but far
		too slow to run on the Tk main loop before the window becomes usable.
		"""
		def work():
			authors, series = collect_vocab_values(self.library)
			if self._alive:
				def apply():
					self._vocab_authors.update(authors)
					self._vocab_series.update(series)
				self._after(apply)

		threading.Thread(target=work, daemon=True).start()

	def _start_thumb_loader(self) -> None:
		def work():
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
			("∅ / ↺", _("field button: apply the field as EMPTY (wrong proposal, correct value unknown)")),
			("Ctrl+D", "delete"),
			("Ctrl+K", _("keep (applies and retains; analyze skips it)")),
			("Ctrl+0", _("clear → pending")),
			("Ctrl+S", _("save")),
			("Ctrl+Q", _("quit")),
			("Ctrl+F", _("focus search")),
			("Ctrl+L", _("RO column → target (focused field)")),
			("Ctrl+N", _("cover: apply new")),
			("Ctrl+B", _("cover: restore .bak")),
			("Ctrl+P", _("cover: keep")),
			("Ctrl+M", _("cover: delete checked cover/.bak, strip embedded covers")),
			("Ctrl+T", _("content: first page / broader text")),
			("Ctrl+G", _("content: recode (“read as” = the wrong read, “actually is” = the real encoding; result always UTF-8)")),
			("↑ ↓ / Enter / Tab", _("author & series: autocomplete from the library (arrows pick, Enter/Tab insert)")),
			("", _("(click on a cover = ☑; click on path / double-click in the list = open folder)")),
			("", _("(wheel: widget under the mouse, at its edge the form; cover in the list → hover popup)")),
		]
		for k, d in shortcuts:
			txt.insert("end", f"{k:<18} {d}\n")
		txt.configure(state="disabled")
