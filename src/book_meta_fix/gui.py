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
(:func:`review._header` + :func:`review._render_entry`). Mutations touch only
``action`` / ``edited`` / ``notes``; every other key is preserved verbatim, so
the round-trip is byte-compatible with ``analyze`` output. Cover and content
operations are immediate, reversible file reads (``.bak`` backed); the actual
metadata write still happens via ``bmf apply``.

Tkinter is optional: the top-level import is guarded so that importing this
module (e.g. in tests, which exercise only the pure helpers below) does not
require ``python3-tk``. Only :func:`run_gui` needs a working Tk.
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import threading
from pathlib import Path

from .covers import analyze_cover, download_cover, extract_cover_from_book
from .encoding import detect_double_decode, recode
from .extractors import extract
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
# list-valued (stored comma-separated in the Entry, split on save).
FIELD_SPECS: list[tuple[str, str]] = [
	("author", "Autor"),
	("title", "Titul"),
	("isbn", "ISBN"),
	("year", "Rok"),
	("publisher", "Vydavatel"),
	("language", "Jazyk"),
	("authors", "Autoři (odděleni čárkou)"),
	("genres", "Žánry (odděleny čárkou)"),
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


def compose_edited(selection: dict[str, tuple[bool, str]]) -> dict | None:
	"""Build the ``edited`` dict from per-field ``(include, value)`` selections.

	List fields are split on commas; ``year`` is coerced to int when numeric.
	Returns ``None`` when nothing is included (so the key is omitted on save).
	"""
	edited: dict = {}
	for field, (include, value) in selection.items():
		if not include:
			continue
		if field in LIST_FIELDS:
			edited[field] = [p.strip() for p in str(value).split(",") if p.strip()]
		elif field == "year":
			v = str(value).strip()
			edited[field] = int(v) if v.isdigit() else (v or None)
		else:
			edited[field] = str(value)
	return edited or None


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


def embedded_cover_thumb(book_path: Path | str, max_w: int = 240, max_h: int = 320):
	"""Preview thumbnail of the cover EMBEDDED in an ebook file (any format).

	Extracts via :func:`covers.extract_cover_from_book` (calibre ebook-meta)
	into a temp file, thumbnails it, and removes the temp — nothing lands in
	the library. Unlike :func:`covers.recover_cover_from_book` this does NOT
	gate on generated placeholders: the point of the GUI preview is to SEE a
	calibre-written placeholder, so the file can be flagged for deletion.
	Returns a PIL image or None (calibre absent / no embedded cover / corrupt).
	"""
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

	def _schedule(self, _event=None) -> None:
		self._cancel()
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
	ACTIONS = ["pending", "accept", "reject", "swap", "edit", "delete", "keep"]

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
			messagebox.showerror("bmf gui", f"review file not found:\n{self.review_path}")
			self.root.destroy()
			return
		try:
			self.entries = _load_raw_entries(self.review_path)
		except Exception as e:  # noqa: BLE001
			messagebox.showerror("bmf gui", f"failed to parse {self.review_path}:\n{e}")
			self.root.destroy()
			return

		# Filter / search state.
		self._filter_action = tk.StringVar(value="all")
		self._filter_category = tk.StringVar(value="all")
		self._search = tk.StringVar()
		self._search.trace_add("write", lambda *_: self.refresh_list())

		# Action / notes state.
		self._action_var = tk.StringVar(value="pending")
		self._notes_var = tk.StringVar()
		self._action_var.trace_add("write", lambda *_: self._mark_dirty())
		self._notes_var.trace_add("write", lambda *_: self._mark_dirty())

		# Per-field widgets (checkbutton / RO label / copy btn / target Entry).
		self._fields: dict[str, dict] = {}
		self._field_entries: list = []  # target Entries in Tab-traversal order
		self._editable_widgets: list = []  # field entries + notes (Tab cycle)

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

		# Load the first book and focus its first field (start focus rule).
		if self.entries:
			self._select_index(0, keep_focus=False)
			self.root.after(50, self._focus_first_field)

	# ------------------------------------------------------------------
	# UI construction
	# ------------------------------------------------------------------

	def _setup_style(self) -> None:
		"""Pick up the platform ttk theme and sync tk.Text palette to it.

		Why: Tk's ``tk.Text`` widgets default to a hard white background that
		looks like a bright island under a dark desktop theme. Reading the
		ttk ``TEntry`` field colors and applying them to the Text widgets keeps
		the whole window visually consistent. True automatic light/dark
		switching (reading GNOME's color-scheme) is out of scope without an
		optional extra; this gets the consistency right.
		"""
		self._style = ttk.Style()
		try:
			# Uniform row height so list rows align with or without a cover
			# thumbnail (the user's "texty zarovnány pod sebou" request).
			self._style.configure("Treeview", rowheight=54)
		except Exception:  # noqa: BLE001
			pass
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
		frame = ttk.LabelFrame(parent, text="Seznam")
		# Filters row.
		filt = ttk.Frame(frame)
		filt.pack(fill="x", padx=6, pady=4)
		ttk.Label(filt, text="Akce:").pack(side="left")
		self._action_combo = ttk.Combobox(
			filt, textvariable=self._filter_action, state="readonly", width=9,
			values=["all", "pending", "accept", "reject", "swap", "edit", "delete", "keep"],
		)
		self._action_combo.pack(side="left", padx=(2, 8))
		self._action_combo.bind("<<ComboboxSelected>>", lambda *_: self.refresh_list())
		ttk.Label(filt, text="Kat:").pack(side="left")
		self._cat_combo = ttk.Combobox(
			filt, textvariable=self._filter_category, state="readonly", width=10,
		)
		self._cat_combo.pack(side="left", padx=(2, 8))
		self._cat_combo.bind("<<ComboboxSelected>>", lambda *_: self.refresh_list())
		ttk.Label(filt, text="Hledat:").pack(side="left")
		self._search_entry = ttk.Entry(filt, textvariable=self._search, width=22)
		self._search_entry.pack(side="left", fill="x", expand=True)
		self._bind_select_all(self._search_entry)

		# Tree with per-row cover thumbnail + author/title + action.
		tree_frame = ttk.Frame(frame)
		tree_frame.pack(fill="both", expand=True, padx=6, pady=(0, 4))
		self.tree = ttk.Treeview(
			tree_frame, columns=("action",), show="tree", selectmode="browse",
		)
		self.tree.heading("#0", text="Autor – Titul")
		self.tree.heading("action", text="Akce")
		self.tree.column("#0", width=300)
		self.tree.column("action", width=70, anchor="w")
		self.tree.pack(side="left", fill="both", expand=True)
		vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
		self.tree.configure(yscrollcommand=vsb.set)
		vsb.pack(side="right", fill="y")
		self.tree.bind("<<TreeviewSelect>>", lambda *_: self._on_tree_select())
		# Hover popup: show a larger cover near the cursor when the pointer
		# lingers on a row that has a thumbnail.
		self.tree.bind("<Motion>", self._on_tree_motion, add="+")
		self.tree.bind("<Leave>", self._on_tree_leave, add="+")

		parent.add(frame, weight=1)

	def _build_right_panel(self, parent) -> None:
		frame = ttk.Frame(parent)
		# Scrollable detail column: Canvas + inner frame + scrollbar.
		self.canvas = tk.Canvas(frame, highlightthickness=0)
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
		box = ttk.LabelFrame(self._scroll_inner, text="Pole")
		box.pack(fill="x", padx=8, pady=4)
		fields = ttk.Frame(box)
		fields.pack(fill="x", padx=6, pady=6)
		fields.columnconfigure(3, weight=1)
		for row, (role, label) in enumerate(FIELD_SPECS):
			incl = tk.BooleanVar(value=False)
			current = tk.StringVar(value="")
			value = tk.StringVar(value="")

			def _on_focus(_e, r=role):
				self._last_field_role = r

			chk = ttk.Checkbutton(fields, text=label, variable=incl)
			lbl = ttk.Label(fields, textvariable=current, relief="sunken", anchor="w", width=32)
			# Arrow points RO -> edit (copy the current value into the target).
			copy_btn = ttk.Button(fields, text="➡", width=2, command=lambda r=role: self._copy_current(r))
			entry = ttk.Entry(fields, textvariable=value)
			entry.bind("<FocusIn>", _on_focus)
			self._bind_select_all(entry)
			chk.grid(row=row, column=0, padx=(0, 4), pady=1, sticky="w")
			lbl.grid(row=row, column=1, padx=2, pady=1, sticky="we")
			copy_btn.grid(row=row, column=2, padx=2, pady=1)
			entry.grid(row=row, column=3, padx=(2, 0), pady=1, sticky="we")
			if role == "title":
				# Compact swap icon (full label lives in the tooltip so it
				# cannot overlap the title row, the prior bug).
				swap_btn = ttk.Button(fields, text="⇄", width=3, command=self.swap_fields)
				swap_btn.grid(row=row, column=4, padx=(4, 0), pady=1, sticky="w")
				_Tooltip(swap_btn, "Prohodit autora a titul  (Ctrl+W)")
			self._fields[role] = {"incl": incl, "current": current, "value": value, "entry": entry}
			self._field_entries.append(entry)
			# Trace value/include → dirty (but not during programmatic load).
			value.trace_add("write", lambda *_: self._mark_dirty())
			incl.trace_add("write", lambda *_: self._mark_dirty())
		box.columnconfigure(0, weight=1)

		# Read-only proposed block.
		ttk.Label(box, text="Doporučený návrh (pro accept/keep):").pack(anchor="w", padx=6)
		self._proposed_txt = self._style_text(tk.Text(box, height=7, wrap="word", state="disabled"))
		self._proposed_txt.pack(fill="x", padx=6, pady=2)

		# Action radios + notes + nav.
		bottom = ttk.Frame(box)
		bottom.pack(fill="x", padx=6, pady=6)
		ttk.Label(bottom, text="Akce:").grid(row=0, column=0, sticky="w")
		rad = ttk.Frame(bottom)
		rad.grid(row=0, column=1, columnspan=6, sticky="w")
		for i, a in enumerate(self.ACTIONS):
			ttk.Radiobutton(rad, text=a, value=a, variable=self._action_var).grid(row=0, column=i, padx=2, sticky="w")
		ttk.Label(bottom, text="Poznámka:").grid(row=1, column=0, sticky="w", pady=(4, 0))
		self._notes_entry = ttk.Entry(bottom, textvariable=self._notes_var)
		self._notes_entry.grid(row=1, column=1, columnspan=6, sticky="we", pady=(4, 0))
		self._bind_select_all(self._notes_entry)
		bottom.columnconfigure(1, weight=1)
		nav = ttk.Frame(box)
		nav.pack(fill="x", padx=6, pady=(2, 8))
		ttk.Button(nav, text="◀ Předchozí (PgUp)", command=self.prev_book).pack(side="left")
		ttk.Button(nav, text="Uložit (Ctrl+S)", command=self.save).pack(side="left", padx=20)
		ttk.Button(nav, text="Další (PgDn) ▶", command=self.next_book).pack(side="right")

		# Tab cycle = the target fields + the notes entry (nothing else).
		self._editable_widgets = list(self._field_entries) + [self._notes_entry]

	def _cover_cell(self, parent, title: str, var=None, tip: str | None = None):
		"""One fixed-size cover cell: a slot box + caption; ``(label, caption)``.

		The slot is a fixed-size ``tk.Frame`` with ``pack_propagate(False)``
		so a missing or undersized cover renders as an identically-sized box
		— the previews stay aligned side by side regardless of image size or
		absence. The selection checkbox (when *var* is given) overlays the
		slot's top-left corner.
		"""
		cell = ttk.Frame(parent)
		cell.pack(side="left", padx=6)
		slot = tk.Frame(
			cell, width=self.COVER_SLOT_W, height=self.COVER_SLOT_H,
			relief="sunken", borderwidth=1, background=self._field_bg,
		)
		slot.pack_propagate(False)
		slot.pack()
		lbl = ttk.Label(slot, text="(načítám…)", anchor="center")
		lbl.pack(fill="both", expand=True)
		if var is not None:
			chk = ttk.Checkbutton(lbl, variable=var)
			chk.place(x=4, y=4, anchor="nw")
			if tip:
				_Tooltip(chk, tip)
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
		box = ttk.LabelFrame(self._scroll_inner, text="Obálky")
		box.pack(fill="x", padx=8, pady=4)
		# Selection checkboxes sit directly ON each cover (top-left overlay,
		# the customary selection spot); "Smazat označené" then removes what
		# is checked. The recommended cover is a URL preview, not a file, so
		# it gets no checkbox.
		self._cover_row = ttk.Frame(box)
		self._cover_row.pack(fill="x", padx=6, pady=8)
		self._cover_imgs = []
		self._cover_caps = []
		for title, var, tip in (
			("Aktuální", self._del_cover, "Smazat cover.jpg (při Smazat označené)"),
			(".bak záloha", self._del_bak, "Smazat cover.jpg.bak (při Smazat označené)"),
			("Doporučená", None, None),
		):
			lbl, cap = self._cover_cell(self._cover_row, title, var, tip)
			self._cover_imgs.append(lbl)
			self._cover_caps.append(cap)
		# Embedded covers of the book's format files (calibre extraction) —
		# rebuilt per book in _apply_fmt_covers. Their checkbox deletes the
		# format FILE itself (cleaning out invalid calibre-titled copies).
		ttk.Label(box, text="Vložené obálky ve formátech (☐ = smazat soubor):").pack(anchor="w", padx=6)
		self._fmt_cover_row = ttk.Frame(box)
		self._fmt_cover_row.pack(fill="x", padx=6, pady=(2, 8))
		# Keep the slot widths equal and fitted on every pane resize.
		self._cover_row.bind("<Configure>", self._sync_cover_slots, add="+")
		self._fmt_cover_row.bind("<Configure>", self._sync_cover_slots, add="+")
		btns = ttk.Frame(box)
		btns.pack(fill="x", padx=6, pady=4)
		ttk.Button(btns, text="Ponechat (Ctrl+P)", command=self.cover_keep).pack(side="left", padx=2)
		ttk.Button(btns, text="Obnovit .bak (Ctrl+B)", command=self.cover_restore_bak).pack(side="left", padx=2)
		ttk.Button(btns, text="Aplikovat novou (Ctrl+N)", command=self.cover_new).pack(side="left", padx=2)
		ttk.Button(btns, text="Smazat označené (Ctrl+M)", command=self.cover_delete_checked).pack(side="left", padx=10)

	def _build_content_section(self) -> None:
		box = ttk.LabelFrame(self._scroll_inner, text="Obsah")
		box.pack(fill="both", expand=True, padx=8, pady=4)
		top = ttk.Frame(box)
		top.pack(fill="x", padx=6, pady=4)
		ttk.Label(top, text="Formát:").pack(side="left")
		self._format_holder = ttk.Frame(top)
		self._format_holder.pack(side="left", fill="x", expand=True, padx=6)
		view = ttk.Frame(box)
		view.pack(fill="x", padx=6)
		ttk.Label(view, text="Zobrazení:").pack(side="left")
		ttk.Radiobutton(view, text="první strana", value="first", variable=self._view_var).pack(side="left", padx=4)
		ttk.Radiobutton(view, text="širší text", value="broader", variable=self._view_var).pack(side="left", padx=4)
		self._view_var.trace_add("write", lambda *_: self._apply_content())
		rec = ttk.Frame(box)
		rec.pack(fill="x", padx=6, pady=(2, 4))
		self._recode_chk = ttk.Checkbutton(
			rec, text="↻ Překódovat (Ctrl+G)", variable=self._recode_var,
			command=self._apply_content_text, state="disabled",
		)
		self._recode_chk.pack(side="left")
		# Manual codec experiment: pick z (what the text should be re-encoded
		# through) and do (what the recovered bytes really are); the preview
		# re-renders live, always as UTF-8 text, whatever the pair.
		ttk.Label(rec, text="  z:").pack(side="left")
		self._recode_from_box = ttk.Combobox(
			rec, textvariable=self._recode_from, values=list(ENCODING_CHOICES),
			width=13, state="readonly",
		)
		self._recode_from_box.pack(side="left", padx=(1, 4))
		ttk.Label(rec, text="do:").pack(side="left")
		self._recode_to_box = ttk.Combobox(
			rec, textvariable=self._recode_to, values=list(ENCODING_CHOICES),
			width=13, state="readonly",
		)
		self._recode_to_box.pack(side="left", padx=(1, 4))
		self._recode_hint = ttk.Label(rec, text="", foreground="#a00")
		self._recode_hint.pack(side="left", padx=8)
		self._recode_from.trace_add("write", lambda *_: self._recode_changed())
		self._recode_to.trace_add("write", lambda *_: self._recode_changed())
		body = ttk.Frame(box)
		body.pack(fill="both", expand=True, padx=6, pady=(0, 6))
		self._content_txt = self._style_text(
			tk.Text(body, wrap="word", state="disabled", height=12)
		)
		self._content_txt.pack(side="left", fill="both", expand=True)
		vsb = ttk.Scrollbar(body, orient="vertical", command=self._content_txt.yview)
		self._content_txt.configure(yscrollcommand=vsb.set)
		vsb.pack(side="right", fill="y")

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
			if node is self.tree:
				return "break"  # the list scrolled itself; the form stays put
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

	def _on_tab(self, event) -> str:
		shift = bool(event.state & 0x1)
		self._cycle_editable(forward=not shift)
		return "break"

	def _on_ctrl_key(self, event) -> str | None:
		k = (event.keysym or "").lower()
		if k in _PASSTHROUGH:
			return None  # keep native copy/paste/cut/undo/select-all
		dispatch = {
			"return": self.act_accept, "r": self.act_reject, "w": self.swap_fields,
			"e": self.act_edit, "d": self.act_delete, "k": self.act_keep,
			"0": self.act_clear, "s": self.save, "q": self.quit_app,
			"f": self.focus_search, "l": self.copy_current_to_focused,
			"space": self.toggle_include_focused, "n": self.cover_new,
			"b": self.cover_restore_bak, "p": self.cover_keep, "m": self.cover_delete_checked,
			"t": self.content_toggle_view, "g": self.content_recode_toggle,
		}
		handler = dispatch.get(k)
		if handler is not None:
			handler()
			return "break"
		return None

	def _cycle_editable(self, *, forward: bool) -> None:
		ws = self._editable_widgets
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
			kw = dict(iid=str(i), text=self._entry_label(e), values=(self._action_label(e),))
			img = self._thumb_photo_for(uuid)
			if img is not None:
				kw["image"] = img
			self.tree.insert("", "end", **kw)
		# Preserve selection.
		if sel_iid and self.tree.exists(sel_iid):
			self.tree.selection_set(sel_iid)
			self.tree.focus(sel_iid)
			self.tree.see(sel_iid)
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
	def _entry_label(e: dict) -> str:
		cur = e.get("current") or {}
		author = cur.get("author") or "—"
		title = cur.get("title") or "—"
		return f"{author} – {title}"

	@staticmethod
	def _action_label(e: dict) -> str:
		return e.get("action") or "·"

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
		action = self._action_var.get()
		e["action"] = action if action != "pending" else None
		selection = {r: (f["incl"].get(), f["value"].get()) for r, f in self._fields.items()}
		e["edited"] = compose_edited(selection)
		notes = self._notes_var.get().strip()
		e["notes"] = notes or None

	def _load_book(self, idx: int) -> None:
		e = self.entries[idx]
		self._loading = True
		try:
			# Header.
			diag = e.get("diagnosis") or {}
			uuid = e.get("uuid") or "—"
			path = e.get("path") or ""
			all_d = e.get("diagnoses") or [diag]
			extra = f"  (+{len(all_d) - 1} další)" if len(all_d) > 1 else ""
			self._header_lbl.configure(
				text=f"Záznam {idx + 1}/{len(self.entries)}   uuid: {uuid}\n"
				f"path: {path}\n"
				f"diagnóza: {diag.get('category', '—')} – {diag.get('reason', '')} "
				f"[{diag.get('confidence', '—')}]{extra}",
			)
			# Fields.
			cur = e.get("current") or {}
			prop = e.get("proposed") or {}
			edited = e.get("edited") or {}
			for role, f in self._fields.items():
				f["current"].set(self._display_value(cur.get(role)))
				if role in edited:
					target = edited[role]
				elif role in prop:
					target = prop[role]
				else:
					target = cur.get(role)
				f["value"].set(self._display_value(target))
				f["incl"].set(role in edited)
			# Proposed RO block.
			self._set_proposed(prop)
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

	def _set_proposed(self, prop: dict) -> None:
		self._proposed_txt.configure(state="normal")
		self._proposed_txt.delete("1.0", "end")
		if not prop:
			self._proposed_txt.insert("end", "(žádný návrh)")
		else:
			for k, v in prop.items():
				if isinstance(v, list):
					v = ", ".join(str(x) for x in v)
				self._proposed_txt.insert("end", f"{k}: {v}\n")
		self._proposed_txt.configure(state="disabled")

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
		if self._field_entries:
			self._field_entries[0].focus_set()
			self._see_widget(self._field_entries[0])

	def _focus_restore(self) -> None:
		role = self._last_field_role
		if role and role in self._fields:
			w = self._fields[role]["entry"]
			w.focus_set()
			self._see_widget(w)
		else:
			self._focus_first_field()

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

	def set_action(self, a: str) -> None:
		if 0 <= self._cur < len(self.entries):
			self._action_var.set(a)

	def act_accept(self): self.set_action("accept")
	def act_reject(self): self.set_action("reject")
	def act_edit(self): self.set_action("edit")
	def act_delete(self): self.set_action("delete")
	def act_keep(self): self.set_action("keep")
	def act_clear(self): self.set_action("pending")

	def swap_fields(self) -> None:
		"""Swap author <-> title target values, and mark the action as swap."""
		av = self._fields["author"]["value"]
		tv = self._fields["title"]["value"]
		a, t = av.get(), tv.get()
		av.set(t)
		tv.set(a)
		self._fields["author"]["incl"].set(True)
		self._fields["title"]["incl"].set(True)
		self.set_action("swap")

	def _copy_current(self, role: str) -> None:
		f = self._fields[role]
		f["value"].set(f["current"].get())
		f["incl"].set(True)

	def copy_current_to_focused(self) -> None:
		w = self.focus_get_safe()
		for role, f in self._fields.items():
			if f["entry"] is w:
				self._copy_current(role)
				return

	def toggle_include_focused(self) -> None:
		w = self.focus_get_safe()
		for f in self._fields.values():
			if f["entry"] is w:
				f["incl"].set(not f["incl"].get())
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
		except ValueError:
			pos = -1 if delta < 0 else 0
		nxt = idxs[(pos + delta) % len(idxs)]
		self.tree.selection_set(str(nxt))
		self.tree.focus(str(nxt))
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
		self._flash(f"uloženo → {self.review_path}", seconds=4)

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
			messagebox.showerror("bmf gui", f"uložení selhalo: {e}")
			return False
		return True

	def quit_app(self) -> None:
		if self._dirty:
			choice = messagebox.askyesnocancel("bmf gui", "Uložit změny před ukončením?")
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
		caps.append("generated" if (info and info.is_generated) else ("ok" if cur else "chybí"))
		caps.append(".bak záloha")
		caps.append("doporučená" if has_url else "bez URL")
		for lbl, _cap, pil in zip(self._cover_imgs, self._cover_caps, imgs, strict=False):
			if pil is not None:
				photo = self._fit_photo(pil, lbl)
				if photo is not None:
					self._cover_photos[id(pil)] = photo
					lbl.configure(image=photo, text="")
					continue
			lbl.configure(image="", text="(žádný náhled)")
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
			ttk.Label(self._fmt_cover_row, text="(žádné / calibre nedostupné)").pack(side="left")
			return
		for path, pil in fmt_covers:
			var = tk.BooleanVar(value=False)
			lbl, _cap = self._cover_cell(
				self._fmt_cover_row, path.name, var,
				f"Smazat soubor {path.name} (při Smazat označené)",
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
			self._flash("žádná doporučená obálka (cover_url)")
			return
		cover_path, _ = cover_paths(self.library, e.get("path", ""))
		if download_cover(url, cover_path):
			self._flash("obálka aplikována")
		else:
			self._flash("stažení obálky selhalo")
		self._refresh_covers()

	def cover_restore_bak(self) -> None:
		e = self.entries[self._cur]
		cover_path, bak_path = cover_paths(self.library, e.get("path", ""))
		if restore_bak_cover(cover_path, bak_path):
			self._flash("obnoveno z .bak")
		else:
			self._flash(".bak neexistuje")
		self._refresh_covers()

	def cover_keep(self) -> None:
		self._flash("ponecháno")

	def cover_delete_checked(self) -> None:
		e = self.entries[self._cur]
		cover_path, bak_path = cover_paths(self.library, e.get("path", ""))
		paths = []
		if self._del_cover.get():
			paths.append(cover_path)
		if self._del_bak.get():
			paths.append(bak_path)
		fmt_paths = [Path(p) for p, v in self._del_formats.items() if v.get()]
		if not paths and not fmt_paths:
			self._flash("nic neoznačeno ke smazání")
			return
		# Deleting ebook FILES is irreversible (sidecar covers are recoverable
		# via the enrichers / .bak; a deleted format is gone) — confirm first.
		if fmt_paths and not messagebox.askyesno(
			"bmf gui",
			"Smazat tyto soubory e-knihy?\n\n" + "\n".join(f"  • {p.name}" for p in fmt_paths),
		):
			return
		n = delete_covers(paths + fmt_paths)
		self._del_cover.set(False)
		self._del_bak.set(False)
		for v in self._del_formats.values():
			v.set(False)
		self._flash(f"smazáno {n}")
		self._refresh_covers()
		self._refresh_formats()  # the format radios / content changed too

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
			ttk.Label(self._format_holder, text="(žádné formáty / složka nenalezena)").pack(side="left")
			self._content_raw = ""
			self._content_repaired = None
			self._recode_var.set(False)
			self._recode_chk.configure(state="disabled")
			self._recode_hint.configure(text="")
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
		self._set_content_text("(načítám…)")

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
			self._recode_hint.configure(text="")
			self._set_content_text("")
			return
		view = self._view_var.get()
		raw = (meta.broader_text if view == "broader" else meta.first_page_text) or ""
		if not raw and meta.error:
			raw = f"(extrakce selhala: {meta.error})"
		elif not raw:
			raw = "(žádný text)"
		self._content_raw = raw
		# Detect double-encoding (utf-8 mis-decoded twice): default the z/do
		# selectors to the usual CZ suspect and show the repaired text. A
		# clean book keeps the user's last pair, so manual experimenting
		# works even when the detector saw nothing.
		if detect_double_decode(raw):
			self._recode_hint.configure(text="⚠ detekováno dvojí kódování")
			self._recode_from.set("cp1250")
			self._recode_to.set("utf-8")
			self._recode_var.set(True)  # default to the readable, repaired text
		self._recompute_recode()
		self._apply_content_text()

	def _recompute_recode(self) -> None:
		"""Recompute the transformed text from the current z/do pair.

		The toggle is enabled only when the pair yields an actual change; a
		failing pair is reported in the hint — the user is experimenting, so
		telling them a combination cannot run is the point.
		"""
		frm, to = self._recode_from.get(), self._recode_to.get()
		self._content_repaired = recode(self._content_raw, frm, to)
		if self._content_repaired is None:
			self._recode_chk.configure(state="disabled")
			self._recode_var.set(False)
			if self._content_raw:
				self._recode_hint.configure(text=f"⚠ z {frm} do {to}: převod selhal")
		elif self._content_repaired == self._content_raw:
			self._recode_chk.configure(state="disabled")
			self._recode_var.set(False)
		else:
			self._recode_chk.configure(state="normal")

	def _recode_changed(self, *_args) -> None:
		"""A codec was picked (z/do) — live-preview the result from page one."""
		if not self._content_raw:
			return
		self._recompute_recode()
		if self._content_repaired is not None and self._content_repaired != self._content_raw:
			self._recode_var.set(True)
			self._recode_hint.configure(
				text=f"z {self._recode_from.get()} → {self._recode_to.get()} ✓")
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
			self._flash("žádné dvojí kódování k opravě")
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
		if not (0 <= self._cur < len(self.entries)):
			base = f"{len(self.entries)} záznamů"
		else:
			base = f"{self._cur + 1}/{len(self.entries)}"
		dirty = " *" if self._dirty else ""
		base += dirty
		if extra:
			base += f"   —   {extra}"
		self._status.set(base)

	def _help_overlay(self) -> None:
		win = tk.Toplevel(self.root)
		win.title("Klávesové zkratky (F1)")
		txt = tk.Text(win, width=54, height=30, wrap="word")
		txt.pack(fill="both", expand=True)
		shortcuts = [
			("PgDn / PgUp", "další / předchozí kniha"),
			("Tab / Shift+Tab", "další / předchozí editační pole (jen pole)"),
			("Ctrl+A", "označit vše v poli"),
			("Ctrl+Enter", "accept"),
			("Ctrl+R", "reject"),
			("Ctrl+W", "prohodit autora↔titul (+akce swap)"),
			("Ctrl+E", "edit"),
			("Ctrl+D", "delete"),
			("Ctrl+K", "keep (aplikuje a ponechá; analyze přeskočí)"),
			("Ctrl+0", "vyčistit → pending"),
			("Ctrl+S", "uložit"),
			("Ctrl+Q", "konec"),
			("Ctrl+F", "focus hledání"),
			("Ctrl+L", "current → cíl (fokus pole)"),
			("Ctrl+Space", "☑ include fokus pole"),
			("Ctrl+N", "obálka: aplikovat novou"),
			("Ctrl+B", "obálka: obnovit .bak"),
			("Ctrl+P", "obálka: ponechat"),
			("Ctrl+M", "obálka: smazat označené (cover/.bak/soubory formátů)"),
			("Ctrl+T", "obsah: první strana / širší text"),
			("Ctrl+G", "obsah: překódovat (z/do kodeky volí komboboxy; výsledek vždy UTF-8)"),
			("", "(kolečko: prvek pod myší, na jeho hraně formulář; obálka v seznamu → hover popup)"),
		]
		for k, d in shortcuts:
			txt.insert("end", f"{k:<18} {d}\n")
		txt.configure(state="disabled")
