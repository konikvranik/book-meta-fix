"""Unit tests for the pure helpers in book_meta_fix.gui.

These exercise the no-Tk, no-network logic (field composition, cover path &
file handling, review.yaml round-trip rendering). The Tkinter UI itself is not
tested headlessly — it is kept thin and delegates to these helpers.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import book_meta_fix.gui as gui
from book_meta_fix.gui import (
	action_value,
	build_library_index,
	collect_vocab_values,
	compose_overlay,
	cover_paths,
	delete_covers,
	embedded_cover_thumb,
	entries_to_write,
	entry_matches_search,
	entry_series_label,
	extract_series_values,
	library_entry_changed,
	library_entry_from_meta,
	list_format_files,
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
		}.items():
			d = tmp_path / name
			d.mkdir()
			(d / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")
		authors, series = collect_vocab_values(tmp_path)
		assert authors == ["Jan Amos Komen", "Karel Čapek"]
		assert series == ["Světová próza"]  # dict + plain-string shapes collapse

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
		# current) — and the prefill alone must NOT count as a change.
		self._book(tmp_path, "A/T (1)", {"authors": ["Mark Stone"], "title": "X",
		                                  "series": ["Mark Stone #73"]})
		index = build_library_index(tmp_path)
		entry = index[0][0]
		assert entry["proposed"] == {"series": "Mark Stone", "series_index": "73"}
		assert library_entry_changed(entry) is False


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
