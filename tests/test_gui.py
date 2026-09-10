"""Unit tests for the pure helpers in book_meta_fix.gui.

These exercise the no-Tk, no-network logic (field composition, cover path &
file handling, review.yaml round-trip rendering). The Tkinter UI itself is
mostly untested headlessly — it is kept thin and delegates to these helpers —
except for a few Tk-gated smoke tests (tooltip, per-format cover row) that
skip when tkinter or a display is unavailable.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

import book_meta_fix.gui as gui
from book_meta_fix.gui import (
	MergeOutcome,
	action_value,
	apply_bulk_action,
	apply_bulk_field,
	apply_bulk_verified,
	build_library_index,
	collect_vocab_values,
	compose_overlay,
	cover_paths,
	delete_covers,
	embedded_cover_thumb,
	entries_to_write,
	entry_author_label,
	entry_label,
	entry_matches_search,
	entry_series_label,
	entry_series_pair,
	entry_sort_key,
	execute_bulk_cover_delete,
	execute_merge,
	extract_series_values,
	library_entry_changed,
	library_entry_from_meta,
	list_format_files,
	merge_cell_text,
	merge_field_value,
	open_folder_in_manager,
	render_review_text,
	restore_bak_cover,
	search_library_index,
)
from book_meta_fix.review import _load_raw_entries


def _make_jpeg(path: Path, size: tuple[int, int] = (600, 800), color: tuple = (10, 20, 30)) -> None:
	from PIL import Image

	Image.new("RGB", size, color).save(path, format="JPEG")


def _jpeg_bytes(size: tuple[int, int] = (600, 800), color: tuple = (10, 20, 30)) -> bytes:
	from PIL import Image

	buf = io.BytesIO()
	Image.new("RGB", size, color).save(buf, format="JPEG")
	return buf.getvalue()


def _make_epub(path: Path) -> Path:
	"""Minimal EPUB whose OPF wires images/cover.jpg as the cover."""
	with zipfile.ZipFile(path, "w") as zf:
		zf.writestr("mimetype", "application/epub+zip")
		zf.writestr(
			"META-INF/container.xml",
			'<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
			'<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
		)
		zf.writestr(
			"content.opf",
			'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
			'<metadata><meta name="cover" content="cvr"/></metadata>'
			'<manifest><item id="cvr" href="images/cover.jpg" media-type="image/jpeg"/></manifest>'
			"<spine/></package>",
		)
		zf.writestr("images/cover.jpg", _jpeg_bytes())
	return path


class TestComposeOverlay:
	def test_filled_fields_returned_empty_skipped(self):
		sel = compose_overlay({"author": "X", "title": "", "isbn": "80-1-2"})
		assert sel == {"author": "X", "isbn": "80-1-2"}

	def test_year_coerced_to_int(self):
		assert compose_overlay({"year": "1989"}) == {"year": 1989}

	def test_year_non_numeric_kept_as_string(self):
		assert compose_overlay({"year": "cca 1989"}) == {"year": "cca 1989"}

	def test_list_fields_split_on_comma(self):
		assert compose_overlay({"authors": " A , B ,C "}) == {"authors": ["A", "B", "C"]}
		assert compose_overlay({"genres": "sci-fi, fantasy"}) == {"genres": ["sci-fi", "fantasy"]}

	def test_none_when_nothing_filled(self):
		assert compose_overlay({"author": "", "title": "   "}) is None
		assert compose_overlay({}) is None

	def test_cleared_role_stored_as_null(self):
		# The ∅ mark: whatever the Entry holds, the field is stored as an
		# explicit null ("delete the value") and apply clears it.
		sel = compose_overlay({"author": "X", "series": "bogus"}, cleared={"series"})
		assert sel == {"author": "X", "series": None}

	def test_cleared_alone_is_not_none(self):
		# A single ∅ mark must still produce an overlay (not the "nothing
		# changed" None), otherwise the mark would be dropped on merge.
		assert compose_overlay({"year": ""}, cleared={"year"}) == {"year": None}

	def test_series_fields_pass_through_as_strings(self):
		"""Série / Pořadí are plain string fields (no comma split, no int
		coercion) — apply packs them into meta.series as {"name","index"}."""
		sel = compose_overlay({"series": "Nadace", "series_index": "3", "title": ""})
		assert sel == {"series": "Nadace", "series_index": "3"}

	def test_field_specs_cover_all_apply_fields(self):
		"""Every field _apply_fields understands from ``proposed`` must be
		editable in the GUI, otherwise the user cannot reach it by hand."""
		editable = {role for role, _label in gui.FIELD_SPECS}
		assert {"author", "title", "isbn", "year", "publisher", "language",
				"series", "series_index", "authors", "genres"} <= editable


class TestActionValue:
	def test_pending_serialises_as_null(self):
		# "pending" is the radio's no-decision state; on disk it must be
		# ``action: null`` (the GUI list shows the · glyph for it).
		assert action_value("pending") is None

	def test_decisions_pass_through(self):
		for a in ("accept", "delete", "keep"):
			assert action_value(a) == a


class TestApplyBulkField:
	"""The Ctrl+E bulk edit: one author/series value into many proposals."""

	def test_merges_value_and_decides_pending(self):
		entries = [
			{"current": {"author": "A"}, "proposed": {"title": "T"}, "action": None},
			{"current": {"author": "B"}, "proposed": None, "action": None},
		]
		assert apply_bulk_field(entries, [0, 1], "author", "Petr Vraník") == 2
		# Existing proposal keys survive the merge.
		assert entries[0]["proposed"] == {"title": "T", "author": "Petr Vraník"}
		assert entries[1]["proposed"] == {"author": "Petr Vraník"}
		# A proposal without a decision is skipped by apply — the bulk edit
		# IS the decision (the same rule the ∅ button follows).
		assert entries[0]["action"] == "accept"
		assert entries[1]["action"] == "accept"

	def test_existing_decision_kept(self):
		entries = [{"current": {}, "proposed": None, "action": "keep"}]
		apply_bulk_field(entries, [0], "series", "Nadace")
		assert entries[0]["action"] == "keep"

	def test_delete_stores_null(self):
		entries = [{"current": {"series": "špatná"}, "proposed": None, "action": None}]
		apply_bulk_field(entries, [0], "series", "", delete=True)
		assert entries[0]["proposed"]["series"] is None
		assert entries[0]["action"] == "accept"

	def test_series_order_untouched(self):
		# Only the NAME is touched — the per-book order (the C14 prefill
		# included) stays as it was.
		entries = [{"current": {"series": "Mark Stone #73"},
		            "proposed": {"series": "Mark Stone", "series_index": "73"},
		            "action": None}]
		apply_bulk_field(entries, [0], "series", "Mark Stone (V)")
		assert entries[0]["proposed"] == {"series": "Mark Stone (V)",
		                                  "series_index": "73"}

	def test_empty_value_without_delete_is_noop(self):
		entries = [{"current": {}, "proposed": None, "action": None}]
		assert apply_bulk_field(entries, [0], "author", "   ") == 0
		assert entries[0]["proposed"] is None
		assert entries[0]["action"] is None

	def test_out_of_range_indices_skipped(self):
		entries = [{"current": {}, "proposed": None, "action": None}]
		assert apply_bulk_field(entries, [0, 5], "author", "X") == 1

	def test_proposed_rebound_not_mutated(self):
		# A library-served entry SHARES its `proposed` dict with the pristine
		# index (search serves shallow copies) — the merge must rebind, never
		# mutate the shared dict in place.
		shared = {"series": "old"}
		entries = [{"current": {}, "proposed": shared, "action": None}]
		apply_bulk_field(entries, [0], "series", "new")
		assert shared == {"series": "old"}
		assert entries[0]["proposed"] == {"series": "new"}


class TestEntryAuthorLabel:
	"""The row's author follows the same decision-aware policy as the series
	label: accept/keep shows the proposed author (a bulk edit or a C1 swap
	is visible in the list immediately, not only after apply)."""

	def test_current_when_pending(self):
		e = {"current": {"author": "Old"}, "proposed": {"author": "New"}}
		assert entry_author_label(e) == "Old"

	def test_accepted_proposal_overlays_current(self):
		for a in ("accept", "keep"):
			e = {"action": a, "current": {"author": "Old"},
			     "proposed": {"author": "New"}}
			assert entry_author_label(e) == "New"

	def test_accepted_without_author_proposal_shows_current(self):
		e = {"action": "accept", "current": {"author": "Old"},
		     "proposed": {"isbn": "X"}}
		assert entry_author_label(e) == "Old"

	def test_accepted_null_clears(self):
		e = {"action": "accept", "current": {"author": "Old"},
		     "proposed": {"author": None}}
		assert entry_author_label(e) == ""

	def test_missing_author_is_empty(self):
		assert entry_author_label({}) == ""
		assert entry_author_label({"current": {}}) == ""
		assert entry_author_label(None) == ""


class TestBulkSelectionIndices:
	def test_parses_valid_indices_sorted_deduped(self):
		import types

		app = gui.ReviewEditorApp.__new__(gui.ReviewEditorApp)
		app.entries = [{"uuid": "a"}, {"uuid": "b"}, {"uuid": "c"}]
		app.tree = types.SimpleNamespace(
			selection_get=lambda: ("2", "0", "bogus", "9"))
		assert app._bulk_selection_indices() == [0, 2]


class TestApplyBulkAction:
	"""Ctrl+Shift+A: an explicit selection IS the decision — unlike the bulk
	field edit (which only decides pending entries) it OVERRIDES."""

	def test_overrides_previous_decisions(self):
		entries = [
			{"current": {}, "action": None},
			{"current": {}, "action": "keep"},
			{"current": {}, "action": "delete"},
		]
		assert apply_bulk_action(entries, [0, 1, 2], "accept") == 3
		assert [e["action"] for e in entries] == ["accept", "accept", "accept"]

	def test_out_of_range_indices_skipped(self):
		entries = [{"current": {}, "action": None}]
		assert apply_bulk_action(entries, [0, 7], "accept") == 1


class TestApplyBulkVerified:
	"""Ctrl+Shift+O: the persistent OK mark en masse (set pops as None)."""

	def test_set_and_clear(self):
		entries = [{"uuid": "a"}, {"uuid": "b", "verified": True}, {"uuid": "c"}]
		assert apply_bulk_verified(entries, [0, 1], True) == 2
		assert entries[0].get("verified") is True
		assert entries[1].get("verified") is True
		assert apply_bulk_verified(entries, [0, 1, 9], False) == 2
		assert "verified" not in entries[0]
		assert "verified" not in entries[1]
		assert "verified" not in entries[2]  # never touched


class TestEntryLabel:
	def test_title_and_author_from_current(self):
		e = {"current": {"title": "Babička", "author": "Božena Němcová"}}
		assert entry_label(e) == "Babička — Božena Němcová"

	def test_falls_back_to_proposed_title(self):
		e = {"current": {}, "proposed": {"title": "Saturnin"}}
		assert entry_label(e).startswith("Saturnin —")

	def test_empty_entry(self):
		assert entry_label({}) == "? — "


class TestBulkActionsApp:
	"""The app methods drive the helpers + reload/refresh (bare app, no Tk)."""

	def _bare_app(self, selection):
		import types

		app = gui.ReviewEditorApp.__new__(gui.ReviewEditorApp)
		app.entries = [
			{"uuid": "a", "current": {}, "action": None},
			{"uuid": "b", "current": {}, "action": "keep"},
		]
		app.tree = types.SimpleNamespace(selection_get=lambda: selection)
		app._cur = 1
		app._loading = False
		app._dirty = False
		app._set_status = lambda extra="": None
		app.calls = []
		app._collect_current = lambda: app.calls.append("collect")
		app._load_book = lambda idx: app.calls.append(("load", idx))
		app.refresh_list = lambda: app.calls.append("refresh")
		app.flashes = []
		app._flash = lambda msg, seconds=None: app.flashes.append(msg)
		return app

	def test_bulk_accept_overrides_and_reloads_current(self):
		app = self._bare_app(("0", "1"))
		app.bulk_accept()
		assert [e["action"] for e in app.entries] == ["accept", "accept"]
		assert ("load", 1) in app.calls  # the current book was selected → reload
		assert app.flashes == ["bulk accept: 2 books"]
		assert app._dirty is True

	def test_bulk_accept_without_selection_flashes(self):
		app = self._bare_app(())
		app.bulk_accept()
		assert app.entries[1]["action"] == "keep"
		assert app.flashes == ["select books first (Ctrl+click, Shift+click)"]

	def test_bulk_toggle_verified_sets_then_clears(self):
		app = self._bare_app(("0", "1"))
		app.bulk_toggle_verified()
		assert all(e.get("verified") for e in app.entries)
		app.bulk_toggle_verified()  # ALL selected verified → clear
		assert not any(e.get("verified") for e in app.entries)

	def test_bulk_toggle_verified_partial_selection_still_sets(self):
		app = self._bare_app(("0",))
		app.entries[1]["verified"] = True  # verified but NOT selected → set
		app.bulk_toggle_verified()
		assert app.entries[0].get("verified") is True
		assert app.entries[1].get("verified") is True


class TestCtrlKeyDispatch:
	"""Shift bulk shortcuts must win over the Ctrl passthrough set."""

	def _bare_app(self):
		import types

		app = gui.ReviewEditorApp.__new__(gui.ReviewEditorApp)
		app.root = types.SimpleNamespace(grab_current=lambda: None)
		app.calls = []
		for name in ("bulk_accept", "bulk_toggle_verified", "bulk_delete_covers",
		             "merge_selected", "save"):
			setattr(app, name, (lambda n: lambda: app.calls.append(n))(name))
		return app

	def test_shift_plus_a_dispatches_bulk_accept(self):
		app = self._bare_app()
		# Uppercase keysym is what X11 delivers with Shift held.
		res = app._on_ctrl_key(type("E", (), {"keysym": "A", "state": 0x0001})())
		assert res == "break"
		assert app.calls == ["bulk_accept"]

	def test_shift_unknown_letter_falls_through(self):
		app = self._bare_app()
		# Ctrl+Shift+C must keep its native meaning (copy in entries).
		res = app._on_ctrl_key(type("E", (), {"keysym": "c", "state": 0x0001})())
		assert res is None
		assert app.calls == []

	def test_plain_a_stays_passthrough(self):
		app = self._bare_app()
		res = app._on_ctrl_key(type("E", (), {"keysym": "a", "state": 0})())
		assert res is None
		assert app.calls == []

	def test_j_dispatches_merge(self):
		app = self._bare_app()
		res = app._on_ctrl_key(type("E", (), {"keysym": "j", "state": 0})())
		assert res == "break"
		assert app.calls == ["merge_selected"]


class TestExecuteBulkCoverDelete:
	"""Ctrl+Shift+M: sidecar/embedded cover removal + the cover_url drop."""

	def _lib(self, tmp_path):
		folder = tmp_path / "Autor" / "Kniha (1)"
		folder.mkdir(parents=True)
		_make_jpeg(folder / "cover.jpg")
		_make_jpeg(folder / "cover.jpg.bak")
		(folder / "kniha.epub").write_bytes(b"not a real epub")
		entries = [{"uuid": "u1", "path": "Autor/Kniha (1)",
		            "proposed": {"cover_url": "https://x/cover.jpg", "title": "T"},
		            "action": None}]
		return folder, entries

	def test_deletes_sidecars_and_drops_cover_url(self, tmp_path):
		folder, entries = self._lib(tmp_path)
		removed, _stripped = execute_bulk_cover_delete(
			entries, [0], tmp_path, covers=True, baks=True)
		assert removed == 2
		assert not (folder / "cover.jpg").exists()
		assert not (folder / "cover.jpg.bak").exists()
		# apply re-downloads a proposed cover for C11/MISSING_COVER books —
		# keeping the URL would undo the deletion on the next run.
		assert "cover_url" not in entries[0]["proposed"]
		assert entries[0]["proposed"]["title"] == "T"  # the rest survives

	def test_proposed_rebound_not_mutated(self, tmp_path):
		_folder, entries = self._lib(tmp_path)
		shared = {"cover_url": "https://x/c.jpg"}
		entries[0]["proposed"] = shared
		execute_bulk_cover_delete(entries, [0], tmp_path, covers=True)
		assert shared == {"cover_url": "https://x/c.jpg"}  # index copy untouched
		assert entries[0]["proposed"] == {}

	def test_bak_only_keeps_cover_url(self, tmp_path):
		folder, entries = self._lib(tmp_path)
		execute_bulk_cover_delete(entries, [0], tmp_path, covers=False, baks=True)
		assert (folder / "cover.jpg").is_file()
		assert not (folder / "cover.jpg.bak").exists()
		# Only deleting the sidecar itself contradicts the proposal.
		assert entries[0]["proposed"]["cover_url"] == "https://x/cover.jpg"

	def test_embedded_strip_counts_only_successes(self, tmp_path):
		folder, entries = self._lib(tmp_path)
		removed, stripped = execute_bulk_cover_delete(
			entries, [0], tmp_path, covers=False, embedded=True)
		assert removed == 0
		assert stripped == 0  # "not a real epub" cannot be stripped
		assert (folder / "kniha.epub").is_file()  # the file itself stays

	def test_missing_files_are_not_counted(self, tmp_path):
		entries = [{"uuid": "u", "path": "Autor/Prázdné (2)"}]
		removed, _stripped = execute_bulk_cover_delete(entries, [0], tmp_path)
		assert removed == 0


def _make_lib_book(lib: Path, rel: str, uuid: str, *, title: str, author: str,
                   fmts: tuple[str, ...] = (".epub",),
                   extra_meta: dict | None = None) -> Path:
	"""Create <lib>/<rel>/ with metadata.json + dummy format files."""
	import json

	folder = lib / rel
	folder.mkdir(parents=True)
	md = {"title": title, "authors": [author], "uuid": uuid, **(extra_meta or {})}
	(folder / "metadata.json").write_text(json.dumps(md), encoding="utf-8")
	for ext in fmts:
		(folder / f"{title} - {author}{ext}").write_bytes(f"{uuid}{ext}".encode())
	return folder


class TestExecuteMerge:
	"""Ctrl+J: files move into the survivor, losers leave the list."""

	def _setup(self, tmp_path):
		w_folder = _make_lib_book(tmp_path, "A/Hlavní (1)", "uw",
		                          title="Hlavní", author="A", fmts=(".epub",))
		l_folder = _make_lib_book(tmp_path, "A/Hlavní (2)", "ul",
		                          title="Hlavní", author="A", fmts=(".pdb",),
		                          extra_meta={"isbn": "978-0-30-640615-7"})
		entries = [
			{"uuid": "uw", "path": "A/Hlavní (1)",
			 "current": {"title": "Hlavní", "author": "A"},
			 "proposed": {"language": "cs"}, "action": None},
			{"uuid": "ul", "path": "A/Hlavní (2)",
			 "current": {"title": "Hlavní", "author": "A"},
			 "proposed": None, "action": "accept"},
		]
		return w_folder, l_folder, entries

	def test_merges_files_drops_looser_keeps_winner(self, tmp_path):
		w_folder, l_folder, entries = self._setup(tmp_path)
		out = execute_merge(entries, 0, [1], tmp_path)
		assert out.merged_count == 1
		assert out.dropped_uuids == ["ul"]
		assert out.failures == []
		assert out.moved_files == 1
		# The loser's format moved into the winner folder; loser folder gone.
		assert (w_folder / "Hlavní - A.pdb").is_file()
		assert (w_folder / "Hlavní - A.epub").is_file()
		assert not l_folder.exists()
		# Entry surgery: the loser left the list, the winner survived with its
		# decision/proposal untouched (apply finishes it later).
		assert len(entries) == 1
		assert entries[0]["uuid"] == "uw"
		assert entries[0]["action"] is None
		assert entries[0]["proposed"] == {"language": "cs"}
		assert out.winner is entries[0]
		# The winner's current reflects the merged disk metadata (the isbn
		# was filled from the loser — merge_meta unions the gaps; readers
		# canonicalize it to bare digits).
		assert entries[0]["current"]["isbn"] == "9780306406157"

	def test_missing_loser_folder_reports_failure_and_stays(self, tmp_path):
		_w, _l, entries = self._setup(tmp_path)
		entries[1]["path"] = "A/Chybí (9)"
		out = execute_merge(entries, 0, [1], tmp_path)
		assert out.merged_count == 0
		assert len(out.failures) == 1
		assert "folder not found" in out.failures[0]
		assert len(entries) == 2  # the failed loser stays in the list

	def test_same_folder_guard(self, tmp_path):
		w_folder, _l, entries = self._setup(tmp_path)
		entries[1]["path"] = entries[0]["path"]
		out = execute_merge(entries, 0, [1], tmp_path)
		assert out.merged_count == 0
		assert len(out.failures) == 1
		assert w_folder.is_dir()  # nothing happened to the survivor

	def test_out_of_range_loser_ignored(self, tmp_path):
		_w, _l, entries = self._setup(tmp_path)
		out = execute_merge(entries, 0, [1, 42], tmp_path)
		assert out.merged_count == 1


class TestMergeFieldValue:
	"""The merge grid's effective value: a DECIDED proposal beats the disk
	(the same convention the list labels follow — it is what apply writes)."""

	def _meta(self, **kw):
		from book_meta_fix.models import BookMeta

		return BookMeta(**kw)

	def test_decided_proposal_wins(self):
		meta = self._meta(title="Disk", authors=["A"])
		e = {"action": "accept", "proposed": {"title": "Fixed", "author": "B"}}
		assert merge_field_value(e, meta, "title") == "Fixed"
		assert merge_field_value(e, meta, "authors") == ["B"]

	def test_pending_shows_disk_value(self):
		meta = self._meta(title="Disk", authors=["A"])
		e = {"action": None, "proposed": {"title": "Suggestion"}}
		assert merge_field_value(e, meta, "title") == "Disk"
		assert merge_field_value(e, meta, "authors") == ["A"]

	def test_null_proposal_is_empty(self):
		meta = self._meta(title="Disk")
		e = {"action": "accept", "proposed": {"title": None}}
		assert merge_field_value(e, meta, "title") is None

	def test_series_from_proposal_and_disk(self):
		meta = self._meta(series=[{"name": "Nadace", "index": "3"}])
		e = {"action": "accept", "proposed": {"series": "Legie", "series_index": "7"}}
		assert merge_field_value(e, meta, "series") == ("Legie", "7")
		assert merge_field_value({"action": None}, meta, "series") == ("Nadace", "3")

	def test_authors_proposal_list_scalar_and_null(self):
		meta = self._meta(authors=["A"])
		assert merge_field_value(
			{"action": "keep", "proposed": {"authors": ["X", "Y"]}}, meta,
			"authors") == ["X", "Y"]
		assert merge_field_value(
			{"action": "keep", "proposed": {"author": None}}, meta,
			"authors") == []

	def test_disk_fallbacks(self):
		meta = self._meta(title="T", isbn="80-1", year=1984, publisher="P",
		                  language="cs", genres=["g1"], description="d")
		e = {"action": None}
		assert merge_field_value(e, meta, "isbn") == "80-1"
		assert merge_field_value(e, meta, "year") == 1984
		assert merge_field_value(e, meta, "publisher") == "P"
		assert merge_field_value(e, meta, "language") == "cs"
		assert merge_field_value(e, meta, "genres") == ["g1"]
		assert merge_field_value(e, meta, "description") == "d"


class TestMergeCellText:
	def test_empty_variants(self):
		assert merge_cell_text("title", None) == "∅"
		assert merge_cell_text("title", "") == "∅"
		assert merge_cell_text("authors", []) == "∅"
		assert merge_cell_text("series", ("", "3")) == "∅"

	def test_series_pair_display(self):
		assert merge_cell_text("series", ("Nadace", "3")) == "Nadace #3"
		assert merge_cell_text("series", ("Legie", "")) == "Legie"

	def test_list_joined_and_truncation(self):
		assert merge_cell_text("authors", ["A", "B"]) == "A, B"
		assert len(merge_cell_text("title", "x" * 100)) == 56
		assert merge_cell_text("title", "x" * 100).endswith("…")


class TestExecuteMergeChoices:
	"""The dialog's per-field picks override the automatic field merge."""

	def _setup(self, tmp_path):
		w_folder = _make_lib_book(tmp_path, "A/Vítěz (1)", "uw",
		                          title="Titul vítěze", author="A", fmts=(".epub",))
		l_folder = _make_lib_book(tmp_path, "A/Poražený (2)", "ul",
		                          title="Titul poraženého", author="B",
		                          fmts=(".pdb",),
		                          extra_meta={"publisher": "Albatros",
		                                      "publishedYear": "1984",
		                                      "isbn": "978-0-30-640615-7"})
		entries = [
			{"uuid": "uw", "path": "A/Vítěz (1)",
			 "current": {"title": "Titul vítěze", "author": "A"},
			 "proposed": {"title": "Starý návrh", "language": "cs"},
			 "action": "accept"},
			{"uuid": "ul", "path": "A/Poražený (2)",
			 "current": {"title": "Titul poraženého", "author": "B"},
			 "proposed": None, "action": None},
		]
		return w_folder, l_folder, entries

	def test_choices_override_merged_metadata(self, tmp_path):
		from book_meta_fix.readers import read_book_folder

		w_folder, _l, entries = self._setup(tmp_path)
		values = {
			"title": "Titul poraženého",   # the loser's title wins
			"authors": ["B"],
			"publisher": "Albatros",
			"year": 1984,
			"series": ("Nadace", "3"),
		}
		out = execute_merge(entries, 0, [1], tmp_path, values=values)
		assert out.merged_count == 1
		meta = read_book_folder(w_folder)
		assert meta.title == "Titul poraženého"
		assert meta.authors == ["B"]
		assert meta.publisher == "Albatros"
		assert meta.year == 1984
		name, idx = meta.series_pair()
		assert (name, str(idx)) == ("Nadace", "3")
		# The winner's current reflects the chosen values...
		assert entries[0]["current"]["title"] == "Titul poraženého"
		assert entries[0]["current"]["author"] == "B"
		# ...a CONFLICTING proposed key is rebased to the choice (a stale
		# proposal must not undo the pick at the next apply)...
		assert entries[0]["proposed"]["title"] == "Titul poraženého"
		# ...while untouched proposal keys survive.
		assert entries[0]["proposed"]["language"] == "cs"

	def test_no_proposal_invented_for_clean_fields(self, tmp_path):
		_w, _l, entries = self._setup(tmp_path)
		entries[0]["proposed"] = None
		out = execute_merge(entries, 0, [1], tmp_path,
		                    values={"title": "Titul poraženého"})
		assert out.merged_count == 1
		# No conflict existed — no proposal noise is invented for a clean book.
		assert entries[0]["proposed"] is None

	def test_series_pick_rebases_proposal_pair(self, tmp_path):
		_w, _l, entries = self._setup(tmp_path)
		entries[0]["proposed"] = {"series": "Stará", "series_index": "1"}
		execute_merge(entries, 0, [1], tmp_path,
		              values={"series": ("Nadace", "3")})
		assert entries[0]["proposed"]["series"] == "Nadace"
		assert entries[0]["proposed"]["series_index"] == "3"

	def test_empty_pick_clears_field_and_rebases_proposal(self, tmp_path):
		from book_meta_fix.readers import read_book_folder

		w_folder, _l, entries = self._setup(tmp_path)
		entries[0]["proposed"] = {"isbn": "80-1"}
		# The automatic merge would FILL the isbn from the loser; the ∅ pick
		# explicitly keeps the merged book without one.
		execute_merge(entries, 0, [1], tmp_path, values={"isbn": None})
		assert read_book_folder(w_folder).isbn is None
		assert entries[0]["proposed"]["isbn"] is None  # ∅ delete-mark semantics

	def test_choices_skipped_when_nothing_merged(self, tmp_path):
		_w, _l, entries = self._setup(tmp_path)
		entries[1]["path"] = "A/Chybí (9)"
		out = execute_merge(entries, 0, [1], tmp_path, values={"title": "X"})
		assert out.merged_count == 0
		# Nothing merged → nothing decided; the winner stays untouched.
		assert entries[0]["current"] == {"title": "Titul vítěze", "author": "A"}
		assert entries[0]["proposed"]["title"] == "Starý návrh"


class TestAfterMerge:
	"""Post-merge cleanup: index/thumbs pruning, immediate save, selection."""

	def _bare_app(self, entries, tmp_path):
		import types

		app = gui.ReviewEditorApp.__new__(gui.ReviewEditorApp)
		app.entries = entries
		app.library = tmp_path
		app._lib_uuids = {"ul": "hay"}
		app._lib_index = [({"uuid": "ul", "path": "x"}, "hay"),
		                  ({"uuid": "other", "path": "y"}, "hay2")]
		app._thumbs_pil = {"ul": object(), "other": object()}
		app._thumbs_photo = {"ul": object()}
		app._big_thumbs = {"ul": object()}
		app.cfg = types.SimpleNamespace(cache_db=tmp_path / "cache.db")
		app.saved = []
		app._do_save = lambda: (app.saved.append(1), True)[1]
		app._dirty = True
		app.refreshed = []
		app.refresh_list = lambda: app.refreshed.append(1)
		app.picked = []
		app.tree = types.SimpleNamespace(
			selection_set=lambda iid, silent=False: app.picked.append(iid),
			see=lambda iid: None)
		app.loaded = []
		app._load_book = lambda idx: app.loaded.append(idx)
		app._reload_thumbs = lambda idxs: None
		app.flashes = []
		app._flash = lambda msg, seconds=None: app.flashes.append(msg)
		return app

	def test_prunes_saves_and_selects_winner(self, tmp_path):
		w_folder, _l_folder, entries = TestExecuteMerge()._setup(tmp_path)
		out = execute_merge(entries, 0, [1], tmp_path)
		app = self._bare_app(entries, tmp_path)
		app._cur = 1  # stale focus on the (now dropped) loser row
		app._after_merge(out, str(w_folder), [str(tmp_path / "A" / "Hlavní (2)")])
		# review.yaml must agree with the disk RIGHT NOW (losers are gone).
		assert app.saved == [1]
		assert app._dirty is False
		assert app.picked == ["0"]
		assert app.loaded == [0]
		# _load_book alone does not move _cur — a stale one would crash
		# _refresh_covers after the list shrank.
		assert app._cur == 0
		# The library index / thumb caches must not resurrect the loser.
		assert "ul" not in app._lib_uuids
		assert [e.get("uuid") for e, _h in app._lib_index] == ["other"]
		assert "ul" not in app._thumbs_pil
		assert "ul" not in app._big_thumbs
		assert app.flashes == ["merged 1 books, 1 files moved"]

	def test_nothing_merged_flashes_only(self, tmp_path):
		_w, _l, entries = TestExecuteMerge()._setup(tmp_path)
		app = self._bare_app(entries, tmp_path)
		out = MergeOutcome(winner=entries[0], merged_count=0,
		                   dropped_uuids=[], failures=[], moved_files=0)
		app._after_merge(out, "/x", [])
		assert app.flashes == ["nothing merged"]
		assert app.saved == []  # no save, nothing changed


class TestCoverPaths:
	def test_paths_beside_book_folder(self):
		cur, bak = cover_paths("/lib", "Autor/Titul (1)")
		assert cur == Path("/lib/Autor/Titul (1)/cover.jpg")
		assert bak == Path("/lib/Autor/Titul (1)/cover.jpg.bak")


class TestListFormatFiles:
	def test_orders_by_ebook_preference_and_filters(self, tmp_path):
		(tmp_path / "note.md").write_text("x")
		(tmp_path / "b.epub").write_text("x")
		(tmp_path / "a.pdf").write_text("x")
		(tmp_path / "c.txt").write_text("x")
		# metadata sidecars are not ebook files.
		(tmp_path / "metadata.json").write_text("{}")
		files = [f.name for f in list_format_files(tmp_path)]
		# .epub (pref 0) before .pdf (pref 1) before .txt; .md/.json excluded.
		assert files == ["b.epub", "a.pdf", "c.txt"]

	def test_missing_folder_returns_empty(self, tmp_path):
		assert list_format_files(tmp_path / "does-not-exist") == []


class TestRestoreBakCover:
	def test_restores_from_bak(self, tmp_path):
		cover = tmp_path / "cover.jpg"
		bak = tmp_path / "cover.jpg.bak"
		cover.write_bytes(b"current")
		bak.write_bytes(b"previous")
		assert restore_bak_cover(cover, bak) is True
		assert cover.read_bytes() == b"previous"
		assert bak.read_bytes() == b"previous"  # backup left intact

	def test_no_bak_returns_false(self, tmp_path):
		cover = tmp_path / "cover.jpg"
		cover.write_bytes(b"current")
		assert restore_bak_cover(cover, tmp_path / "cover.jpg.bak") is False
		assert cover.read_bytes() == b"current"  # untouched


class TestDeleteCovers:
	def test_deletes_only_existing(self, tmp_path):
		cover = tmp_path / "cover.jpg"
		bak = tmp_path / "cover.jpg.bak"
		cover.write_bytes(b"x")
		bak.write_bytes(b"y")
		assert delete_covers([cover]) == 1
		assert not cover.exists()
		assert bak.exists()
		assert delete_covers([bak]) == 1
		assert not bak.exists()

	def test_missing_files_count_zero(self, tmp_path):
		assert delete_covers([tmp_path / "cover.jpg", tmp_path / "cover.jpg.bak"]) == 0


class TestRenderReviewText:
	def test_round_trips_through_parser(self, tmp_path):
		entries = [
			{"id": 1, "uuid": "u1", "path": "a/b", "diagnosis": {"category": "C2", "reason": "r", "confidence": "HIGH"},
			 "current": {"author": "A", "title": "T"}, "proposed": None, "action": None},
			{"id": 2, "uuid": "u2", "path": "c/d", "diagnosis": {"category": "MISSING_ISBN", "reason": "r", "confidence": "LOW"},
			 "current": {"author": "X", "title": "Y"}, "proposed": {"isbn": "123"}, "action": "keep"},
		]
		out = tmp_path / "review.yaml"
		out.write_text(render_review_text(entries), encoding="utf-8")
		parsed = _load_raw_entries(out)
		assert len(parsed) == 2
		assert parsed[0]["action"] is None
		assert parsed[1]["action"] == "keep"
		assert parsed[1]["proposed"] == {"isbn": "123"}
		# Header is present and documents all actions.
		text = out.read_text(encoding="utf-8")
		assert text.startswith("# Auto-generated by book-meta-fix")
		assert "keep" in text

	def test_edited_null_round_trips(self, tmp_path):
		"""The ∅ mark survives the GUI save → bmf apply read cycle."""
		entries = [{
			"id": 1, "uuid": "u1", "path": "a/b",
			"diagnosis": {"category": "C2", "reason": "r", "confidence": "HIGH"},
			"current": {"author": "A", "title": "T"}, "proposed": {"series": None, "publisher": "správné"},
			"action": "accept",
		}]
		out = tmp_path / "review.yaml"
		out.write_text(render_review_text(entries), encoding="utf-8")
		parsed = _load_raw_entries(out)
		assert parsed[0]["proposed"] == {"series": None, "publisher": "správné"}
		assert parsed[0]["action"] == "accept"

	def test_legacy_edited_block_migrated_on_load(self, tmp_path):
		"""An old review.yaml (edited block, action: edit) is migrated on
		load — the GUI never sees the legacy keys."""
		legacy = ("---\nid: 1\nuuid: u1\npath: a/b\n"
			"current: {author: A, title: T}\nproposed: {series: wrong}\n"
			"edited: {series: Right, publisher: null}\naction: edit\n")
		out = tmp_path / "review.yaml"
		out.write_text(legacy, encoding="utf-8")
		parsed = _load_raw_entries(out)
		assert parsed[0]["proposed"] == {"series": "Right", "publisher": None}
		assert "edited" not in parsed[0]
		assert parsed[0]["action"] == "accept"

	def test_header_count_matches(self):
		text = render_review_text([{"id": 1, "path": "a", "current": {}, "action": None}])
		assert "# 1 books need review." in text


class TestEmbeddedCoverThumb:
	"""EPUB previews come from the zip probe; other formats extract via calibre."""

	def test_epub_thumb_from_zip_without_calibre(self, tmp_path, monkeypatch):
		# The EPUB path must NOT shell out to calibre: the zip probe is the
		# truth (ebook-meta would render page 1 even for a coverless book).
		def fail_extract(*_a, **_k):  # pragma: no cover - must not run
			raise AssertionError("extract_cover_from_book called for an EPUB")

		monkeypatch.setattr(gui, "extract_cover_from_book", fail_extract)
		img = embedded_cover_thumb(_make_epub(tmp_path / "book.epub"), 240, 320)
		assert img is not None
		assert img.size <= (240, 320)

	def test_epub_without_cover_returns_none(self, tmp_path):
		empty = tmp_path / "empty.epub"
		with zipfile.ZipFile(empty, "w") as zf:
			zf.writestr("mimetype", "application/epub+zip")
		assert embedded_cover_thumb(empty, 240, 320) is None

	def test_other_format_thumb_and_temp_cleanup(self, tmp_path, monkeypatch):
		# Fake calibre extraction: the "extracted" cover is a real JPEG on disk.
		def fake_extract(book_path, dest=None):
			assert Path(book_path).name == "book.mobi"
			out = tmp_path / "bmf-cover-fake.jpg" if dest is None else dest
			_make_jpeg(out)
			return out

		monkeypatch.setattr(gui, "extract_cover_from_book", fake_extract)
		img = embedded_cover_thumb(tmp_path / "book.mobi", 240, 320)
		assert img is not None
		assert img.size <= (240, 320)
		# The temp file must be removed — nothing may land in the library.
		leftovers = [p for p in tmp_path.iterdir() if p.name.startswith("bmf-cover-")]
		assert leftovers == []

	def test_returns_none_when_extraction_fails(self, tmp_path, monkeypatch):
		monkeypatch.setattr(gui, "extract_cover_from_book", lambda *_a, **_k: None)
		assert embedded_cover_thumb(tmp_path / "book.mobi") is None

	def test_returns_none_for_missing_file(self, tmp_path, monkeypatch):
		# calibre absent / unreadable book -> extract returns None -> None.
		monkeypatch.setattr(gui, "extract_cover_from_book", lambda *_a, **_k: None)
		assert embedded_cover_thumb(tmp_path / "nope.epub") is None


class TestOpenFolderInManager:
	"""open_folder_in_manager = platform-delegated, detached folder opening."""

	def test_missing_folder_reports_error(self, tmp_path):
		err = open_folder_in_manager(tmp_path / "neexistuje")
		assert err is not None
		assert "does not exist" in err

	def test_file_opens_its_parent(self, tmp_path, monkeypatch):
		import sys
		import types

		opened = []
		monkeypatch.setattr(gui, "subprocess", types.SimpleNamespace(
			Popen=lambda argv: opened.append(argv)))
		monkeypatch.setattr(gui, "shutil", types.SimpleNamespace(
			which=lambda name: f"/usr/bin/{name}"))
		book = tmp_path / "kniha.epub"
		book.write_bytes(b"x")
		assert open_folder_in_manager(book) is None
		expect = {"darwin": "open", "win32": "explorer"}.get(sys.platform, "xdg-open")
		assert opened[0][0] == expect
		assert opened[0][1] == str(tmp_path)

	def test_no_opener_available(self, tmp_path, monkeypatch):
		import types

		monkeypatch.setattr(gui, "shutil", types.SimpleNamespace(which=lambda _n: None))
		assert open_folder_in_manager(tmp_path) is not None

	def test_spawn_failure_reported(self, tmp_path, monkeypatch):
		import types

		def boom(_argv):
			raise OSError("spawn failed")

		monkeypatch.setattr(gui, "subprocess", types.SimpleNamespace(Popen=boom))
		monkeypatch.setattr(gui, "shutil", types.SimpleNamespace(which=lambda n: f"/usr/bin/{n}"))
		err = open_folder_in_manager(tmp_path)
		assert err is not None
		assert "failed" in err


class TestCollectVocabValues:
	def test_authors_and_series_from_library(self, tmp_path):
		import json
		for name, payload in {
			"A B (1)": {"authors": ["Jan Amos Komen", ""], "series": [{"name": "Světová próza", "sequence": 3}]},
			"C D (2)": {"authors": ["Karel Čapek"], "series": "Světová próza"},
			# ABS-native string form: the " #7" is the index — the pool
			# must suggest the BARE name.
			"E F (3)": {"authors": ["Jean-Pierre Garen"], "series": ["Mark Stone #7"]},
		}.items():
			d = tmp_path / name
			d.mkdir()
			(d / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")
		authors, series = collect_vocab_values(tmp_path)
		assert authors == ["Jan Amos Komen", "Jean-Pierre Garen", "Karel Čapek"]
		assert series == ["Mark Stone", "Světová próza"]  # all shapes collapse to bare names

	def test_unreadable_folders_skipped(self, tmp_path):
		for name, text in {"broken": "{not json", "empty": "{}"}.items():
			d = tmp_path / name
			d.mkdir()
			(d / "metadata.json").write_text(text, encoding="utf-8")
		assert collect_vocab_values(tmp_path) == ([], [])

	def test_missing_library_returns_empty(self, tmp_path):
		assert collect_vocab_values(tmp_path / "nope") == ([], [])


class TestExtractSeriesValues:
	"""Series tokens for the search haystack across the shapes on disk."""

	def test_container_with_flat_strings(self):
		# review.yaml shape: current/proposed carry `series` + `series_index`.
		tokens = extract_series_values({"series": "Legion", "series_index": "3"})
		assert "Legion" in tokens
		assert "3" in tokens
		assert "Legion 3" in tokens
		assert "Legion #3" in tokens

	def test_abs_list_shape(self):
		# metadata.json shape: [{"name", "index"}].
		tokens = extract_series_values({"series": [{"name": "Světová próza", "index": 2}]})
		assert "Světová próza" in tokens
		assert "Světová próza #2" in tokens

	def test_legacy_sequence_key(self):
		assert "Nadace" in extract_series_values({"series": [{"name": "Nadace", "sequence": 4}]})

	def test_plain_string_and_none(self):
		assert extract_series_values("Hrdinové z Ivory") == ["Hrdinové z Ivory"]
		assert extract_series_values(None) == []

	def test_index_only(self):
		assert "7" in extract_series_values({"series_index": 7})


class TestEntrySeriesLabel:
	"""The "Series #N" label the left list shows on the author's line."""

	def test_flat_pair_composes_with_hash(self):
		e = {"current": {"series": "Sága hadích válek", "series_index": "2"}}
		assert entry_series_label(e) == "Sága hadích válek #2"

	def test_name_only(self):
		assert entry_series_label({"current": {"series": "Legion"}}) == "Legion"

	def test_missing_series_is_empty(self):
		assert entry_series_label({"current": {"title": "x"}}) == ""
		assert entry_series_label({}) == ""
		assert entry_series_label(None) == ""

	def test_glued_index_split_like_c14(self):
		# "Mark Stone #73" with an empty index shows the SPLIT pair, not the
		# glued name twice.
		e = {"current": {"series": "Mark Stone #73"}}
		assert entry_series_label(e) == "Mark Stone #73"  # same text, split form
		e = {"current": {"series": "Mark Stone #73", "series_index": "73"}}
		assert entry_series_label(e) == "Mark Stone #73"

	def test_differing_index_kept_verbatim(self):
		# A stored index that disagrees with an embedded one is the deliberate
		# state C14 refuses to touch — show it as stored.
		e = {"current": {"series": "Mark Stone #73", "series_index": "5"}}
		assert entry_series_label(e) == "Mark Stone #73 #5"

	def test_index_only_shows_bare_order(self):
		assert entry_series_label({"current": {"series_index": 7}}) == "#7"

	def test_abs_shapes_tolerated(self):
		e = {"current": {"series": [{"name": "Světová próza", "index": 2}]}}
		assert entry_series_label(e) == "Světová próza #2"
		e = {"current": {"series": [{"name": "Nadace", "sequence": 4}]}}
		assert entry_series_label(e) == "Nadace #4"

	def test_index_normalized_decimal(self):
		# split_series_index normalizes "3,5" → "3.5"; a flat index passes as-is.
		e = {"current": {"series": "Perry Rhodan #3,5"}}
		assert entry_series_label(e) == "Perry Rhodan #3.5"

	def test_accepted_proposal_overlays_current(self):
		# An accept/keep decision applies `proposed` — the label shows the
		# series as apply will leave it (the C14 pre-filled accept scenario).
		e = {"action": "accept", "current": {"series": "Legion"},
		     "proposed": {"series": "Legion", "series_index": "3"}}
		assert entry_series_label(e) == "Legion #3"
		e = {"action": "keep", "current": {"series": "Mark Stone #73"},
		     "proposed": {"series": "Mark Stone", "series_index": "73"}}
		assert entry_series_label(e) == "Mark Stone #73"

	def test_accepted_null_deletes_the_series(self):
		e = {"action": "accept", "current": {"series": "Legion", "series_index": "1"},
		     "proposed": {"series": None, "series_index": None}}
		assert entry_series_label(e) == ""

	def test_pending_or_delete_show_current(self):
		# Undecided proposals are not applied yet; delete removes the book.
		e = {"current": {"series": "Legion"},
		     "proposed": {"series": "Legion", "series_index": "2"}}
		assert entry_series_label(e) == "Legion"
		e = {"action": "delete", "current": {"series": "Legion"},
		     "proposed": {"series": "X", "series_index": "9"}}
		assert entry_series_label(e) == "Legion"

	def test_accept_without_proposal_falls_back_to_current(self):
		# The auto-accept MISSING_* path carries an empty proposal.
		e = {"action": "accept", "current": {"series": "Ráma", "series_index": "1"},
		     "proposed": None}
		assert entry_series_label(e) == "Ráma #1"


class TestEntrySeriesPair:
	"""The (name, order) normalization behind the label and the sort key."""

	def test_flat_pair(self):
		assert entry_series_pair(
			{"current": {"series": "Sága hadích válek", "series_index": "2"}}
		) == ("Sága hadích válek", "2")

	def test_no_series_is_empty_pair(self):
		assert entry_series_pair({}) == ("", "")
		assert entry_series_pair(None) == ("", "")

	def test_glued_index_split_like_c14(self):
		# The glued C14 form sorts INSIDE its series block at the split
		# order — the pair comes out split even while the entry is pending.
		assert entry_series_pair({"current": {"series": "Mark Stone #73"}}) == ("Mark Stone", "73")

	def test_accepted_proposal_overlays_current(self):
		e = {"action": "accept", "current": {"series": "Legion"},
		     "proposed": {"series": "Legion", "series_index": "3"}}
		assert entry_series_pair(e) == ("Legion", "3")


class TestEntrySortKey:
	"""Left-panel display order: series (name, order in it), author, title."""

	def test_series_blocks_before_standalone_books(self):
		series_book = {"current": {"author": "Žeber", "title": "A",
		                           "series": "Mark Stone", "series_index": "73"}}
		standalone = {"current": {"author": "Adams", "title": "B"}}
		assert entry_sort_key(series_book) < entry_sort_key(standalone)

	def test_within_series_numeric_not_lexicographic(self):
		# "2" < "10" only when the order is compared as a NUMBER.
		def k(i):
			return entry_sort_key({"current": {"series": "Legion", "series_index": i}})

		assert k("1") < k("1.5") < k("2") < k("10")
		# A missing order closes its block: the numbered books come first.
		assert k("3") < k("")

	def test_blocks_ordered_by_series_name(self):
		def k(name):
			return entry_sort_key({"current": {"series": name, "series_index": "1"}})

		assert k("Legion") < k("Mark Stone") < k("Nadace")

	def test_standalone_by_author_then_title(self):
		def k(author, title):
			return entry_sort_key({"current": {"author": author, "title": title}})

		assert k("Adams", "Zulu") < k("Brown", "Alpha")
		assert k("Adams", "Alpha") < k("Adams", "Beta")

	def test_casefolded_strings(self):
		# A lowercase author sorts under its letter, not below "Z".
		def k(author):
			return entry_sort_key({"current": {"author": author, "title": "x"}})

		assert k("adams") < k("Brown")

	def test_glued_c14_sorts_into_its_series_at_split_order(self):
		glued = {"current": {"series": "Mark Stone #73"}}
		split = {"current": {"series": "Mark Stone", "series_index": "72"}}
		assert entry_sort_key(split) < entry_sort_key(glued)  # 72 before the glued 73
		before = {"current": {"series": "Mark Stone", "series_index": "74"}}
		assert entry_sort_key(glued) < entry_sort_key(before)

	def test_decision_aware_series_and_author(self):
		# An accepted proposal moves the book into the series/author block
		# the decision leaves it in — the row shows the projected values.
		decided = {"action": "accept", "current": {"author": "Špatný, Autorský"},
		           "proposed": {"author": "Správný, Autorský",
		                        "series": "Legion", "series_index": "1"}}
		pending = {"current": {"author": "A Autor", "title": "x",
		                       "series": "Legion", "series_index": "2"}}
		assert entry_sort_key(decided) < entry_sort_key(pending)  # Legion #1 before #2

	def test_title_sorts_by_current_value(self):
		# The title column shows CURRENT — the sort follows the display.
		e = {"current": {"title": "Buch"}, "proposed": {"title": "Alespoň"}}
		other = {"current": {"title": "Chata"}}
		assert entry_sort_key(e) < entry_sort_key(other)


class TestListDisplayOrder:
	"""_filtered_indices returns DISPLAY-sorted indices (series first), while
	self.entries keeps its storage order — the iids stay stable."""

	def _bare_app(self):
		import types

		app = gui.ReviewEditorApp.__new__(gui.ReviewEditorApp)
		app._filter_action = types.SimpleNamespace(get=lambda: "all")
		app._filter_category = types.SimpleNamespace(get=lambda: "all")
		app._search = types.SimpleNamespace(get=lambda: "")
		app._cur = -1
		app._fields = {}
		app._lib_uuids = {}
		return app

	def test_indices_sorted_series_first_author_title(self):
		app = self._bare_app()
		app.entries = [
			{"uuid": "a", "current": {"author": "Novák", "title": "Osamocená"}},
			{"uuid": "b", "current": {"author": "X", "title": "Náhradník",
			                          "series": "Legion", "series_index": "10"}},
			{"uuid": "c", "current": {"author": "Y", "title": "První",
			                          "series": "Legion", "series_index": "2"}},
			{"uuid": "d", "current": {"author": "Adams", "title": "Druhá"}},
		]
		# Legion block in numeric order (2 before 10), then standalone books
		# by author. The list, j/k stepping and the position counter all read
		# this one order.
		assert app._filtered_indices() == [2, 1, 3, 0]


class TestEntryMatchesSearch:
	"""The GUI search must reach series, not just author/title."""

	def test_series_name_case_insensitive(self):
		e = {"current": {"author": "Doris Egan", "title": "Neznámý"},
		     "proposed": {"series": "Hrdinové z Ivory"}}
		assert entry_matches_search("ivory", e)
		assert not entry_matches_search("Odyssea", e)

	def test_series_with_index_compound(self):
		e = {"current": {"series": "Sága hadích válek", "series_index": "2"}}
		assert entry_matches_search("hadích válek 2", e)
		assert entry_matches_search("sága #2", e)

	def test_abs_list_series_in_current(self):
		e = {"current": {"series": [{"name": "Space Odyssey", "index": 2}]}}
		assert entry_matches_search("odyssey", e)

	def test_unsaved_edits_in_active(self):
		# What the user typed into the Series field but has not yet switched
		# books / saved must still match (the active-field overlay).
		e = {"current": {"title": "x"}}
		assert entry_matches_search("legion", e, active={"series": "Legion", "series_index": "1"})

	def test_multi_term_needs_all_words(self):
		e = {"current": {"series": "Star Trek: Stargazer"}}
		assert entry_matches_search("star stargazer", e)
		assert not entry_matches_search("star picard", e)

	def test_empty_needle_matches_all(self):
		assert entry_matches_search("   ", {"current": {}})

	def test_extra_hay_extends_the_haystack(self):
		# The library scan passes the manifest description here — review
		# entries never carry it.
		e = {"current": {"author": "Jiří Pavlovský", "title": "Zatykač na Stonea"}}
		assert not entry_matches_search("mark stone", e)
		assert entry_matches_search(
			"mark stone", e,
			extra_hay="Rutinní výprava se zvrtne a Mark Stone se dostává do péče lékařů.")


class TestLibraryEntryChanged:
	"""Only a CHANGED library-loaded entry may enter review.yaml on save."""

	def test_untouched_entry_is_unchanged(self):
		e = {"current": {"author": "Mark Stone", "title": "Jupiter"},
		     "proposed": None, "action": None}
		assert library_entry_changed(e) is False

	def test_prefill_noise_is_not_a_change(self):
		# Browsing a book merges its prefilled values into `proposed`
		# (_collect_current) — equal-to-current values must not count.
		e = {"current": {"author": "Mark Stone", "title": "Jupiter"},
		     "proposed": {"author": "Mark Stone", "title": "Jupiter", "source": "current"},
		     "action": None}
		assert library_entry_changed(e) is False

	def test_differing_value_is_a_change(self):
		e = {"current": {"author": "Mark Stone", "title": "Jupiter"},
		     "proposed": {"author": "Mark Stone", "title": "Jupiter I"}, "action": None}
		assert library_entry_changed(e) is True

	def test_null_delete_is_a_change(self):
		e = {"current": {"series": "Mark Stone"}, "proposed": {"series": None},
		     "action": None}
		assert library_entry_changed(e) is True

	def test_new_field_is_a_change(self):
		# current has no genres (the review shape never carries them) — a
		# filled Genres field is a real addition.
		e = {"current": {"title": "X"}, "proposed": {"genres": ["sci-fi"]},
		     "action": None}
		assert library_entry_changed(e) is True

	def test_noise_keys_alone_are_not_a_change(self):
		e = {"current": {"title": "X"},
		     "proposed": {"source": "embedded", "reasoning": "…",
		                  "location": "Autor/Nadace (1)", "cover_url": "http://x"},
		     "action": None}
		assert library_entry_changed(e) is False

	def test_decision_channels_are_changes(self):
		base = {"current": {"title": "X"}, "proposed": None}
		assert library_entry_changed({**base, "action": "accept"}) is True
		assert library_entry_changed({**base, "verified": True}) is True
		assert library_entry_changed({**base, "notes": "zkontrolovat"}) is True


class TestEntriesToWrite:
	def test_review_entries_always_written(self):
		e = {"uuid": "u1", "action": None, "current": {}}
		assert entries_to_write([e], set()) == [e]

	def test_unchanged_library_entries_dropped(self):
		review = {"uuid": "u1", "action": None, "current": {}}
		lib = {"uuid": "u2", "action": None, "current": {"title": "T"}, "proposed": None}
		assert entries_to_write([review, lib], {"u2"}) == [review]

	def test_changed_library_entries_kept(self):
		lib = {"uuid": "u2", "action": "accept", "current": {"title": "T"},
		       "proposed": None}
		assert entries_to_write([lib], {"u2"}) == [lib]

	def test_legacy_uuidless_review_entry_survives(self):
		# uuid: null cannot match lib_uuids (which only holds minted strings).
		e = {"uuid": None, "action": None, "current": {}}
		assert entries_to_write([e], {"u2"}) == [e]


class TestLibraryEntryFromMeta:
	def test_shape_matches_review_entry(self):
		from book_meta_fix.models import BookMeta

		meta = BookMeta(path="/lib/Mark Stone/Jupiter (5)", calibre_id=5, uuid="abc",
		                 authors=["Mark Stone"], title="Jupiter",
		                 series=[{"name": "Mark Stone", "index": 3}])
		e = library_entry_from_meta(meta, "/lib")
		assert e == {
			"id": 5, "uuid": "abc", "path": "Mark Stone/Jupiter (5)",
			"current": {"author": "Mark Stone", "title": "Jupiter",
			            "series": "Mark Stone", "series_index": "3"},
			"proposed": None, "action": None,
		}
		assert "diagnosis" not in e  # no rule flagged it — no fake diagnosis

	def test_absolute_path_fallback(self):
		from book_meta_fix.models import BookMeta

		meta = BookMeta(path="/elsewhere/kniha", uuid="u")
		assert library_entry_from_meta(meta, "/lib")["path"] == "/elsewhere/kniha"

	def test_broken_series_gets_split_prefilled(self):
		"""'Mark Stone #73' (index empty) → proposed: bare name + order.

		The GUI pre-fills exactly what the C14 detector/analyze would
		propose (the shared split_series_index), so the user accepts instead
		of retyping. Action stays pending — bulk pre-accept is analyze's.
		"""
		from book_meta_fix.models import BookMeta

		meta = BookMeta(path="/lib/X/Kniha (9)", uuid="u", title="Kniha",
		                series=[{"name": "Mark Stone #73", "index": ""}])
		e = library_entry_from_meta(meta, "/lib")
		assert e["current"]["series"] == "Mark Stone #73"
		assert e["proposed"] == {"series": "Mark Stone", "series_index": "73"}
		assert e["action"] is None

	def test_healthy_series_gets_no_prefill(self):
		from book_meta_fix.models import BookMeta

		meta = BookMeta(path="/lib/X/Kniha (9)", uuid="u", title="Kniha",
		                series=[{"name": "Nadace", "index": "3"}])
		assert library_entry_from_meta(meta, "/lib")["proposed"] is None


class TestLibraryIndex:
	"""The '+ library' fulltext index: one sweep at GUI startup, instant
	multi-word searches over the in-memory haystacks."""

	@staticmethod
	def _book(library, rel, payload):
		import json as _json

		d = library / rel
		d.mkdir(parents=True)
		(d / "metadata.json").write_text(_json.dumps(payload), encoding="utf-8")
		return d

	@staticmethod
	def _search(index, needle, skip_paths=None, skip_uuids=None):
		return search_library_index(index, needle, skip_paths or set(), skip_uuids or set())

	def test_matches_by_series_and_path(self, tmp_path):
		# Series match: metadata carries the series the user searches for.
		self._book(tmp_path, "Mark Stone/Jupiter (5)",
		           {"authors": ["Mark Stone"], "title": "Jupiter",
		            "series": [{"name": "Mark Stone", "index": 5}]})
		# Path match: broken metadata (no author/title) but the FOLDER is
		# named after the series — the haystack includes the path.
		self._book(tmp_path, "Mark Stone/nepojmenovana (6)",
		           {"title": ""})
		# No match at all.
		self._book(tmp_path, "Jiné/Svět (7)",
		           {"authors": ["Karel Čapek"], "title": "Svět"})
		index = build_library_index(tmp_path)
		found = self._search(index, "mark stone")
		assert {e["path"] for e, _h in found} == {
			"Mark Stone/Jupiter (5)", "Mark Stone/nepojmenovana (6)"}

	def test_second_series_of_multi_series_book(self, tmp_path):
		# `current` (the entry half of the haystack) carries only the FIRST
		# series — the manifest-only half must append the REST, or the second
		# series of a multi-series book is unsearchable. Mixed stored shapes:
		# the ABS-native glued string and the legacy `sequence` dict.
		self._book(tmp_path, "A/Kniha (1)",
		           {"authors": ["Autor"], "title": "Kniha",
		            "series": ["Zaklínač #8", {"name": "Lasst", "sequence": 3}]})
		index = build_library_index(tmp_path)
		for needle in ("zaklínač", "zaklínač 8", "lasst", "lasst 3"):
			assert [e["path"] for e, _h in self._search(index, needle)] == ["A/Kniha (1)"], needle

	def test_multi_word_query_needs_all_words(self, tmp_path):
		self._book(tmp_path, "A/T (1)", {"authors": ["Mark"], "title": "X"})
		index = build_library_index(tmp_path)
		assert self._search(index, "mark stone") == []

	def test_known_paths_and_uuids_skipped(self, tmp_path):
		self._book(tmp_path, "A/Book (1)", {"authors": ["Mark Stone"], "title": "T", "uuid": "u1"})
		self._book(tmp_path, "B/Book (2)", {"authors": ["Mark Stone"], "title": "T", "uuid": "u2"})
		index = build_library_index(tmp_path)
		assert self._search(index, "mark stone", {"B/Book (2)"}, {"u1"}) == []

	def test_empty_needle_returns_empty(self, tmp_path):
		self._book(tmp_path, "A/T (1)", {"authors": ["Mark Stone"], "title": "X"})
		index = build_library_index(tmp_path)
		assert self._search(index, "   ") == []

	def test_uuid_minted_and_persisted_at_index_time(self, tmp_path):
		import json

		self._book(tmp_path, "A/T (1)", {"authors": ["Mark Stone"], "title": "X"})
		index = build_library_index(tmp_path)
		assert len(index) == 1
		assert index[0][0]["uuid"]
		# The mint is persisted (the review workflow is uuid-keyed) — the
		# same lazy identity augmentation a cache-miss scan performs.
		on_disk = json.loads((tmp_path / "A/T (1)/metadata.json").read_text(encoding="utf-8"))
		assert on_disk["uuid"] == index[0][0]["uuid"]

	def test_result_sorted_by_author_title(self, tmp_path):
		self._book(tmp_path, "B/aaa (1)", {"authors": ["Zed Stone"], "title": "aaa"})
		self._book(tmp_path, "A/zzz (2)", {"authors": ["Abe Stone"], "title": "zzz"})
		index = build_library_index(tmp_path)
		found = self._search(index, "stone")
		assert [e["current"]["author"] for e, _h in found] == ["Abe Stone", "Zed Stone"]

	def test_entry_round_trips_through_review_parser(self, tmp_path):
		"""A served entry saved into review.yaml parses back — `bmf apply`
		must be able to process it (no diagnosis → {} is fine)."""
		from book_meta_fix.review import parse_review

		self._book(tmp_path, "A/T (1)", {"authors": ["Mark Stone"], "title": "X"})
		index = build_library_index(tmp_path)
		found = [e for e, _h in self._search(index, "mark stone")]
		found[0]["action"] = "accept"
		found[0]["proposed"] = {"title": "Y"}
		out = tmp_path / "review.yaml"
		out.write_text(render_review_text(found), encoding="utf-8")
		items = parse_review(out)
		assert len(items) == 1
		assert items[0].action == "accept"
		assert items[0].diagnosis == {}
		assert items[0].proposed == {"title": "Y"}
		assert items[0].path == "A/T (1)"

	def test_matches_by_description_only(self, tmp_path):
		# A series book whose `series` is empty and whose author folder is
		# the real writer (a house-pseudonym series like Mark Stone): the
		# series name lives ONLY in the annotation — the index haystack must
		# carry the manifest-only fields.
		self._book(tmp_path, "Jiří Pavlovský/Zatykač na Stonea (3222)",
		           {"authors": ["Jiří Pavlovský"], "title": "Zatykač na Stonea",
		            "series": [],
		            "description": "Rutinní výprava se zvrtne a Mark Stone se dostává do péče lékařů."})
		index = build_library_index(tmp_path)
		found = self._search(index, "mark stone")
		assert [e["path"] for e, _h in found] == ["Jiří Pavlovský/Zatykač na Stonea (3222)"]

	def test_top_level_book_folder_found(self, tmp_path):
		# The parallel walk splits by top-level directory — a book folder
		# sitting DIRECTLY in the library root must survive it.
		self._book(tmp_path, "Samotná kniha (9)", {"authors": ["Mark Stone"], "title": "X"})
		index = build_library_index(tmp_path)
		found = self._search(index, "mark stone")
		assert [e["path"] for e, _h in found] == ["Samotná kniha (9)"]

	def test_progress_callback_reports_totals(self, tmp_path):
		self._book(tmp_path, "A/T (1)", {"authors": ["Mark Stone"], "title": "X"})
		calls = []
		index = build_library_index(tmp_path, progress=lambda done, total: calls.append((done, total)))
		assert index
		assert calls and calls[-1] == (1, 1)  # the final call reports totals

	def test_cache_serves_hits_and_reads_only_misses(self, tmp_path, monkeypatch):
		"""With a Cache the sweep serves unchanged folders from SQLite and
		reads only misses — the GUI start sweep used to re-read every
		metadata.json over NFS on each launch."""
		import book_meta_fix.gui as gui_mod
		from book_meta_fix.library import Cache

		self._book(tmp_path, "A/Cached (1)", {"authors": ["Mark Stone"], "title": "X"})
		cache = Cache(tmp_path / "cache.db")
		build_library_index(tmp_path, cache=cache)
		cache.commit()

		self._book(tmp_path, "B/New (2)", {"authors": ["Mark Stone"], "title": "Y"})
		orig = gui_mod.read_book_folder
		read: list[str] = []

		def _spy(folder):
			read.append(str(folder))
			return orig(folder)

		monkeypatch.setattr(gui_mod, "read_book_folder", _spy)
		index = build_library_index(tmp_path, cache=cache)
		assert sorted(e["path"] for e, _h in index) == ["A/Cached (1)", "B/New (2)"]
		# Only the uncached folder paid a disk read.
		assert [str(p) for p in read] == [str(tmp_path / "B" / "New (2)")]
		# The miss was put back into the cache: a third sweep reads nothing.
		read.clear()
		index2 = build_library_index(tmp_path, cache=cache)
		assert read == []
		assert sorted(e["path"] for e, _h in index2) == ["A/Cached (1)", "B/New (2)"]
		cache.close()

	def test_missing_library_returns_empty(self, tmp_path):
		assert build_library_index(tmp_path / "nope") == []

	def test_served_entries_are_copies(self, tmp_path):
		"""The GUI mutates served entries (action/proposed); the index must
		stay pristine so a later search serves the original values."""
		self._book(tmp_path, "A/T (1)", {"authors": ["Mark Stone"], "title": "X"})
		index = build_library_index(tmp_path)
		served = self._search(index, "mark stone")
		served[0][0]["action"] = "accept"
		again = self._search(index, "mark stone")
		assert again[0][0]["action"] is None  # index untouched

	def test_series_split_prefill_in_index(self, tmp_path):
		# The C14 prefill rides on the indexed entry (proposed differs from
		# current) — and the prefill alone must NOT count as a change. The
		# glued name must be in DICT form: a plain "Mark Stone #73" string is
		# the ABS-native form that series_entry_pair already splits at read
		# time, so there is nothing to prefill.
		self._book(tmp_path, "A/T (1)", {"authors": ["Mark Stone"], "title": "X",
		                                  "series": [{"name": "Mark Stone #73", "index": ""}]})
		index = build_library_index(tmp_path)
		entry = index[0][0]
		assert entry["proposed"] == {"series": "Mark Stone", "series_index": "73"}
		assert library_entry_changed(entry) is False

	def test_abs_native_series_string_needs_no_prefill(self, tmp_path):
		self._book(tmp_path, "A/T (1)", {"authors": ["Mark Stone"], "title": "X",
		                                  "series": ["Mark Stone #73"]})
		index = build_library_index(tmp_path)
		assert index[0][0]["proposed"] is None


class TestTabTrapShiftTab:
	"""Regression: X11 delivers a real Shift+Tab as keysym ISO_Left_Tab.

	The trap must bind that keysym too (a <Shift-Tab>-only binding never
	fires on Linux) and _on_tab must derive the direction from it —
	otherwise Shift-Tab falls through to Tk's default traversal, which
	visits widgets in CREATION order (➡ of the row, then the previous
	row's ∅) instead of the previous field entry.
	"""

	def _bare_app(self):
		return gui.ReviewEditorApp.__new__(gui.ReviewEditorApp)

	def test_trap_binds_x11_shift_tab_keysym(self):
		import types
		app = self._bare_app()
		bound = {}
		app.root = types.SimpleNamespace(
			bind_class=lambda tag, seq, fn: bound.__setitem__(seq, fn))
		app._trap_subtree = lambda _parent: None
		app._install_tab_trap()
		assert set(bound) == {"<Tab>", "<Shift-Tab>", "<ISO_Left_Tab>"}
		assert all(fn == app._on_tab for fn in bound.values())

	def test_on_tab_direction(self):
		import types
		app = self._bare_app()
		w = object()
		app._editable_widgets = [w]
		app._acs = {}
		app.focus_get_safe = lambda: w
		calls = []
		app._cycle_editable = lambda *, forward: calls.append(forward)
		app._on_tab(types.SimpleNamespace(state=0, keysym="ISO_Left_Tab"))
		app._on_tab(types.SimpleNamespace(state=0, keysym="Tab"))
		app._on_tab(types.SimpleNamespace(state=1, keysym="Tab"))  # Windows <Shift-Tab>
		assert calls == [False, True, False]

	def test_on_tab_falls_through_off_fields(self):
		import types
		app = self._bare_app()
		app._editable_widgets = [object()]

		def _boom(*, forward):
			raise AssertionError("must not cycle from a non-field widget")

		app._acs = {}
		app.focus_get_safe = lambda: "a button"
		app._cycle_editable = _boom
		assert app._on_tab(types.SimpleNamespace(state=0, keysym="Tab")) is None


class TestTabAutocompleteGate:
	"""Tab must not silently accept the FIRST autocomplete suggestion.

	Real complaint: editing the series, tabbing to the next field rewrote
	the value with whatever the pool happened to start with. Tab accepts
	only after a deliberate arrow pick; an untouched popup just closes and
	the typed text stays. Enter / a click remain the explicit confirmation.
	"""

	def _bare_app(self):
		return gui.ReviewEditorApp.__new__(gui.ReviewEditorApp)

	@staticmethod
	def _fake_ac(*, is_open: bool, picked: bool):
		import types

		calls = []
		return types.SimpleNamespace(
			is_open=is_open, has_user_pick=picked,
			accept=lambda: calls.append("accept"),
			hide=lambda: calls.append("hide"),
		), calls

	def test_tab_closes_unpicked_popup_without_accepting(self):
		import types

		app = self._bare_app()
		w = object()
		app._editable_widgets = [w]
		ac, calls = self._fake_ac(is_open=True, picked=False)
		app._acs = {w: ac}
		app.focus_get_safe = lambda: w
		app._cycle_editable = lambda *, forward: None
		assert app._on_tab(types.SimpleNamespace(state=0, keysym="Tab")) == "break"
		assert calls == ["hide"]  # the typed value survives

	def test_tab_accepts_after_arrow_pick(self):
		import types

		app = self._bare_app()
		w = object()
		app._editable_widgets = [w]
		ac, calls = self._fake_ac(is_open=True, picked=True)
		app._acs = {w: ac}
		app.focus_get_safe = lambda: w
		app._cycle_editable = lambda *, forward: None
		app._on_tab(types.SimpleNamespace(state=0, keysym="Tab"))
		assert calls == ["accept"]

	def test_tab_ignores_closed_popup(self):
		import types

		app = self._bare_app()
		w = object()
		app._editable_widgets = [w]
		ac, calls = self._fake_ac(is_open=False, picked=True)
		app._acs = {w: ac}
		app.focus_get_safe = lambda: w
		app._cycle_editable = lambda *, forward: None
		app._on_tab(types.SimpleNamespace(state=0, keysym="Tab"))
		assert calls == []


class TestLibHaystackFilterExemption:
	"""The list filter exempts library entries via their INDEX haystack (the
	entry itself carries no description) — and hides a stale superset match
	when the needle narrows past what that book actually matches."""

	def _bare_app(self):
		import types

		app = gui.ReviewEditorApp.__new__(gui.ReviewEditorApp)
		app._filter_action = types.SimpleNamespace(get=lambda: "all")
		app._filter_category = types.SimpleNamespace(get=lambda: "all")
		app._search = types.SimpleNamespace(get=lambda: "mark stone")
		app._cur = -1
		app._fields = {}
		app._lib_uuids = {}
		return app

	def test_matching_lib_entry_visible_superset_hidden(self):
		app = self._bare_app()
		app.entries = [
			{"uuid": "r1", "path": "X/Y (1)", "current": {"title": "mark stone review"},
			 "action": None},
			{"uuid": "u6", "path": "A/B (2)", "current": {"title": "nevyjadřuje se"},
			 "action": None},
			{"uuid": "u9", "path": "C/D (3)", "current": {"title": "take nic"},
			 "action": None},
		]
		# u6: description-only match (both words); u9: matched the settled
		# partial needle "mark" only — hidden once the needle narrows.
		app._lib_uuids = {
			"u6": "a/b (2) nevyjadřuje se … anotace: mark stone se dostává …",
			"u9": "c/d (3) take nic … jen mark, nic víc …",
		}
		assert app._filtered_indices() == [0, 1]

	def test_lib_entry_without_haystack_falls_back_to_entry_search(self):
		app = self._bare_app()
		app.entries = [
			{"uuid": "u6", "path": "A/B (2)", "current": {"title": "mark stone v titulku"},
			 "action": None},
		]
		assert app._filtered_indices() == [0]  # not in _lib_uuids → normal path


def _tk_root():
	"""A real Tk root for widget-level smoke tests, or a skip.

	These tests exist to catch ttk option misuse and format-string breaks
	that only surface when Tk actually builds the widget.
	"""
	if gui.ttk is None:
		pytest.skip("tkinter unavailable")
	try:
		return gui.tk.Tk()
	except Exception:  # noqa: BLE001
		pytest.skip("no display")


class TestTooltipShow:
	"""_show must build its tip with ttk-valid options.

	Regression: the ttk alignment left ``padx``/``pady`` on the ttk.Label,
	so every hover raised ``TclError: unknown option "-padx"``.
	"""

	def test_show_creates_tip_without_tcl_error(self):
		root = _tk_root()
		try:
			lbl = gui.ttk.Label(root, text="x")
			tip = gui._Tooltip(lbl, "hover text")
			tip._show()
			assert tip._tip is not None
			tip._hide()
			assert tip._tip is None
		finally:
			root.destroy()


class TestApplyFmtCovers:
	"""Painting the per-format embedded-cover row must not raise for any format.

	Regression: the non-EPUB tooltip called ``.format(text=…)`` against the
	``{ext}`` msgid placeholder, so every MOBI/AZW3/PRC cover cell raised
	``KeyError: 'ext'`` and the row died mid-loop.
	"""

	def _bare_app(self, root):
		app = gui.ReviewEditorApp.__new__(gui.ReviewEditorApp)
		app.root = root
		app._alive = True
		app._cur = 0
		app._field_bg = "#ffffff"
		app._cover_photos = {}
		app._del_formats = {}
		app._fmt_cover_row = gui.ttk.Frame(root)
		return app

	def test_non_epub_cell_paints_without_keyerror(self):
		from PIL import Image

		root = _tk_root()
		try:
			app = self._bare_app(root)
			app._apply_fmt_covers(0, [(Path("/lib/A/B (1)/book.mobi"), Image.new("RGB", (60, 80)))])
			# The cell was built (checkbox present but disabled → var tracked, unchecked).
			assert app._fmt_cover_row.winfo_children()
			assert app._del_formats[str(Path("/lib/A/B (1)/book.mobi"))].get() is False
		finally:
			root.destroy()

	def test_epub_cell_registers_strip_var(self):
		from PIL import Image

		root = _tk_root()
		try:
			app = self._bare_app(root)
			app._apply_fmt_covers(0, [(Path("/lib/A/B (1)/book.epub"), Image.new("RGB", (60, 80)))])
			assert list(app._del_formats) == [str(Path("/lib/A/B (1)/book.epub"))]
		finally:
			root.destroy()

	def test_empty_row_shows_placeholder(self):
		root = _tk_root()
		try:
			app = self._bare_app(root)
			app._apply_fmt_covers(0, [])
			assert app._del_formats == {}
		finally:
			root.destroy()


class TestOnWheelStringWidget:
	"""The wheel router must survive a wrapper-less event target.

	Regression: scrolling over an open ttk.Combobox popdown (the Action/
	Category filter) delivers event.widget as the raw pathname STRING —
	tkinter's _substitute falls back to %W when _nametowidget misses the
	Tcl-internal popdown — so _on_wheel raised AttributeError on every
	wheel tick over the dropdown.
	"""

	def test_string_widget_returns_none(self):
		root = _tk_root()
		try:
			app = gui.ReviewEditorApp.__new__(gui.ReviewEditorApp)
			app.root = root
			app.canvas = gui.tk.Canvas(root)
			app.tree = object()  # only identity-compared, never reached

			class _Ev:
				num = 4  # X11 wheel-up
				delta = 0
				widget = ".!combobox.popdown.f.l"

			assert app._on_wheel(_Ev()) is None
		finally:
			root.destroy()


class TestDialogConfirmParent:
	"""A confirm fired from inside a grabbed Toplevel must be its CHILD.

	Regression: the merge and bulk-cover-delete confirmations called
	``messagebox.askyesno`` without ``parent=``, so the box attached to the
	default ROOT while the grabbed dialog stacked above it — the dialog looked
	hung until the hidden box was found and dismissed.
	"""

	def _bare_app(self, root, library, entries, selection):
		import types

		app = gui.ReviewEditorApp.__new__(gui.ReviewEditorApp)
		app.root = root
		app.entries = entries
		app.library = library
		app.tree = types.SimpleNamespace(focus=lambda: str(selection[0]))
		app._bulk_selection_indices = lambda: list(selection)
		app._collect_current = lambda: None
		self.wins = []
		app._modal_over_main = lambda win, focus=None: self.wins.append(win)
		return app

	def _confirm_button(self, win):
		# The LAST packed frame is the button row; its first child is the
		# confirm button ("Merge" / "Delete").
		frames = [w for w in win.winfo_children() if isinstance(w, gui.ttk.Frame)]
		return frames[-1].winfo_children()[0]

	def test_merge_confirm_parented_to_dialog(self, tmp_path, monkeypatch):
		root = _tk_root()
		try:
			app = self._bare_app(root, tmp_path, [
				{"uuid": "a", "path": "A/Kniha (1)", "current": {"title": "Kniha"}, "action": None},
				{"uuid": "b", "path": "A/Kniha (2)", "current": {"title": "Kniha"}, "action": None},
			], selection=(0, 1))
			captured = {}

			def _ask(_title, _message, **kw):
				captured.update(kw)
				return False  # decline → execute_merge never runs

			monkeypatch.setattr(gui.messagebox, "askyesno", _ask)
			app.merge_selected()
			assert len(self.wins) == 1
			self._confirm_button(self.wins[0]).invoke()
			assert captured.get("parent") is self.wins[0]
		finally:
			root.destroy()

	def test_bulk_cover_delete_confirm_parented_to_dialog(self, tmp_path, monkeypatch):
		root = _tk_root()
		try:
			folder = tmp_path / "A" / "Kniha (1)"
			folder.mkdir(parents=True)
			(folder / "kniha.epub").write_bytes(b"x")  # an ebook → the strip confirm fires
			app = self._bare_app(root, tmp_path, [
				{"uuid": "a", "path": "A/Kniha (1)", "current": {"title": "Kniha"}, "action": None},
			], selection=(0,))
			captured = {}

			def _ask(_title, _message, **kw):
				captured.update(kw)
				return False

			monkeypatch.setattr(gui.messagebox, "askyesno", _ask)
			app.bulk_delete_covers()
			assert len(self.wins) == 1
			win = self.wins[0]
			checkboxes = [w for w in win.winfo_children() if isinstance(w, gui.ttk.Checkbutton)]
			checkboxes[2].invoke()  # "covers embedded in the ebook files (EPUB only)"
			self._confirm_button(win).invoke()
			assert captured.get("parent") is win
		finally:
			root.destroy()


class TestBulkClearAndRemove:
	"""Ctrl+Shift+D (mass veto → pending) and Ctrl+Shift+R (drop from review)."""

	def _bare_app(self, selection=("0",)):
		import types

		app = gui.ReviewEditorApp.__new__(gui.ReviewEditorApp)
		app.entries = [
			{"uuid": "a", "current": {"title": "Alpha"}, "action": "delete",
			 "proposed": {"delete_files": ["a.epub"]}},
			{"uuid": "b", "current": {"title": "Beta"}, "action": "keep"},
		]
		app.tree = types.SimpleNamespace(
			selection_get=lambda: selection,
			selection_set=lambda *a, **kw: None,
			see=lambda *a: None,
		)
		app._cur = 0
		app._loading = False
		app._dirty = False
		app.calls = []
		app._set_status = lambda extra="": None
		app._collect_current = lambda: app.calls.append("collect")
		app._load_book = lambda idx: app.calls.append(("load", idx))
		app.refresh_list = lambda: app.calls.append("refresh")
		app.flashes = []
		app._flash = lambda msg, seconds=None: app.flashes.append(msg)
		app._mark_dirty = lambda: setattr(app, "_dirty", True)
		app._do_save = lambda: app.calls.append("save") or True
		app._lib_uuids = {"a": "hay-a"}
		app._thumbs_pil = {}
		app._thumbs_photo = {}
		app._big_thumbs = {}
		app._lib_index = [({"uuid": "a"}, "hay-a")]
		return app

	def test_bulk_clear_returns_delete_to_pending(self):
		app = self._bare_app(("0", "1"))
		app.bulk_clear_action()
		assert [e["action"] for e in app.entries] == [None, None]
		assert app.flashes == ["bulk decision cleared: 2 books"]
		assert app._dirty is True

	def test_bulk_clear_without_selection_flashes(self):
		app = self._bare_app(())
		app.bulk_clear_action()
		assert app.entries[0]["action"] == "delete"
		assert app.flashes == ["select books first (Ctrl+click, Shift+click)"]

	def test_remove_from_review_drops_entries_and_saves(self, monkeypatch):
		monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **kw: True)
		app = self._bare_app(("0",))
		app.remove_from_review()
		assert [e["uuid"] for e in app.entries] == ["b"]  # only the selected gone
		assert "save" in app.calls  # saved IMMEDIATELY, not at the next Ctrl+S
		assert "a" not in app._lib_uuids  # the library index cannot re-serve it
		assert app._lib_index == []
		assert app.flashes == ["removed 1 entries from review"]

	def test_remove_from_review_cancelled_touches_nothing(self, monkeypatch):
		monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **kw: False)
		app = self._bare_app(("0",))
		app.remove_from_review()
		assert len(app.entries) == 2
		assert "save" not in app.calls

	def test_remove_last_entry_leaves_empty_review(self, monkeypatch):
		monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **kw: True)
		app = self._bare_app(("0", "1"))
		app.remove_from_review()
		assert app.entries == []
		assert app.flashes == ["review is empty"]
		assert "save" in app.calls

	def test_shift_plus_d_and_r_dispatch(self):
		import types

		app = gui.ReviewEditorApp.__new__(gui.ReviewEditorApp)
		app.root = types.SimpleNamespace(grab_current=lambda: None)
		app.calls = []
		for name in ("bulk_clear_action", "remove_from_review"):
			setattr(app, name, (lambda n: lambda: app.calls.append(n))(name))
		assert app._on_ctrl_key(type("E", (), {"keysym": "D", "state": 0x0001})()) == "break"
		assert app._on_ctrl_key(type("E", (), {"keysym": "R", "state": 0x0001})()) == "break"
		assert app.calls == ["bulk_clear_action", "remove_from_review"]
