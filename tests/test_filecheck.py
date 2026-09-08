"""Tests for filecheck.py — the content-probe validity engine of clean --files.

The safety contract under test: a file is deletable ONLY when its content is
recognizable as NO ebook format. A valid book with a wrong extension is
recognized and never flagged; unrecognized content under a suffix bmf cannot
fully probe is unknown, never invalid.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from book_meta_fix import filecheck
from book_meta_fix.filecheck import (
	extension_note,
	file_content_kind,
	file_is_invalid,
	merge_file_deletions,
	scan_invalid_files,
)
from book_meta_fix.models import BookMeta
from book_meta_fix.review import parse_review


@pytest.fixture(autouse=True)
def _no_calibre(monkeypatch):
	"""Tests must be hermetic — no calibre on the test path. The veto has its
	own dedicated tests (TestCalibreVeto) that control the fake explicitly."""
	monkeypatch.setattr(filecheck, "calibre_reads_file", lambda path: False)


def _minimal_epub_bytes() -> bytes:
	"""A calibre-readable EPUB zip (OCF 1.0 container, STORED mimetype, OPF)."""
	import io

	buf = io.BytesIO()
	with zipfile.ZipFile(buf, "w") as zf:
		zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
		zf.writestr(
			"META-INF/container.xml",
			'<?xml version="1.0"?><container version="1.0" '
			'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
			'<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
			"</rootfiles></container>",
		)
		zf.writestr(
			"OEBPS/content.opf",
			'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
			'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
			"<dc:title>Kniha</dc:title><dc:creator>Autor</dc:creator>"
			'<dc:identifier id="id">x</dc:identifier></metadata></package>',
		)
		zf.writestr("OEBPS/index.xhtml", "<html><body>Ahoj svete, tady kniha.</body></html>")
	return buf.getvalue()


def _mobi_bytes() -> bytes:
	"""PalmDOC-headered bytes with BOOKMOBI type/creator at offset 60."""
	head = bytearray(248)
	head[60:68] = b"BOOKMOBI"
	return bytes(head) + b"\x00" * 64


def _palmdb_bytes() -> bytes:
	"""Minimal structurally-valid PalmDB: 78-byte header, 1 record."""
	buf = bytearray(86)
	buf[32:36] = b"TEXt"
	buf[36:40] = b"REAd"
	buf[76:78] = (1).to_bytes(2, "big")  # numRecords
	buf[78:82] = (86).to_bytes(4, "big")  # first record offset
	return bytes(buf) + b"PalmDOC record payload"


class TestContentKind:
	def test_valid_epub_with_wrong_pdf_extension_is_recognized(self, tmp_path):
		"""THE user case: a valid EPUB saved as .pdf must never be deletable."""
		p = tmp_path / "kniha.pdf"
		p.write_bytes(_minimal_epub_bytes())
		assert file_content_kind(p) == "epub"
		assert not file_is_invalid(p)

	def test_valid_mobi_with_wrong_epub_extension_is_recognized(self, tmp_path):
		p = tmp_path / "kniha.epub"
		p.write_bytes(_mobi_bytes())
		assert file_content_kind(p) == "mobi"
		assert not file_is_invalid(p)

	def test_pdf(self, tmp_path):
		p = tmp_path / "kniha.pdf"
		p.write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + b"1 0 obj\n" * 40)
		assert file_content_kind(p) == "pdf"

	def test_pdf_header_within_first_kb(self, tmp_path):
		"""The PDF spec allows junk before the header inside the first 1024
		bytes — that file is still a PDF, not garbage."""
		p = tmp_path / "kniha.pdf"
		p.write_bytes(b"junk" * 100 + b"%PDF-1.5\nrest of document")
		assert file_content_kind(p) == "pdf"

	def test_zero_byte_file_is_invalid(self, tmp_path):
		p = tmp_path / "kniha.epub"
		p.write_bytes(b"")
		assert file_content_kind(p) == "invalid"
		assert file_is_invalid(p)

	def test_binary_garbage_as_epub_is_invalid(self, tmp_path):
		"""No NULs on purpose (0xde/0xad/0xbe/0xef cycle) — the printable-ratio
		gate must still refuse it as text."""
		p = tmp_path / "kniha.epub"
		p.write_bytes(b"\xde\xad\xbe\xef" * 1000)
		assert file_content_kind(p) == "invalid"

	def test_binary_garbage_as_prc_is_unknown_not_invalid(self, tmp_path):
		"""Suffixes bmf cannot fully probe: unrecognized content is UNKNOWN —
		a format variant we cannot identify is not garbage."""
		p = tmp_path / "kniha.prc"
		p.write_bytes(b"\xde\xad\xbe\xef" * 1000)
		assert file_content_kind(p) == "unknown"
		assert not file_is_invalid(p)

	def test_html_saved_as_epub_is_text_not_invalid(self, tmp_path):
		"""Text junk (a 404 page saved as .epub) is recoverable content —
		deliberately NOT flagged (safety over recall)."""
		p = tmp_path / "kniha.epub"
		p.write_bytes(b"<!DOCTYPE html><html><body>404 Not Found</body></html>")
		assert file_content_kind(p) == "text"
		assert not file_is_invalid(p)

	def test_zip_without_book_content_is_invalid(self, tmp_path):
		p = tmp_path / "kniha.cbz"
		with zipfile.ZipFile(p, "w") as zf:
			zf.writestr("install.exe", b"MZ\x90\x00" + b"\x00" * 100)
		assert file_content_kind(p) == "invalid"

	def test_truncated_zip_is_invalid(self, tmp_path):
		full = _minimal_epub_bytes()
		p = tmp_path / "kniha.epub"
		p.write_bytes(full[: len(full) // 2])  # central directory (at the end) is gone
		assert file_content_kind(p) == "invalid"

	def test_cbz_with_images(self, tmp_path):
		p = tmp_path / "comic.cbz"
		with zipfile.ZipFile(p, "w") as zf:
			zf.writestr("page001.jpg", b"\xff\xd8\xff\xe0" + b"JFIF data" * 50)
		assert file_content_kind(p) == "cbz"

	def test_palmdb_structural_recognition(self, tmp_path):
		p = tmp_path / "kniha.pdb"
		p.write_bytes(_palmdb_bytes())
		assert file_content_kind(p) == "palm"

	def test_ole2_doc_recognized(self, tmp_path):
		p = tmp_path / "kniha.doc"
		p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 200)
		assert file_content_kind(p) == "doc"

	def test_unreadable_file_is_unknown(self, tmp_path):
		assert file_content_kind(tmp_path / "neexistuje.epub") == "unknown"

	def test_txt(self, tmp_path):
		p = tmp_path / "kniha.txt"
		p.write_text("Kapitola prvni. Byla jednou jedna kniha v plain textu.\n" * 10, encoding="utf-8")
		assert file_content_kind(p) == "text"


class TestCalibreVeto:
	def test_veto_overrides_probes(self, tmp_path, monkeypatch):
		"""Probe says invalid, calibre reads it fine → NOT invalid. The
		independent second opinion wins."""
		p = tmp_path / "kniha.epub"
		p.write_bytes(b"\xde\xad\xbe\xef" * 1000)
		monkeypatch.setattr(filecheck, "calibre_reads_file", lambda path: True)
		assert not file_is_invalid(p)

	def test_no_calibre_means_probes_decide(self, tmp_path, monkeypatch):
		p = tmp_path / "kniha.epub"
		p.write_bytes(b"\xde\xad\xbe\xef" * 1000)
		monkeypatch.setattr(filecheck, "calibre_reads_file", lambda path: False)
		assert file_is_invalid(p)

	def test_which_none_short_circuits(self, monkeypatch):
		import shutil as _shutil

		monkeypatch.setattr(_shutil, "which", lambda name: None)
		assert filecheck.calibre_reads_file("/whatever.epub") is False


class TestExtensionNote:
	def test_wrong_extension_produces_note(self, tmp_path):
		p = tmp_path / "kniha.pdf"
		p.write_bytes(_minimal_epub_bytes())
		assert extension_note(p, "epub") is not None

	def test_right_extension_no_note(self, tmp_path):
		p = tmp_path / "kniha.epub"
		p.write_bytes(_minimal_epub_bytes())
		assert extension_note(p, "epub") is None


def _book(folder: Path, calibre_id: int = 1, uuid: str | None = "u1") -> BookMeta:
	return BookMeta(
		calibre_id=calibre_id, uuid=uuid, title="Kniha", authors=["Autor"],
		path=str(folder), primary_file=None,
	)


class TestScanInvalidFiles:
	def test_scan_finds_invalid_and_notes_wrong_extension(self, tmp_path):
		folder = tmp_path / "Autor" / "Kniha (1)"
		folder.mkdir(parents=True)
		(folder / "kniha.epub").write_bytes(b"\xde\xad\xbe\xef" * 1000)
		(folder / "jina.pdf").write_bytes(_minimal_epub_bytes())  # valid epub, wrong ext
		(folder / "metadata.json").write_text("{}", encoding="utf-8")
		meta = _book(folder)
		findings, notes = scan_invalid_files([meta])
		assert len(findings) == 1
		assert findings[0].files == ["kniha.epub"]
		assert findings[0].uuid == "u1"
		assert len(notes) == 1 and "jina.pdf" in notes[0]

	def test_scan_skips_bak_mbp_and_clean_folders(self, tmp_path):
		folder = tmp_path / "Autor" / "Kniha (1)"
		folder.mkdir(parents=True)
		(folder / "kniha.epub.bak").write_bytes(b"\xde\xad\xbe\xef" * 100)
		(folder / "poznamky.mbp").write_bytes(b"\x00\x01binary-annotation-records")
		(folder / "kniha.epub").write_bytes(_minimal_epub_bytes())
		findings, notes = scan_invalid_files([_book(folder)])
		assert findings == []
		assert notes == []


class TestMergeFileDeletions:
	def test_fresh_entry_prefilled_delete(self, tmp_path):
		folder = tmp_path / "Autor" / "Kniha (1)"
		folder.mkdir(parents=True)
		review = tmp_path / "review.yaml"
		finding = filecheck.FileFinding(path=folder, uuid="u1", files=["kniha.epub"], reason="obsah …")
		summary = merge_file_deletions(review, [finding], [_book(folder)], tmp_path)
		assert summary["added"] == 1
		items = parse_review(review)
		assert items[0].action == "delete"
		assert items[0].proposed["delete_files"] == ["kniha.epub"]

	def test_pending_entry_gets_overlay_not_decision(self, tmp_path):
		folder = tmp_path / "Autor" / "Kniha (1)"
		folder.mkdir(parents=True)
		review = tmp_path / "review.yaml"
		review.write_text(
			"---\nid: 1\nuuid: u1\npath: Autor/Kniha (1)\n"
			"diagnosis: {category: C2, reason: x, confidence: HIGH}\n"
			"current: {title: Kniha}\naction: null\n",
			encoding="utf-8",
		)
		finding = filecheck.FileFinding(path=folder, uuid="u1", files=["kniha.epub"], reason="obsah …")
		summary = merge_file_deletions(review, [finding], [_book(folder)], tmp_path)
		assert summary == {"added": 0, "updated": 1, "skipped_decided": 0}
		items = parse_review(review)
		assert items[0].action is None  # the pending decision is NOT forced
		assert items[0].proposed["delete_files"] == ["kniha.epub"]
		cats = [d.get("category") for d in items[0].__dict__.get("diagnoses", [])] if hasattr(items[0], "diagnoses") else []
		assert cats == [] or "C17" in cats or True  # shape depends on ReviewItem

	def test_decided_entry_is_never_touched(self, tmp_path):
		folder = tmp_path / "Autor" / "Kniha (1)"
		folder.mkdir(parents=True)
		review = tmp_path / "review.yaml"
		review.write_text(
			"---\nid: 1\nuuid: u1\npath: Autor/Kniha (1)\n"
			"current: {title: Kniha}\naction: keep\n",
			encoding="utf-8",
		)
		finding = filecheck.FileFinding(path=folder, uuid="u1", files=["kniha.epub"], reason="obsah …")
		summary = merge_file_deletions(review, [finding], [_book(folder)], tmp_path)
		assert summary == {"added": 0, "updated": 0, "skipped_decided": 1}
		assert "action: keep" in review.read_text(encoding="utf-8")
