"""Tests for extractor helpers, focused on the catdoc fallback for .doc files.

catdoc handles legacy MS Word .doc files (Composite Document File) which
calibre's ebook-convert cannot read (no DOC input plugin). These are common
in the CZ/SK library — ~340 .doc files, many with filename-as-title C2
corruption that text_meta can only fix once the page text is available.
"""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from unittest.mock import patch

from book_meta_fix.extractors import _catdoc_to_text, _ebook_convert_to_text, _epub_isbn_scan_text, extract, extract_txt
from book_meta_fix.isbn import extract_isbn


class TestCatdocHelper:
	def test_returns_none_when_catdoc_not_installed(self, tmp_path):
		"""If catdoc is not on PATH, return None (don't raise)."""
		f = tmp_path / "book.doc"
		f.write_bytes(b"fake doc content")
		with patch("book_meta_fix.extractors.shutil.which", return_value=None):
			assert _catdoc_to_text(f) is None

	def test_returns_text_on_success(self, tmp_path):
		"""A successful catdoc invocation returns its stdout."""
		f = tmp_path / "book.doc"
		f.write_bytes(b"fake")
		with patch("book_meta_fix.extractors.shutil.which", return_value="/usr/bin/catdoc"), \
			 patch("book_meta_fix.extractors.subprocess.run") as mock_run:
			mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "Stanisław Lem\n\nNeapol - Řím\n"})()
			text = _catdoc_to_text(f)
		assert text is not None
		assert "Neapol - Řím" in text

	def test_returns_none_on_nonzero_exit(self, tmp_path):
		f = tmp_path / "book.doc"
		f.write_bytes(b"fake")
		with patch("book_meta_fix.extractors.shutil.which", return_value="/usr/bin/catdoc"), \
			 patch("book_meta_fix.extractors.subprocess.run") as mock_run:
			mock_run.return_value = type("R", (), {"returncode": 1, "stdout": ""})()
			assert _catdoc_to_text(f) is None

	def test_returns_none_on_empty_output(self, tmp_path):
		f = tmp_path / "book.doc"
		f.write_bytes(b"fake")
		with patch("book_meta_fix.extractors.shutil.which", return_value="/usr/bin/catdoc"), \
			 patch("book_meta_fix.extractors.subprocess.run") as mock_run:
			mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "  \n"})()
			assert _catdoc_to_text(f) is None

	def test_uses_errors_replace_to_survive_bad_utf8(self, tmp_path):
		"""catdoc output isn't always valid UTF-8 (one byte it can't map is
		enough); subprocess must use errors='replace' so a bad byte produces a
		replacement char instead of crashing the whole extraction (regression:
		UnicodeDecodeError on CZ .doc files)."""
		f = tmp_path / "book.doc"
		f.write_bytes(b"fake")
		with patch("book_meta_fix.extractors.shutil.which", return_value="/usr/bin/catdoc"), \
			 patch("book_meta_fix.extractors.subprocess.run") as mock_run:
			mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "ok"})()
			_catdoc_to_text(f)
			_, kwargs = mock_run.call_args
			assert kwargs.get("errors") == "replace"


class TestSafeExtractFallback:
	"""_safe_extract tries sibling formats when the primary yields no usable
	page text (corrupt epub, image-only PDF, empty catdoc .doc)."""

	def _meta_with_formats(self, tmp_path: Path, formats: list[str]):  # noqa: ANN001
		from book_meta_fix.models import BookMeta
		from book_meta_fix.readers import _collect_formats

		folder = tmp_path / "book (1)"
		folder.mkdir()
		(folder / "metadata.opf").write_text("<package/>", encoding="utf-8")
		for ext in formats:
			(folder / f"book{ext}").write_bytes(b"x")
		meta = BookMeta(calibre_id=1, title="t", authors=["a"], path=str(folder))
		_collect_formats(folder, meta)
		return meta

	def test_primary_with_usable_text_no_fallback(self, tmp_path):
		from book_meta_fix.extractors import ExtractedMeta
		from book_meta_fix.pipeline import _safe_extract

		meta = self._meta_with_formats(tmp_path, [".epub", ".pdb"])
		good = ExtractedMeta(first_page_text="Božena Němcová Babička text " * 10)
		with patch("book_meta_fix.pipeline.extract", return_value=good) as me:
			result = _safe_extract(meta)
		assert result is good
		assert me.call_count == 1  # only the primary, no sibling tried

	def test_falls_back_when_primary_text_unusable(self, tmp_path):
		from book_meta_fix.extractors import ExtractedMeta
		from book_meta_fix.pipeline import _safe_extract

		meta = self._meta_with_formats(tmp_path, [".epub", ".pdb"])
		bad = ExtractedMeta(first_page_text=None, title="from epub")
		good = ExtractedMeta(first_page_text="Karel Čapek R.U.R. text " * 10)
		with patch("book_meta_fix.pipeline.extract", side_effect=[bad, good]) as me:
			result = _safe_extract(meta)
		assert result is good  # fell back to the pdb sibling
		assert me.call_count == 2  # primary + one sibling

	def test_falls_back_when_primary_returns_none(self, tmp_path):
		from book_meta_fix.extractors import ExtractedMeta
		from book_meta_fix.pipeline import _safe_extract

		meta = self._meta_with_formats(tmp_path, [".epub", ".pdb"])
		good = ExtractedMeta(first_page_text="Franz Kafka Zámek text " * 10)
		with patch("book_meta_fix.pipeline.extract", side_effect=[None, good]):
			result = _safe_extract(meta)
		assert result is good

	def test_returns_primary_when_no_sibling_helps(self, tmp_path):
		"""If no sibling yields usable text either, return the primary (it may
		still carry embedded metadata even without page text)."""
		from book_meta_fix.extractors import ExtractedMeta
		from book_meta_fix.pipeline import _safe_extract

		meta = self._meta_with_formats(tmp_path, [".epub", ".pdb"])
		bad = ExtractedMeta(first_page_text=None, title="from epub")
		bad2 = ExtractedMeta(first_page_text=None)
		with patch("book_meta_fix.pipeline.extract", side_effect=[bad, bad2]):
			result = _safe_extract(meta)
		assert result is bad


class TestEbookConvertToTextDocFallback:
	def test_doc_tries_catdoc_first(self, tmp_path):
		"""For .doc files, catdoc is tried before ebook-convert."""
		f = tmp_path / "book.doc"
		f.write_bytes(b"fake doc")
		with patch("book_meta_fix.extractors._catdoc_to_text", return_value="extracted text") as mock_catdoc, \
			 patch("book_meta_fix.extractors.shutil.which", return_value="/usr/bin/ebook-convert") as mock_which:
			text = _ebook_convert_to_text(f)
		assert text == "extracted text"
		mock_catdoc.assert_called_once_with(f)
		# ebook-convert should NOT have been called (catdoc succeeded)
		mock_which.assert_not_called()

	def test_doc_falls_back_to_ebook_convert_when_catdoc_fails(self, tmp_path):
		"""If catdoc returns None, fall through to ebook-convert."""
		f = tmp_path / "book.doc"
		f.write_bytes(b"fake doc")
		with patch("book_meta_fix.extractors._catdoc_to_text", return_value=None), \
			 patch("book_meta_fix.extractors.shutil.which", return_value=None):
			# Both catdoc and ebook-convert unavailable -> None
			assert _ebook_convert_to_text(f) is None

	def test_non_doc_skips_catdoc(self, tmp_path):
		"""For .pdb/.mobi files, catdoc is never tried."""
		f = tmp_path / "book.pdb"
		f.write_bytes(b"fake pdb")
		with patch("book_meta_fix.extractors._catdoc_to_text") as mock_catdoc, \
			 patch("book_meta_fix.extractors.shutil.which", return_value=None):
			_ebook_convert_to_text(f)
		mock_catdoc.assert_not_called()


def _build_epub(path: Path, item_htmls: list[str]) -> None:
	"""Write a minimal valid EPUB whose spine items are *item_htmls* in order."""
	container = (
		'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
		'<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
		'</rootfiles></container>'
	)
	manifest = "".join(
		f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>'
		for i in range(len(item_htmls))
	)
	spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(item_htmls)))
	opf = (
		'<package xmlns="http://www.idpf.org/2007/opf">'
		'<metadata><dc:title xmlns:dc="http://purl.org/dc/elements/1.1/">x</dc:title></metadata>'
		f'<manifest>{manifest}</manifest><spine>{spine}</spine></package>'
	)
	with zipfile.ZipFile(path, "w") as zf:
		zf.writestr("META-INF/container.xml", container)
		zf.writestr("content.opf", opf)
		for i, html in enumerate(item_htmls):
			zf.writestr(f"c{i}.xhtml", html)


class TestIsbnScanEndPages:
	"""ISBN often lives on the copyright/colophon page (middle/end of the book),
	not in the first 3000 chars. The ISBN scan now covers first + last chunks."""

	def test_txt_isbn_in_tail(self, tmp_path):
		"""An ISBN only present in the LAST 5000 bytes of a TXT is found."""
		f = tmp_path / "book.txt"
		head = "Babička. Božena Němcová.\n" + ("obsah kapitoly " * 800)  # >8000 bytes
		tail = "\n\nVydavatel: Albatros. ISBN 978-80-720-7232-3.\n"
		f.write_text(head + tail, encoding="utf-8")
		result = extract_txt(f)
		assert result.isbn_from_text is not None
		assert "9788072072323" in result.isbn_from_text

	def test_epub_isbn_in_last_spine_item(self, tmp_path):
		"""An ISBN only in the LAST spine item (beyond the first 8) is found via
		the first-5 + last-5 ISBN scan, not just first_page_text."""
		# 12 spine items; the ISBN is in item 11 (last), which is NOT in the
		# first 8 (first_page_text) but IS in the last 5.
		items = [f"<p>kapitola {i} text text text</p>" for i in range(11)]
		items.append("<p>Colophon. Vydavatel Albatros. ISBN 978-80-720-7232-3.</p>")
		epub = tmp_path / "book.epub"
		_build_epub(epub, items)
		with zipfile.ZipFile(epub) as zf:
			scan_text = _epub_isbn_scan_text(zf, "content.opf")
		assert extract_isbn(scan_text) is not None
		assert "9788072072323" in (extract_isbn(scan_text) or "")

	def test_epub_isbn_scan_includes_end_when_short(self, tmp_path):
		"""For a short spine (≤10 items), all items are scanned."""
		items = ["<p>titul</p>", "<p>ISBN 978-80-720-7232-3</p>"]
		epub = tmp_path / "book.epub"
		_build_epub(epub, items)
		with zipfile.ZipFile(epub) as zf:
			scan_text = _epub_isbn_scan_text(zf, "content.opf")
		assert extract_isbn(scan_text) is not None


class TestComicExtractor:
	"""Comics are books too. .cbz/.cbr/.cb7 are image archives; when they carry
	a ComicInfo.xml it is the file's own metadata declaration (like EPUB OPF)."""

	def test_cbz_parses_comicinfo_xml(self, tmp_path):
		from book_meta_fix.extractors import extract_comic

		comicinfo = (
			"<ComicInfo>"
			"<Title>The Return</Title>"
			"<Series>Neverwhere</Series><Number>6</Number>"
			"<Writer>Laini Taylor</Writer>"
			"<Publisher>Archa</Publisher><Year>2018</Year>"
			"<ISBN>978-80-720-7232-3</ISBN><LanguageISO>cs</LanguageISO>"
			"</ComicInfo>"
		)
		cbz = tmp_path / "comic.cbz"
		with zipfile.ZipFile(cbz, "w") as zf:
			zf.writestr("ComicInfo.xml", comicinfo)
			zf.writestr("page01.jpg", b"img")
		result = extract_comic(cbz)
		assert result.error is None
		assert result.source_format == "cbz"
		assert result.title == "The Return"
		assert result.authors == ["Laini Taylor"]
		assert result.publisher == "Archa"
		assert result.year_from_text == 2018
		assert result.isbn == "9788072072323"
		assert result.language == "cs"

	def test_cbz_without_comicinfo_returns_error(self, tmp_path):
		from book_meta_fix.extractors import extract_comic

		cbz = tmp_path / "comic.cbz"
		with zipfile.ZipFile(cbz, "w") as zf:
			zf.writestr("page01.jpg", b"img")
		result = extract_comic(cbz)
		assert result.error == "no ComicInfo.xml"
		assert result.first_page_text is None  # image-only — no text layer

	def test_title_falls_back_to_series_number(self, tmp_path):
		from book_meta_fix.extractors import extract_comic

		cbz = tmp_path / "comic.cbz"
		with zipfile.ZipFile(cbz, "w") as zf:
			zf.writestr("ComicInfo.xml", "<ComicInfo><Series>Prey</Series><Number>4</Number></ComicInfo>")
		result = extract_comic(cbz)
		assert result.title == "Prey #4"
