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
	compose_edited,
	cover_paths,
	delete_covers,
	embedded_cover_thumb,
	list_format_files,
	open_folder_in_manager,
	render_review_text,
	restore_bak_cover,
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


class TestComposeEdited:
	def test_only_included_fields_returned(self):
		sel = {"author": (True, "X"), "title": (False, "Y"), "isbn": (True, "80-1-2")}
		assert compose_edited(sel) == {"author": "X", "isbn": "80-1-2"}

	def test_year_coerced_to_int(self):
		assert compose_edited({"year": (True, "1989")}) == {"year": 1989}

	def test_year_non_numeric_kept_as_string(self):
		assert compose_edited({"year": (True, "cca 1989")}) == {"year": "cca 1989"}

	def test_list_fields_split_on_comma(self):
		assert compose_edited({"authors": (True, " A , B ,C ")}) == {"authors": ["A", "B", "C"]}
		assert compose_edited({"genres": (True, "sci-fi, fantasy")}) == {"genres": ["sci-fi", "fantasy"]}

	def test_none_when_nothing_included(self):
		assert compose_edited({"author": (False, "X")}) is None
		assert compose_edited({}) is None

	def test_series_fields_pass_through_as_strings(self):
		"""Série / Pořadí are plain string fields (no comma split, no int
		coercion) — apply packs them into meta.series as {"name","index"}."""
		sel = {"series": (True, "Nadace"), "series_index": (True, "3"), "title": (False, "X")}
		assert compose_edited(sel) == {"series": "Nadace", "series_index": "3"}

	def test_field_specs_cover_all_apply_fields(self):
		"""Every field _apply_action understands from ``edited`` must be
		editable in the GUI, otherwise the user cannot reach it by hand."""
		editable = {role for role, _label in gui.FIELD_SPECS}
		assert {"author", "title", "isbn", "year", "publisher", "language",
				"series", "series_index", "authors", "genres"} <= editable


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
			 "current": {"author": "X", "title": "Y"}, "proposed": {"isbn": "123"}, "action": "keep", "edited": {"author": "Z"}},
		]
		out = tmp_path / "review.yaml"
		out.write_text(render_review_text(entries), encoding="utf-8")
		parsed = _load_raw_entries(out)
		assert len(parsed) == 2
		assert parsed[0]["action"] is None
		assert parsed[1]["action"] == "keep"
		assert parsed[1]["edited"] == {"author": "Z"}
		# Header is present and documents all actions.
		text = out.read_text(encoding="utf-8")
		assert text.startswith("# Auto-generated by book-meta-fix")
		assert "keep" in text

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
		assert "neexistuje" in err

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
		assert "selhalo" in err or "selhal" in err
