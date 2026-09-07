"""Tests for extractor helpers, focused on the catdoc fallback for .doc files.

catdoc handles legacy MS Word .doc files (Composite Document File) which
calibre's ebook-convert cannot read (no DOC input plugin). These are common
in the CZ/SK library — ~340 .doc files, many with filename-as-title C2
corruption that text_meta can only fix once the page text is available.
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import patch

from book_meta_fix.extractors import _catdoc_to_text, _ebook_convert_to_text, _epub_isbn_scan_text, extract_txt
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
	"""safe_extract tries sibling formats when the primary yields no usable
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
		from book_meta_fix.verifier import safe_extract

		meta = self._meta_with_formats(tmp_path, [".epub", ".pdb"])
		good = ExtractedMeta(first_page_text="Božena Němcová Babička text " * 10)
		with patch("book_meta_fix.verifier.extract", return_value=good) as me:
			result = safe_extract(meta)
		assert result is good
		assert me.call_count == 1  # only the primary, no sibling tried

	def test_falls_back_when_primary_text_unusable(self, tmp_path):
		from book_meta_fix.extractors import ExtractedMeta
		from book_meta_fix.verifier import safe_extract

		meta = self._meta_with_formats(tmp_path, [".epub", ".pdb"])
		bad = ExtractedMeta(first_page_text=None, title="from epub")
		good = ExtractedMeta(first_page_text="Karel Čapek R.U.R. text " * 10)
		with patch("book_meta_fix.verifier.extract", side_effect=[bad, good]) as me:
			result = safe_extract(meta)
		assert result is good  # fell back to the pdb sibling
		assert me.call_count == 2  # primary + one sibling

	def test_falls_back_when_primary_returns_none(self, tmp_path):
		from book_meta_fix.extractors import ExtractedMeta
		from book_meta_fix.verifier import safe_extract

		meta = self._meta_with_formats(tmp_path, [".epub", ".pdb"])
		good = ExtractedMeta(first_page_text="Franz Kafka Zámek text " * 10)
		with patch("book_meta_fix.verifier.extract", side_effect=[None, good]):
			result = safe_extract(meta)
		assert result is good

	def test_returns_primary_when_no_sibling_helps(self, tmp_path):
		"""If no sibling yields usable text either, return the primary (it may
		still carry embedded metadata even without page text)."""
		from book_meta_fix.extractors import ExtractedMeta
		from book_meta_fix.verifier import safe_extract

		meta = self._meta_with_formats(tmp_path, [".epub", ".pdb"])
		bad = ExtractedMeta(first_page_text=None, title="from epub")
		bad2 = ExtractedMeta(first_page_text=None)
		with patch("book_meta_fix.verifier.extract", side_effect=[bad, bad2]):
			result = safe_extract(meta)
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


class TestOcrFallback:
	"""Image-only books (scanned PDFs, comics without ComicInfo.xml) get OCR'd
	via tesseract when it's installed. These tests mock the binaries so they run
	without tesseract/pdftoppm actually present."""

	def test_pdf_ocr_when_no_text_layer(self, tmp_path):
		from book_meta_fix.extractors import extract_pdf

		f = tmp_path / "scan.pdf"
		f.write_bytes(b"%PDF-1.4 fake scanned")
		# No pdfinfo/pdftotext (shutil.which → None) so first_page_text stays
		# None and the OCR fallback path is taken.
		with patch("book_meta_fix.extractors.shutil.which", return_value=None), \
			 patch("book_meta_fix.extractors._render_pdf_page_png", return_value=b"PNGBYTES"), \
			 patch("book_meta_fix.extractors._ocr_image_bytes", return_value="Babička\nBožena Němcová\nISBN 978-80-720-7232-3"):
			result = extract_pdf(f)
		assert result.first_page_text is not None
		assert "Babička" in result.first_page_text
		# ISBN recovered from the OCR'd cover/copyright text.
		assert result.isbn_from_text == "9788072072323"

	def test_pdf_skips_ocr_when_text_present(self, tmp_path):
		"""When pdftotext yields text, the OCR path is not taken."""
		from book_meta_fix.extractors import extract_pdf

		f = tmp_path / "book.pdf"
		f.write_bytes(b"%PDF fake")

		def fake_which(tool):
			return "/usr/bin/pdftotext" if tool == "pdftotext" else None

		with patch("book_meta_fix.extractors.shutil.which", side_effect=fake_which), \
			 patch("book_meta_fix.extractors.subprocess.run") as mock_run, \
			 patch("book_meta_fix.extractors._render_pdf_page_png") as mock_render:
			# The pdftotext call returns real text.
			mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "Kniha s textovou vrstvou " * 30})()
			result = extract_pdf(f)
		assert result.first_page_text is not None  # came from pdftotext
		mock_render.assert_not_called()  # OCR never triggered

	def test_comic_ocr_cover_when_no_comicinfo(self, tmp_path):
		from book_meta_fix.extractors import extract_comic

		cbz = tmp_path / "comic.cbz"
		with zipfile.ZipFile(cbz, "w") as zf:
			zf.writestr("page01.jpg", b"img")  # no ComicInfo.xml
		with patch("book_meta_fix.extractors._comic_first_image", return_value=b"COVERIMG"), \
			 patch("book_meta_fix.extractors._ocr_image_bytes", return_value="Batman\nBob Kane\nISBN 978-80-720-7232-3"):
			result = extract_comic(cbz)
		# Cover was OCR'd → first_page_text + ISBN recovered, error cleared.
		assert result.first_page_text is not None
		assert "Batman" in result.first_page_text
		assert result.isbn_from_text == "9788072072323"
		assert result.error is None

	def test_ocr_noop_without_tesseract(self, tmp_path):
		"""When tesseract isn't installed, OCR silently returns None (no crash)."""
		from book_meta_fix.extractors import _ocr_image_bytes

		with patch("book_meta_fix.extractors.shutil.which", return_value=None):
			assert _ocr_image_bytes(b"some png bytes") is None


class TestExtractMbp:
	"""Mobipocket annotations sidecar (.mbp) — UTF-16-BE AUTH/TITL records.

	Real wild layout (library has 64 of these, several folders where the
	.mbp is the ONLY surviving format file): NUL-padded docname header,
	BPAR/MOBI structures, then tagged records — 4-char tag + uint32
	big-endian length + UTF-16 payload. Mobipocket is PalmOS-descended, so
	the payload is UTF-16 **big-endian** (little-endian decode yields
	printable CJK garbage — see test_be_payload_not_misread_as_le).
	"""

	def _make_mbp(self, path, *, author="Vyhídka, Petr", title="Efekt Gun Clubu"):
		def rec(tag, text):
			payload = text.encode("utf-16-be")
			return tag + len(payload).to_bytes(4, "big") + payload

		data = b"Efekt Gun Clubu_PAR" + b"\x00" * 33 + b"BPARMOBI" + b"\x00" * 24
		if author is not None:
			data += rec(b"AUTH", author)
		if title is not None:
			data += rec(b"TITL", title)
		path.write_bytes(data)
		return path

	def test_reads_author_and_title(self, tmp_path):
		from book_meta_fix.extractors import extract_mbp

		f = self._make_mbp(tmp_path / "kniha.mbp")
		m = extract_mbp(f)
		assert m.error is None
		assert m.title == "Efekt Gun Clubu"
		# the author string is kept WHOLE — "Vyhídka, Petr" is one person
		assert m.authors == ["Vyhídka, Petr"]
		assert m.source_format == "mbp"
		assert not m.first_page_text  # not a book: no page text to mine

	def test_be_payload_not_misread_as_le(self, tmp_path):
		# Regression: LE decoding of the BE payload gives CJK garbage
		# ("䄀搀愀洀猀" for "Adams") which passes isprintable — the dual
		# decode + Latin-score gate must pick the sane variant.
		from book_meta_fix.extractors import extract_mbp

		f = self._make_mbp(tmp_path / "a.mbp", author="Adams, Douglas", title="Stopařův průvodce")
		m = extract_mbp(f)
		assert m.authors == ["Adams, Douglas"]
		assert m.title == "Stopařův průvodce"

	def test_dispatch_by_extension(self, tmp_path):
		from book_meta_fix.extractors import extract

		f = self._make_mbp(tmp_path / "kniha.mbp")
		m = extract(f)
		assert m.source_format == "mbp"
		assert m.error is None
		assert m.title == "Efekt Gun Clubu"

	def test_no_records_reports_error(self, tmp_path):
		from book_meta_fix.extractors import extract_mbp

		f = tmp_path / "kniha.mbp"
		f.write_bytes(b"Efekt Gun Clubu_PAR" + b"\x00" * 64)
		m = extract_mbp(f)
		assert m.error == "no AUTH/TITL records"
		assert m.title is None and not m.authors

	def test_corrupt_length_record_skipped(self, tmp_path):
		# AUTH claiming more bytes than the file has is skipped; TITL survives.
		from book_meta_fix.extractors import extract_mbp

		f = tmp_path / "kniha.mbp"
		payload = "Vyhídka, Petr".encode("utf-16-be")
		data = b"AUTH" + (len(payload) + 9999).to_bytes(4, "big") + payload
		data += b"TITL" + len("Efekt Gun Clubu".encode("utf-16-be")).to_bytes(4, "big")
		data += "Efekt Gun Clubu".encode("utf-16-be")
		f.write_bytes(data)
		m = extract_mbp(f)
		assert m.error is None
		assert m.title == "Efekt Gun Clubu"
		assert m.authors == []

	def test_mbp_registered_last_in_ebook_exts(self):
		# .mbp must be in EBOOK_EXTS but LAST — a real book is always the
		# primary format; the .mbp only steps up in book-less folders.
		from book_meta_fix.readers import EBOOK_EXTS

		assert ".mbp" in EBOOK_EXTS
		assert EBOOK_EXTS[-1] == ".mbp"


class TestTxtBoundedReads:
	"""extract_txt must read ONLY the bounded windows (15 KB head + 5 KB tail).
	The pre-fix code read the WHOLE file into memory twice — catastrophic for
	multi-MB TXTs on NFS."""

	def _make_txt(self, tmp_path: Path, size: int, head: str, tail: str, name: str = "book.txt") -> Path:
		f = tmp_path / name
		middle = "x" * (size - len(head.encode()) - len(tail.encode()))
		f.write_text(head + middle + tail, encoding="utf-8")
		return f

	def test_bytes_read_are_bounded(self, tmp_path: Path, monkeypatch) -> None:
		import builtins

		f = self._make_txt(tmp_path, 100_000, "Titul knihy\n", "\nkonec\n")
		read_bytes = {"n": 0}
		real_open = builtins.open

		def counting_open(file, mode="r", *args, **kwargs):
			fh = real_open(file, mode, *args, **kwargs)
			if "b" in mode and hasattr(fh, "read"):
				real_read = fh.read

				def counted_read(*a, **k):
					data = real_read(*a, **k)
					read_bytes["n"] += len(data) if data else 0
					return data

				fh.read = counted_read  # type: ignore[method-assign]
			return fh

		monkeypatch.setattr(builtins, "open", counting_open)
		result = extract_txt(f)
		monkeypatch.undo()
		assert result.first_page_text  # semantics intact
		# 15 KB head + 5 KB tail (+seek slack) for a 100 KB file.
		assert read_bytes["n"] <= 15000 + 5000 + 8192

	def test_semantics_head_and_tail(self, tmp_path: Path) -> None:
		# ISBN in the head is found; a head without one falls back to the tail.
		f = self._make_txt(tmp_path, 60_000, "ISBN 978-80-720-7232-3\n", "\nkonec\n")
		assert extract_txt(f).isbn_from_text is not None
		f2 = self._make_txt(tmp_path, 60_000, "Babička\nBožena Němcová.\n", "\nISBN 978-80-720-7232-3\n", name="book2.txt")
		# head has no ISBN and is > 8000 bytes -> the tail is scanned
		assert extract_txt(f2).isbn_from_text is not None


class TestEpubSingleOpfRead:
	"""extract_epub reads the OPF member ONCE and every spine member at most
	ONCE per extraction. The pre-fix shape re-read+re-parsed the OPF 4x and
	the first spine items 3x — each read is an NFS round trip."""

	def test_opf_and_spine_members_read_once(self, tmp_path: Path) -> None:
		import book_meta_fix.extractors as exmod
		from book_meta_fix.extractors import extract_epub

		items = [f"<p>kapitola {i} — {('text ' * 40)}</p>" for i in range(12)]
		items.append("<p>Colophon. ISBN 978-80-720-7232-3.</p>")
		epub = tmp_path / "book.epub"
		_build_epub(epub, items)

		counts: dict[str, int] = {}
		real_zipfile = exmod.zipfile.ZipFile

		class CountingZipFile(real_zipfile):  # type: ignore[misc, valid-type]
			def read(self, name):
				counts[name] = counts.get(name, 0) + 1
				return super().read(name)

		monkeypatch_target = "book_meta_fix.extractors.zipfile.ZipFile"
		import pytest

		with pytest.MonkeyPatch.context() as mp:
			mp.setattr(monkeypatch_target, CountingZipFile)
			result = extract_epub(epub)

		assert result.error is None
		assert result.first_page_text
		# The OPF is fetched exactly once (was 4x).
		assert counts.get("content.opf") == 1
		# Spine members in all three windows (first 8 / first 5+last 5 / first
		# 15) are fetched exactly once each (c0 was 3x before).
		for i in range(5):
			assert counts.get(f"c{i}.xhtml") == 1, f"c{i}.xhtml read more than once"
		for i in range(5, 8):
			assert counts.get(f"c{i}.xhtml") == 1
		assert counts.get("c12.xhtml") == 1  # the last-5 window tail item
		# And the semantics survived: ISBN from the colophon, 13 items joined.
		assert result.isbn_from_text and "9788072072323" in result.isbn_from_text
		assert result.broader_text and len(result.broader_text) > len(result.first_page_text)
