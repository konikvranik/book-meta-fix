"""Tests for cover detection, download, and the C11/MISSING_COVER detector rules.

Covers two layers:
  - covers.py: pixel-analysis classification + atomic download
  - detectors.py: rule_generated_cover / rule_missing_cover
"""
from __future__ import annotations

import io
import os
import posixpath
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from book_meta_fix.covers import (
	analyze_cover,
	download_cover,
	epub_cover_image,
	image_is_readable,
	probe_embedded_cover,
	recover_cover_from_book,
	strip_cover_from_book,
	strip_generated_covers,
)
from book_meta_fix.models import BookMeta, Confidence, Verdict

# ---------------------------------------------------------------------------
# Helpers: create synthetic cover images on disk
# ---------------------------------------------------------------------------


def _solid_cover(path: Path, size: tuple[int, int] = (1200, 1600), color=(50, 50, 50)) -> None:
	"""A solid-color image — the simplest generated-cover signature."""
	Image.new("RGB", size, color=color).save(path)


def _gradient_cover(path: Path, size: tuple[int, int] = (458, 500)) -> None:
	"""A colour-rich gradient — mimics real artwork, should NOT be flagged generated."""
	img = Image.new("RGB", size)
	px = img.load()
	import random

	random.seed(42)
	for y in range(size[1]):
		for x in range(size[0]):
			r = (x + y) % 256
			g = (x * 2 + random.randint(0, 50)) % 256
			b = (y * 2 + random.randint(0, 50)) % 256
			px[x, y] = (r, g, b)
	img.save(path)


def _text_page_cover(path: Path, size: tuple[int, int] = (1240, 1752)) -> None:
	"""A rendered text page — white sheet with many black text lines.

	Mimics calibre's page-1 "default cover" (ebook-meta --get-cover on a
	coverless book): white background, rows of dark text with white gaps,
	no colour anywhere.
	"""
	img = Image.new("RGB", size, color=(250, 250, 250))
	px = img.load()
	import random

	random.seed(7)
	margin_x, line_h, gap = 90, 10, 18
	y = 120
	while y + line_h < size[1] - 100:
		for dy in range(line_h):
			for x in range(margin_x, size[0] - margin_x):
				# ragged ink: ~60% coverage with grey-level jitter
				if random.random() < 0.6:
					v = random.randint(0, 90)
					px[x, y + dy] = (v, v, v)
		y += line_h + gap
	img.save(path)


def _minimalist_white_cover(path: Path, size: tuple[int, int] = (600, 900)) -> None:
	"""A white cover with a short title block — a REAL minimalist cover that
	must NOT be classified as a text-page render (few ink rows)."""
	img = Image.new("RGB", size, color=(255, 255, 255))
	px = img.load()
	for y in range(400, 460):
		for x in range(200, 400):
			px[x, y] = (20, 20, 20)
	img.save(path)


def _real_cover(path: Path) -> None:
	"""A small decodable, NON-generated image — the fixture stand-in for
	"a cover exists" shared across test modules.

	Synthetic bytes (b"cover", fake JPEG magic) stopped counting as a cover
	once rule_missing_cover began requiring a decodable file; a SOLID colour
	would trip C11's few-colours signal and make the cover look generated.
	The gradient is colour-rich on purpose: no C11 signal, decodes fine.
	"""
	_gradient_cover(path, size=(60, 90))


def _extractor_writing(image_fn):
	"""A fake extract_cover_from_book that writes image_fn(dest) and returns dest.

	Used to drive recover_cover_from_book without calibre: the real primitive
	shells out to ebook-meta, but recover_cover_from_book only needs *some*
	recognisable image at the temp path to validate.
	"""

	def _fake(book_path, dest=None):
		dest = Path(dest)
		image_fn(dest)
		return dest

	return _fake


def _solid_jpeg_bytes(size: tuple[int, int] = (1200, 1600), color=(60, 60, 60)) -> bytes:
	"""Solid-colour JPEG in memory — a generated placeholder for embedding in EPUBs."""
	buf = io.BytesIO()
	Image.new("RGB", size, color=color).save(buf, format="JPEG")
	return buf.getvalue()


def _gradient_jpeg_bytes(size: tuple[int, int] = (458, 500)) -> bytes:
	"""Colour-rich JPEG in memory — real artwork, must not be flagged generated."""
	img = Image.new("RGB", size)
	px = img.load()
	import random

	random.seed(7)
	for y in range(size[1]):
		for x in range(size[0]):
			r = (x + y) % 256
			g = (x * 2 + random.randint(0, 50)) % 256
			b = (y * 2 + random.randint(0, 50)) % 256
			px[x, y] = (r, g, b)
	buf = io.BytesIO()
	img.save(buf, format="JPEG")
	return buf.getvalue()


def _calibre_marked_jpeg_bytes(size: tuple[int, int] = (1200, 1600)) -> bytes:
	"""Colour-rich JPEG carrying calibre's generator COM comment.

	The parchment-template shape measured in the real library (2026-09-16):
	a beige vignette + ornamental border is colour-rich enough to dodge every
	pixel signal, yet calibre signs its generator output with
	``Generated cover: calibre <version>`` in the JPEG COM comment.
	"""
	buf = io.BytesIO()
	img = Image.new("RGB", size)
	px = img.load()
	import random

	random.seed(11)
	for y in range(size[1]):
		for x in range(size[0]):
			r = (x + y) % 256
			g = (x * 2 + random.randint(0, 50)) % 256
			b = (y * 2 + random.randint(0, 50)) % 256
			px[x, y] = (r, g, b)
	img.save(buf, format="JPEG", comment=b"Generated cover: calibre 5.42.0")
	return buf.getvalue()


# Not-an-image bytes: an HTML page saved where a cover image belongs — the
# exact thing ABS feeds ffmpeg when it picks a cover by file extension.
_HTML_BYTES = b"<html><head><title>Cover</title></head><body>Not an image.</body></html>"


# ---------------------------------------------------------------------------
# analyze_cover
# ---------------------------------------------------------------------------


class TestAnalyzeCover:
	def test_solid_1200x1600_detected_as_generated(self, tmp_path: Path) -> None:
		cover = tmp_path / "cover.jpg"
		_solid_cover(cover)
		info = analyze_cover(cover)
		assert info.is_generated is True
		assert info.width == 1200 and info.height == 1600
		assert info.confidence >= 0.5
		assert any("calibre" in s for s in info.signals)

	def test_gradient_not_detected_as_generated(self, tmp_path: Path) -> None:
		cover = tmp_path / "cover.jpg"
		_gradient_cover(cover)
		info = analyze_cover(cover)
		assert info.is_generated is False

	def test_real_dimensions_not_flagged(self, tmp_path: Path) -> None:
		# A real cover with non-1200x1600 dimensions and rich colour.
		cover = tmp_path / "cover.jpg"
		_gradient_cover(cover, size=(300, 450))
		info = analyze_cover(cover)
		assert info.is_generated is False

	def test_solid_non_calibre_size_detected_as_generated(self, tmp_path: Path) -> None:
		"""A generated cover at a non-default size is caught by colour analysis.

		Regression for the main detection gap: only exactly-1200x1600 used to be
		detected, so Calibre placeholders generated at other sizes (1240x1755,
		876x1240, ...) were missed entirely even though they're solid bg + text.
		"""
		cover = tmp_path / "cover.jpg"
		_solid_cover(cover, size=(876, 1240))  # a real Calibre variant size, not 1200x1600
		info = analyze_cover(cover)
		assert info.is_generated is True
		assert (info.width, info.height) != (1200, 1600)  # caught despite non-default size
		assert any("few_colours" in s for s in info.signals)

	def test_colourful_non_calibre_size_not_detected(self, tmp_path: Path) -> None:
		"""A colour-rich cover at a non-calibre size is not flagged (no false positive)."""
		cover = tmp_path / "cover.jpg"
		_gradient_cover(cover, size=(876, 1240))
		info = analyze_cover(cover)
		assert info.is_generated is False

	def test_calibre_size_photo_not_detected(self, tmp_path: Path) -> None:
		"""A photo-like cover at exactly 1200x1600 is NOT flagged just for the size.

		Regression: the size signal used to fire on every 1200x1600 cover, which
		false-positived ~1/3 of them (photos that merely share the default size).
		The size signal is now gated on the few-colours check.
		"""
		cover = tmp_path / "cover.jpg"
		_gradient_cover(cover, size=(1200, 1600))  # correct size, but colour-rich
		info = analyze_cover(cover)
		assert info.width == 1200 and info.height == 1600
		assert info.is_generated is False

	def test_nonexistent_file_returns_not_generated(self) -> None:
		info = analyze_cover("/nonexistent/cover.jpg")
		assert info.is_generated is False


class TestCalibreCommentMarker:
	"""Signal 5: calibre signs every generated cover with the JPEG COM comment
	``Generated cover: calibre <version>``.

	The parchment default template (beige vignette + ornamental border + title
	text) is colour-rich enough to dodge every PIXEL signal yet is pure
	placeholder — measured 2026-09-16 on three covers surviving
	``clean --covers --generated --apply`` in the real library. The marker
	cannot false-positive: scanners and publishers never write it.
	"""

	def test_marked_gradient_is_generated_despite_pixel_math(self, tmp_path: Path) -> None:
		cover = tmp_path / "cover.jpg"
		cover.write_bytes(_calibre_marked_jpeg_bytes())
		info = analyze_cover(cover)
		assert info.is_generated is True
		assert info.confidence == 1.0
		assert any(s.startswith("calibre_comment") for s in info.signals)

	def test_same_gradient_without_marker_stays_real(self, tmp_path: Path) -> None:
		# Byte-identical construction minus the comment — the pixel math alone
		# must keep classifying this shape as real artwork.
		cover = tmp_path / "cover.jpg"
		cover.write_bytes(_gradient_jpeg_bytes(size=(1200, 1600)))
		assert analyze_cover(cover).is_generated is False

	def test_marker_match_is_case_insensitive(self, tmp_path: Path) -> None:
		buf = io.BytesIO()
		Image.new("RGB", (60, 90), color=(120, 30, 200)).save(
			buf, format="JPEG", comment=b"generated COVER: calibre 3.21.0",
		)
		cover = tmp_path / "cover.jpg"
		cover.write_bytes(buf.getvalue())
		assert analyze_cover(cover).is_generated is True

	def test_analyze_cover_bytes_honours_marker(self) -> None:
		from book_meta_fix.covers import analyze_cover_bytes

		info = analyze_cover_bytes(_calibre_marked_jpeg_bytes(size=(458, 500)))
		assert info.is_generated is True
		assert any(s.startswith("calibre_comment") for s in info.signals)

	def test_is_calibre_generated_cover_marker_only(self) -> None:
		# The ABS-audit helper: marker ONLY, never pixel math — a false
		# positive there deletes a cover the user uploaded via the ABS UI.
		from book_meta_fix.covers import is_calibre_generated_cover

		assert is_calibre_generated_cover(_calibre_marked_jpeg_bytes(size=(60, 90))) is True
		# A solid-colour placeholder WITHOUT the marker: pixel math would flag
		# it, the marker-only helper must not.
		assert is_calibre_generated_cover(_solid_jpeg_bytes()) is False
		assert is_calibre_generated_cover(b"not an image at all") is False


# ---------------------------------------------------------------------------
# download_cover
# ---------------------------------------------------------------------------


class TestDownloadCover:
	def test_successful_download_writes_file(self, tmp_path: Path) -> None:
		"""A valid JPEG response is written atomically to dest_path."""
		# Build a real JPEG in memory
		buf = io.BytesIO()
		Image.new("RGB", (100, 150), color=(10, 20, 30)).save(buf, format="JPEG")
		jpeg_bytes = buf.getvalue()

		dest = tmp_path / "cover.jpg"
		mock_response = type(
			"R",
			(),
			{"status_code": 200, "content": jpeg_bytes},
		)
		with patch("requests.get", return_value=mock_response):
			ok = download_cover("https://example.com/cover.jpg", dest)
		assert ok is True
		assert dest.is_file()
		assert dest.stat().st_size == len(jpeg_bytes)

	def test_backup_created_when_overwriting(self, tmp_path: Path) -> None:
		"""An existing cover.jpg is backed up to cover.jpg.bak before overwrite."""
		dest = tmp_path / "cover.jpg"
		old_bytes = b"old cover bytes"
		dest.write_bytes(old_bytes)

		buf = io.BytesIO()
		Image.new("RGB", (50, 80), color=(1, 2, 3)).save(buf, format="JPEG")
		mock_response = type("R", (), {"status_code": 200, "content": buf.getvalue()})
		with patch("requests.get", return_value=mock_response):
			download_cover("https://example.com/new.jpg", dest)

		bak = dest.with_suffix(".jpg.bak")
		assert bak.is_file()
		assert bak.read_bytes() == old_bytes

	def test_http_error_returns_false(self, tmp_path: Path) -> None:
		dest = tmp_path / "cover.jpg"
		mock_response = type("R", (), {"status_code": 404, "content": b""})
		with patch("requests.get", return_value=mock_response):
			ok = download_cover("https://example.com/missing.jpg", dest)
		assert ok is False
		assert not dest.exists()

	def test_non_image_response_rejected(self, tmp_path: Path) -> None:
		"""A 200 response with non-image bytes is rejected (Pillow verify fails)."""
		dest = tmp_path / "cover.jpg"
		mock_response = type("R", (), {"status_code": 200, "content": b"not an image at all"})
		with patch("requests.get", return_value=mock_response):
			ok = download_cover("https://example.com/fake.jpg", dest)
		assert ok is False
		assert not dest.exists()

	def test_missing_pillow_accepts_bytes_as_is(self, tmp_path: Path) -> None:
		"""When Pillow is unavailable, a successful download is accepted as-is.

		Regression: the import and verify used to share one try/except, and the
		broad `except Exception` came before `except ImportError` — so a missing
		PIL was misreported as "not a valid image" and every download failed.
		"""
		# Genuine JPEG bytes (the kind a real server returns); we don't need to
		# construct them via Pillow here, only to show they're written through.
		buf = io.BytesIO()
		Image.new("RGB", (40, 60), color=(0, 0, 0)).save(buf, format="JPEG")
		jpeg_bytes = buf.getvalue()

		dest = tmp_path / "cover.jpg"
		mock_response = type("R", (), {"status_code": 200, "content": jpeg_bytes})

		import builtins

		real_import = builtins.__import__

		def _block_pil(name, *args, **kwargs):
			if name == "PIL":
				raise ModuleNotFoundError("No module named 'PIL'")
			return real_import(name, *args, **kwargs)

		with patch("requests.get", return_value=mock_response), \
				patch("builtins.__import__", side_effect=_block_pil):
			ok = download_cover("https://example.com/cover.jpg", dest)

		assert ok is True
		assert dest.is_file()
		assert dest.read_bytes() == jpeg_bytes


# ---------------------------------------------------------------------------
# recover_cover_from_book
# ---------------------------------------------------------------------------


class TestRecoverCoverFromBook:
	"""Extracting a cover from the book file as the last-resort fallback.

	The mandatory generated-placeholder gate is the load-bearing piece: a C11
	book whose embedded cover is Calibre's own placeholder must not have that
	placeholder written back as cover.jpg.
	"""

	def test_valid_extract_is_written(self, tmp_path: Path) -> None:
		dest = tmp_path / "cover.jpg"
		with patch("book_meta_fix.covers.extract_cover_from_book", side_effect=_extractor_writing(_gradient_cover)):
			ok = recover_cover_from_book(tmp_path / "book.mobi", dest)
		assert ok is True
		assert dest.is_file()

	def test_generated_extract_is_rejected(self, tmp_path: Path) -> None:
		"""A solid-color (generated) extract never becomes cover.jpg."""
		dest = tmp_path / "cover.jpg"
		with patch("book_meta_fix.covers.extract_cover_from_book", side_effect=_extractor_writing(_solid_cover)):
			ok = recover_cover_from_book(tmp_path / "book.mobi", dest)
		assert ok is False
		assert not dest.exists()

	def test_generated_extract_does_not_overwrite_existing(self, tmp_path: Path) -> None:
		"""A real cover already at dest survives a generated extraction attempt —
		regression guard against replacing artwork with a placeholder."""
		dest = tmp_path / "cover.jpg"
		_gradient_cover(dest)
		original = dest.read_bytes()
		with patch("book_meta_fix.covers.extract_cover_from_book", side_effect=_extractor_writing(_solid_cover)):
			ok = recover_cover_from_book(tmp_path / "book.mobi", dest)
		assert ok is False
		assert dest.read_bytes() == original  # untouched

	def test_extraction_failure_returns_false(self, tmp_path: Path) -> None:
		"""calibre absent / no embedded cover (extract returns None) -> False."""
		dest = tmp_path / "cover.jpg"
		with patch("book_meta_fix.covers.extract_cover_from_book", return_value=None):
			ok = recover_cover_from_book(tmp_path / "book.mobi", dest)
		assert ok is False
		assert not dest.exists()

	def test_existing_dest_backed_up_on_success(self, tmp_path: Path) -> None:
		"""A successful overwrite mirrors download_cover: prior cover -> .bak."""
		dest = tmp_path / "cover.jpg"
		_solid_cover(dest)  # existing cover (will be replaced by the gradient)
		old = dest.read_bytes()
		with patch("book_meta_fix.covers.extract_cover_from_book", side_effect=_extractor_writing(_gradient_cover)):
			ok = recover_cover_from_book(tmp_path / "book.mobi", dest)
		assert ok is True
		bak = dest.with_suffix(".jpg.bak")
		assert bak.is_file()
		assert bak.read_bytes() == old

	def test_cross_device_move_still_succeeds(self, tmp_path: Path) -> None:
		"""Regression: moving the extracted cover across mounts must not crash.

		os.replace / os.rename fail with EXDEV (Errno 18, "Invalid cross-device
		link") when source and dest are on different filesystems — seen in
		production with the system /tmp on the local disk and the library on an
		NFS share. The temp is now created beside the dest, and the move falls
		back to a copy+unlink when a same-filesystem rename is impossible.
		"""
		import errno

		dest = tmp_path / "cover.jpg"

		def _cross_device(src, dst):
			raise OSError(errno.EXDEV, "Invalid cross-device link")

		with patch("book_meta_fix.covers.extract_cover_from_book", side_effect=_extractor_writing(_gradient_cover)), \
				patch("os.rename", side_effect=_cross_device):
			ok = recover_cover_from_book(tmp_path / "book.mobi", dest)
		assert ok is True
		assert dest.is_file()


# ---------------------------------------------------------------------------
# Detector rules
# ---------------------------------------------------------------------------


def _meta_with_path(folder: Path, **kwargs) -> BookMeta:
	"""Build a minimal BookMeta pointing at *folder*."""
	defaults = {"authors": ["Test Author"], "title": "Test Title", "path": str(folder)}
	defaults.update(kwargs)
	return BookMeta(**defaults)


class TestRuleGeneratedCover:
	def test_generated_cover_fires_c11(self, tmp_path: Path) -> None:
		from book_meta_fix.detectors import rule_generated_cover

		_solid_cover(tmp_path / "cover.jpg")
		meta = _meta_with_path(tmp_path)
		diag = rule_generated_cover(meta)
		assert diag is not None
		assert diag.category == "C11"
		assert diag.confidence == Confidence.HIGH
		assert "generated cover" in diag.reason

	def test_real_cover_does_not_fire(self, tmp_path: Path) -> None:
		from book_meta_fix.detectors import rule_generated_cover

		_gradient_cover(tmp_path / "cover.jpg")
		meta = _meta_with_path(tmp_path)
		diag = rule_generated_cover(meta)
		assert diag is None

	def test_no_cover_does_not_fire(self, tmp_path: Path) -> None:
		from book_meta_fix.detectors import rule_generated_cover

		meta = _meta_with_path(tmp_path)
		# No cover.jpg created
		diag = rule_generated_cover(meta)
		assert diag is None


class TestRuleMissingCover:
	def test_missing_cover_fires(self, tmp_path: Path) -> None:
		from book_meta_fix.detectors import rule_missing_cover

		meta = _meta_with_path(tmp_path)
		# No cover.jpg in folder
		diag = rule_missing_cover(meta)
		assert diag is not None
		assert diag.category == "MISSING_COVER"
		assert diag.verdict == Verdict.AUTO_FIXABLE

	def test_present_cover_does_not_fire(self, tmp_path: Path) -> None:
		from book_meta_fix.detectors import rule_missing_cover

		_solid_cover(tmp_path / "cover.jpg")
		meta = _meta_with_path(tmp_path)
		diag = rule_missing_cover(meta)
		assert diag is None


# ---------------------------------------------------------------------------
# strip_cover_from_book (embedded-cover removal, EPUB zip surgery)
# ---------------------------------------------------------------------------

_CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="{opf}" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

_OPF_EPUB2 = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Kniha</dc:title>
    <dc:identifier id="id">test-id</dc:identifier>
    <meta name="cover" content="cover-img"/>
  </metadata>
  <manifest>
    <item id="cover-img" href="{img}" media-type="image/jpeg"/>
    <item id="cover" href="{page}" media-type="application/xhtml+xml"/>
    <item id="chap1" href="{chap}" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="cover"/>
    <itemref idref="chap1"/>
  </spine>
  <guide><reference type="cover" title="Cover" href="{page}"/></guide>
</package>"""

_OPF_EPUB3 = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Kniha</dc:title>
    <dc:identifier id="id">test-id</dc:identifier>
  </metadata>
  <manifest>
    <item id="cvr" href="{img}" media-type="image/jpeg" properties="cover-image"/>
    <item id="chap1" href="{chap}" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="chap1"/></spine>
</package>"""

_OPF_NO_COVER = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Kniha</dc:title><dc:identifier id="id">test-id</dc:identifier>
  </metadata>
  <manifest><item id="chap1" href="chap.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="chap1"/></spine>
</package>"""


def _make_epub(path: Path, opf_tmpl: str = _OPF_EPUB2, opf_name: str = "content.opf",
               chapter: bytes = b"<html><body>text</body></html>",
               with_page: bool = True, cover_bytes: bytes | None = None) -> Path:
	"""Synthetic EPUB with the calibre-style cover chain (image + page + wiring).

	Content entries live next to the OPF (as in real books), i.e. under
	*opf_name*'s folder. The image bytes are arbitrary — strip_cover_from_book
	never parses the image, it only relocates zip entries and OPF elements.
	Pass *cover_bytes* (e.g. _solid_jpeg_bytes) to embed a REAL image; the
	probing path (probe_embedded_cover) analyses the pixels, so it needs a
	genuine JPEG. Set with_page=False for OPFs that reference no cover page
	(e.g. EPUB3).
	"""
	base = posixpath.dirname(opf_name)

	def p(name: str) -> str:
		return posixpath.join(base, name) if base else name

	with zipfile.ZipFile(path, "w") as zf:
		mimetype = zipfile.ZipInfo("mimetype")  # spec: first entry, STORED
		mimetype.compress_type = zipfile.ZIP_STORED
		zf.writestr(mimetype, "application/epub+zip")
		zf.writestr("META-INF/container.xml", _CONTAINER.format(opf=opf_name))
		zf.writestr(opf_name, opf_tmpl.format(
			img="images/cover.jpg", page="cover.xhtml", chap="chap.xhtml",
		))
		zf.writestr(p("images/cover.jpg"), cover_bytes if cover_bytes is not None else b"\xff\xd8fakejpeg")
		if with_page:
			zf.writestr(p("cover.xhtml"), b"<html><body><img src='images/cover.jpg'/></body></html>")
		zf.writestr(p("chap.xhtml"), chapter)
	return path


def _mimetype_is_stored(epub: Path) -> bool:
	with zipfile.ZipFile(epub) as zf:
		return zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED


class TestStripCover:
	def test_epub2_calibre_style_stripped(self, tmp_path: Path) -> None:
		epub = _make_epub(tmp_path / "b.epub")
		assert strip_cover_from_book(epub) is True
		with zipfile.ZipFile(epub) as zf:
			names = zf.namelist()
			opf = zf.read("content.opf").decode()
		assert names[0] == "mimetype"  # spec order preserved
		assert _mimetype_is_stored(epub)
		assert "images/cover.jpg" not in names
		assert "cover.xhtml" not in names
		assert "chap.xhtml" in names
		assert 'name="cover"' not in opf
		assert 'type="cover"' not in opf
		assert 'id="cover-img"' not in opf and 'id="cover"' not in opf
		assert 'id="chap1"' in opf and 'idref="chap1"' in opf

	def test_epub3_cover_image_property_stripped(self, tmp_path: Path) -> None:
		epub = _make_epub(tmp_path / "b.epub", opf_tmpl=_OPF_EPUB3, with_page=False)
		assert strip_cover_from_book(epub) is True
		with zipfile.ZipFile(epub) as zf:
			names = zf.namelist()
			opf = zf.read("content.opf").decode()
		assert "images/cover.jpg" not in names
		assert "cover-image" not in opf
		assert 'id="chap1"' in opf  # content untouched

	def test_image_referenced_inline_is_kept(self, tmp_path: Path) -> None:
		# The chapter embeds the cover image itself — deleting the file would
		# leave a dangling src, so only the cover STATUS is removed.
		chapter = b"<html><body><img src='images/cover.jpg'/></body></html>"
		epub = _make_epub(tmp_path / "b.epub", chapter=chapter)
		assert strip_cover_from_book(epub) is True
		with zipfile.ZipFile(epub) as zf:
			names = zf.namelist()
			opf = zf.read("content.opf").decode()
		assert "images/cover.jpg" in names
		assert 'id="cover-img"' in opf  # manifest item survives (referenced)
		assert 'name="cover"' not in opf
		assert 'type="cover"' not in opf
		assert "cover.xhtml" not in names  # the cover page still goes

	def test_opf_in_subfolder(self, tmp_path: Path) -> None:
		# OPF-relative hrefs must resolve to the OPF's own folder in the zip.
		epub = _make_epub(tmp_path / "b.epub", opf_name="OEBPS/content.opf")
		assert strip_cover_from_book(epub) is True
		with zipfile.ZipFile(epub) as zf:
			names = zf.namelist()
			opf = zf.read("OEBPS/content.opf").decode()
		assert "OEBPS/images/cover.jpg" not in names
		assert "OEBPS/cover.xhtml" not in names
		assert "OEBPS/chap.xhtml" in names
		assert 'name="cover"' not in opf

	def test_non_epub_rejected_untouched(self, tmp_path: Path) -> None:
		mobi = tmp_path / "b.mobi"
		raw = b"BOOKMOBI\x00\x01binary-not-a-zip"
		mobi.write_bytes(raw)
		assert strip_cover_from_book(mobi) is False
		assert mobi.read_bytes() == raw

	def test_corrupt_zip_rejected_untouched(self, tmp_path: Path) -> None:
		epub = tmp_path / "b.epub"
		raw = b"PK\x03\x04truncated garbage"
		epub.write_bytes(raw)
		assert strip_cover_from_book(epub) is False
		assert epub.read_bytes() == raw

	def test_no_cover_wiring_is_noop(self, tmp_path: Path) -> None:
		epub = _make_epub(tmp_path / "b.epub", opf_tmpl=_OPF_NO_COVER)
		before = epub.read_bytes()
		# chap-only zip still carries cover.jpg/cover.xhtml entries (helper
		# writes them unconditionally) but nothing marks them as the cover.
		assert strip_cover_from_book(epub) is False
		assert epub.read_bytes() == before

	def test_missing_container_rejected(self, tmp_path: Path) -> None:
		epub = tmp_path / "b.epub"
		with zipfile.ZipFile(epub, "w") as zf:
			zf.writestr("content.opf", _OPF_EPUB2)
		before = epub.read_bytes()
		assert strip_cover_from_book(epub) is False
		assert epub.read_bytes() == before


class TestEpubCoverImage:
	def test_epub2_returns_embedded_bytes(self, tmp_path: Path) -> None:
		epub = _make_epub(tmp_path / "b.epub")
		assert epub_cover_image(epub) == b"\xff\xd8fakejpeg"

	def test_epub3_property_returns_embedded_bytes(self, tmp_path: Path) -> None:
		epub = _make_epub(tmp_path / "b.epub", opf_tmpl=_OPF_EPUB3, with_page=False)
		assert epub_cover_image(epub) == b"\xff\xd8fakejpeg"

	def test_none_after_strip(self, tmp_path: Path) -> None:
		epub = _make_epub(tmp_path / "b.epub")
		strip_cover_from_book(epub)
		# ebook-meta --get-cover would still hand back a rendered page-1 here;
		# the zip probe must report the truth: no embedded cover remains.
		assert epub_cover_image(epub) is None

	def test_kept_inline_image_still_readable(self, tmp_path: Path) -> None:
		chapter = b"<html><body><img src='images/cover.jpg'/></body></html>"
		epub = _make_epub(tmp_path / "b.epub", chapter=chapter)
		strip_cover_from_book(epub)  # keeps the file, drops the cover status
		assert epub_cover_image(epub) is None

	def test_no_cover_wiring_returns_none(self, tmp_path: Path) -> None:
		epub = _make_epub(tmp_path / "b.epub", opf_tmpl=_OPF_NO_COVER)
		assert epub_cover_image(epub) is None

	def test_non_epub_returns_none(self, tmp_path: Path) -> None:
		mobi = tmp_path / "b.mobi"
		mobi.write_bytes(b"BOOKMOBI")
		assert epub_cover_image(mobi) is None

	def test_opf_in_subfolder(self, tmp_path: Path) -> None:
		epub = _make_epub(tmp_path / "b.epub", opf_name="OEBPS/content.opf")
		assert epub_cover_image(epub) == b"\xff\xd8fakejpeg"


class TestProbeEmbeddedCover:
	def test_generated_placeholder_detected(self, tmp_path: Path) -> None:
		epub = _make_epub(tmp_path / "b.epub", cover_bytes=_solid_jpeg_bytes())
		info = probe_embedded_cover(epub)
		assert info.is_generated is True
		assert (info.width, info.height) == (1200, 1600)

	def test_real_artwork_not_flagged(self, tmp_path: Path) -> None:
		epub = _make_epub(tmp_path / "b.epub", cover_bytes=_gradient_jpeg_bytes())
		assert probe_embedded_cover(epub).is_generated is False

	def test_non_epub_returns_empty_info(self, tmp_path: Path) -> None:
		mobi = tmp_path / "b.mobi"
		mobi.write_bytes(b"BOOKMOBI")
		info = probe_embedded_cover(mobi)
		assert info.is_generated is False
		assert info.width == 0

	def test_no_embedded_cover_returns_empty_info(self, tmp_path: Path) -> None:
		epub = _make_epub(tmp_path / "b.epub", opf_tmpl=_OPF_NO_COVER)
		info = probe_embedded_cover(epub)
		assert info.is_generated is False
		assert info.width == 0

	def test_undecodable_image_bytes_not_flagged(self, tmp_path: Path) -> None:
		# The default fake bytes are not a decodable image — analyze_cover's
		# never-raise contract must yield "not generated", not an exception.
		epub = _make_epub(tmp_path / "b.epub")
		assert probe_embedded_cover(epub).is_generated is False


class TestStripGeneratedCovers:
	def test_dry_run_reports_but_touches_nothing(self, tmp_path: Path) -> None:
		cover = tmp_path / "cover.jpg"
		_solid_cover(cover)
		epub = _make_epub(tmp_path / "b.epub", cover_bytes=_solid_jpeg_bytes())
		result = strip_generated_covers(tmp_path, dry_run=True)
		assert result.touched is True
		assert result.cover_bak is True
		assert result.stripped_epubs == ["b.epub"]
		assert cover.is_file()
		assert not (tmp_path / "cover.jpg.bak").exists()
		assert epub_cover_image(epub) is not None  # still embedded

	def test_apply_baks_sidecar_and_strips_epub(self, tmp_path: Path) -> None:
		cover = tmp_path / "cover.jpg"
		_solid_cover(cover)
		before = cover.read_bytes()
		epub = _make_epub(tmp_path / "b.epub", cover_bytes=_solid_jpeg_bytes())
		result = strip_generated_covers(tmp_path, dry_run=False)
		assert result.cover_bak is True
		assert result.stripped_epubs == ["b.epub"]
		assert not cover.exists()
		bak = tmp_path / "cover.jpg.bak"
		assert bak.is_file() and bak.read_bytes() == before  # reversible
		assert epub_cover_image(epub) is None  # cover no longer wired in

	def test_real_covers_untouched(self, tmp_path: Path) -> None:
		cover = tmp_path / "cover.jpg"
		_gradient_cover(cover)
		epub = _make_epub(tmp_path / "b.epub", cover_bytes=_gradient_jpeg_bytes())
		before_epub = epub.read_bytes()
		result = strip_generated_covers(tmp_path, dry_run=False)
		assert result.touched is False
		assert cover.is_file()
		assert epub.read_bytes() == before_epub

	def test_epub_only_stripped_when_no_sidecar(self, tmp_path: Path) -> None:
		epub = _make_epub(tmp_path / "b.epub", cover_bytes=_solid_jpeg_bytes())
		result = strip_generated_covers(tmp_path, dry_run=False)
		assert result.cover_bak is False
		assert result.stripped_epubs == ["b.epub"]
		assert epub_cover_image(epub) is None

	def test_existing_bak_overwritten(self, tmp_path: Path) -> None:
		cover = tmp_path / "cover.jpg"
		_solid_cover(cover)
		before = cover.read_bytes()
		bak = tmp_path / "cover.jpg.bak"
		bak.write_bytes(b"old junk")
		strip_generated_covers(tmp_path, dry_run=False)
		assert bak.read_bytes() == before

	def test_mobi_untouched(self, tmp_path: Path) -> None:
		# Non-EPUB formats are never opened for stripping; a generated sidecar
		# is still removed around them.
		cover = tmp_path / "cover.jpg"
		_solid_cover(cover)
		mobi = tmp_path / "b.mobi"
		raw = b"BOOKMOBI\x00\x01binary"
		mobi.write_bytes(raw)
		result = strip_generated_covers(tmp_path, dry_run=False)
		assert result.cover_bak is True
		assert result.stripped_epubs == []
		assert result.failed_epubs == []
		assert mobi.read_bytes() == raw

	def test_empty_folder_is_noop(self, tmp_path: Path) -> None:
		result = strip_generated_covers(tmp_path, dry_run=False)
		assert result.touched is False

	@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
	def test_write_failure_recorded_not_raised(self, tmp_path: Path) -> None:
		# The probe sees a generated cover but the strip cannot write (the
		# folder is read-only) — recorded in failed_epubs, never raised.
		_make_epub(tmp_path / "b.epub", cover_bytes=_solid_jpeg_bytes())
		tmp_path.chmod(0o555)
		try:
			result = strip_generated_covers(tmp_path, dry_run=False)
		finally:
			tmp_path.chmod(0o755)
		assert result.failed_epubs == ["b.epub"]
		assert result.stripped_epubs == []


class TestImageIsReadable:
	"""The invalid-cover validity test (Pillow full decode, bytes or path)."""

	def test_valid_file_and_bytes(self, tmp_path: Path) -> None:
		cover = tmp_path / "cover.jpg"
		_gradient_cover(cover)
		assert image_is_readable(cover) is True
		assert image_is_readable(_gradient_jpeg_bytes()) is True

	def test_html_bytes_rejected(self) -> None:
		assert image_is_readable(_HTML_BYTES) is False

	def test_html_file_rejected(self, tmp_path: Path) -> None:
		p = tmp_path / "cover.html"
		p.write_bytes(_HTML_BYTES)
		assert image_is_readable(p) is False

	def test_truncated_jpeg_rejected(self) -> None:
		# Headers intact but pixel data cut in half — Image.open succeeds, only
		# the forced full decode (img.load) catches it.
		data = _gradient_jpeg_bytes()
		assert image_is_readable(data[: len(data) // 2]) is False

	def test_empty_bytes_rejected(self) -> None:
		assert image_is_readable(b"") is False


class TestStripInvalidCovers:
	def test_invalid_sidecar_files_baked(self, tmp_path: Path) -> None:
		# The ABS failure mode: cover.html picked by name, an HTML error page
		# saved as .jpg picked by extension. Valid artwork must stay.
		cover = tmp_path / "cover.jpg"
		_gradient_cover(cover)
		(tmp_path / "cover.html").write_bytes(_HTML_BYTES)
		(tmp_path / "ilustra.jpg").write_bytes(_HTML_BYTES)
		before = cover.read_bytes()
		result = strip_generated_covers(tmp_path, dry_run=False, generated=None, invalid="external")
		assert result.invalid_baks == ["cover.html", "ilustra.jpg"]  # sorted
		assert cover.is_file() and cover.read_bytes() == before
		assert (tmp_path / "cover.html.bak").read_bytes() == _HTML_BYTES
		assert (tmp_path / "ilustra.jpg.bak").read_bytes() == _HTML_BYTES

	def test_image_extension_set_mirrors_abs(self, tmp_path: Path) -> None:
		# png/webp junk is picked by ABS too; gif is NOT in ABS's list and has
		# no cover.* name, so it stays even though it is not an image.
		(tmp_path / "a.png").write_bytes(_HTML_BYTES)
		(tmp_path / "b.webp").write_bytes(_HTML_BYTES)
		(tmp_path / "c.gif").write_bytes(_HTML_BYTES)
		result = strip_generated_covers(tmp_path, dry_run=False, generated=None, invalid="external")
		assert result.invalid_baks == ["a.png", "b.webp"]
		assert (tmp_path / "c.gif").is_file()

	def test_any_cover_name_extension_covers_cover_html(self, tmp_path: Path) -> None:
		# The name rule is extension-agnostic (cover.html was in the ABS log);
		# a bare "cover" with no extension is NOT an ABS candidate and stays.
		(tmp_path / "cover.html").write_bytes(_HTML_BYTES)
		(tmp_path / "cover").write_bytes(_HTML_BYTES)
		result = strip_generated_covers(tmp_path, dry_run=False, generated=None, invalid="external")
		assert result.invalid_baks == ["cover.html"]
		assert (tmp_path / "cover").is_file()

	def test_bak_and_tmp_and_metadata_untouched(self, tmp_path: Path) -> None:
		(tmp_path / "cover.jpg.bak").write_bytes(_HTML_BYTES)
		(tmp_path / "cover.jpg.tmp").write_bytes(_HTML_BYTES)
		(tmp_path / "metadata.json").write_bytes(_HTML_BYTES)
		result = strip_generated_covers(tmp_path, dry_run=False, generated=None, invalid="both")
		assert result.touched is False
		for name in ("cover.jpg.bak", "cover.jpg.tmp", "metadata.json"):
			assert (tmp_path / name).is_file()

	def test_invalid_epub_cover_stripped(self, tmp_path: Path) -> None:
		# OPF wires media-type image/jpeg but the bytes are HTML — exactly what
		# makes ffmpeg fail inside ABS. The strip surgery is content-agnostic.
		epub = _make_epub(tmp_path / "b.epub", cover_bytes=_HTML_BYTES)
		result = strip_generated_covers(tmp_path, dry_run=False, generated=None, invalid="embedded")
		assert result.invalid_epubs == ["b.epub"]
		assert result.stripped_epubs == []
		assert epub_cover_image(epub) is None

	def test_invalid_cover_undecodable_not_generated(self, tmp_path: Path) -> None:
		# Default selectors: generated only — an undecodable cover is NOT the
		# generated class (analyze_cover cannot read pixels), so it stays.
		(tmp_path / "cover.html").write_bytes(_HTML_BYTES)
		epub = _make_epub(tmp_path / "b.epub", cover_bytes=_HTML_BYTES)
		before = epub.read_bytes()
		result = strip_generated_covers(tmp_path, dry_run=False)
		assert result.touched is False
		assert (tmp_path / "cover.html").is_file()
		assert epub.read_bytes() == before

	def test_dry_run_reports_invalid_but_touches_nothing(self, tmp_path: Path) -> None:
		(tmp_path / "cover.html").write_bytes(_HTML_BYTES)
		epub = _make_epub(tmp_path / "b.epub", cover_bytes=_HTML_BYTES)
		result = strip_generated_covers(tmp_path, dry_run=True, generated=None, invalid="both")
		assert result.invalid_baks == ["cover.html"]
		assert result.invalid_epubs == ["b.epub"]
		assert (tmp_path / "cover.html").is_file()
		assert not (tmp_path / "cover.html.bak").exists()
		assert epub_cover_image(epub) is not None

	def test_combined_run_generated_and_invalid(self, tmp_path: Path) -> None:
		cover = tmp_path / "cover.jpg"
		_solid_cover(cover)
		(tmp_path / "cover.html").write_bytes(_HTML_BYTES)
		good = _make_epub(tmp_path / "good.epub", cover_bytes=_gradient_jpeg_bytes())
		bad = _make_epub(tmp_path / "bad.epub", cover_bytes=_HTML_BYTES)
		result = strip_generated_covers(tmp_path, dry_run=False, generated="both", invalid="both")
		assert result.cover_bak is True
		assert result.invalid_baks == ["cover.html"]
		assert result.stripped_epubs == []  # good.epub's real artwork survives
		assert result.invalid_epubs == ["bad.epub"]
		assert epub_cover_image(good) is not None
		assert epub_cover_image(bad) is None


class TestStripScopeGating:
	"""external/embedded scopes gate each selector independently."""

	def _folder(self, tmp_path: Path) -> None:
		_solid_cover(tmp_path / "cover.jpg")  # generated sidecar
		_make_epub(tmp_path / "g.epub", cover_bytes=_solid_jpeg_bytes())  # generated embedded
		(tmp_path / "cover.html").write_bytes(_HTML_BYTES)  # invalid sidecar
		_make_epub(tmp_path / "i.epub", cover_bytes=_HTML_BYTES)  # invalid embedded

	def test_generated_external_only(self, tmp_path: Path) -> None:
		self._folder(tmp_path)
		result = strip_generated_covers(tmp_path, dry_run=False, generated="external")
		assert result.cover_bak is True
		assert result.stripped_epubs == []
		assert result.invalid_baks == []
		assert result.invalid_epubs == []
		assert epub_cover_image(tmp_path / "g.epub") is not None
		assert (tmp_path / "cover.html").is_file()

	def test_generated_embedded_only(self, tmp_path: Path) -> None:
		self._folder(tmp_path)
		result = strip_generated_covers(tmp_path, dry_run=False, generated="embedded")
		assert result.cover_bak is False
		assert (tmp_path / "cover.jpg").is_file()
		assert result.stripped_epubs == ["g.epub"]

	def test_invalid_external_only(self, tmp_path: Path) -> None:
		self._folder(tmp_path)
		result = strip_generated_covers(tmp_path, dry_run=False, generated=None, invalid="external")
		assert result.invalid_baks == ["cover.html"]
		assert (tmp_path / "cover.jpg").is_file()  # generated stays out of scope
		assert result.invalid_epubs == []
		assert epub_cover_image(tmp_path / "i.epub") is not None

	def test_invalid_embedded_only(self, tmp_path: Path) -> None:
		self._folder(tmp_path)
		result = strip_generated_covers(tmp_path, dry_run=False, generated=None, invalid="embedded")
		assert result.invalid_epubs == ["i.epub"]
		assert (tmp_path / "cover.html").is_file()
		assert (tmp_path / "cover.jpg").is_file()


class TestStripSmallCovers:
	"""min_size selector: real-but-small EXTERNAL covers .bak'd, rest kept."""

	def test_small_sidecar_baked_with_size_recorded(self, tmp_path: Path) -> None:
		cover = tmp_path / "cover.jpg"
		_gradient_cover(cover, size=(120, 180))
		before = cover.read_bytes()
		result = strip_generated_covers(tmp_path, dry_run=False, generated=None, invalid=None, min_size=300)
		assert result.small_baks == ["cover.jpg"]
		assert result.small_sizes == {"cover.jpg": (120, 180)}
		assert result.touched is True
		assert not cover.exists()
		bak = tmp_path / "cover.jpg.bak"
		assert bak.is_file() and bak.read_bytes() == before  # reversible

	def test_shorter_side_is_the_measure(self, tmp_path: Path) -> None:
		# 400x250: shorter side 250 < 300 -> flagged; 300x450: 300 >= 300 -> kept.
		small = tmp_path / "a.jpg"
		_gradient_cover(small, size=(400, 250))
		ok = tmp_path / "b.jpg"
		_gradient_cover(ok, size=(300, 450))
		result = strip_generated_covers(tmp_path, dry_run=False, generated=None, invalid=None, min_size=300)
		assert result.small_baks == ["a.jpg"]
		assert ok.is_file()

	def test_dry_run_reports_without_touching(self, tmp_path: Path) -> None:
		cover = tmp_path / "cover.jpg"
		_gradient_cover(cover, size=(120, 180))
		result = strip_generated_covers(tmp_path, dry_run=True, generated=None, invalid=None, min_size=300)
		assert result.small_baks == ["cover.jpg"]
		assert result.small_sizes == {"cover.jpg": (120, 180)}
		assert cover.is_file()
		assert not (tmp_path / "cover.jpg.bak").exists()

	def test_off_by_default(self, tmp_path: Path) -> None:
		cover = tmp_path / "cover.jpg"
		_gradient_cover(cover, size=(120, 180))
		result = strip_generated_covers(tmp_path, dry_run=False, generated=None, invalid=None)
		assert result.small_baks == []
		assert result.touched is False
		assert cover.is_file()

	def test_undecodable_not_judged_small(self, tmp_path: Path) -> None:
		# No readable header -> no size to judge; the invalid selector owns it.
		(tmp_path / "cover.jpg").write_bytes(_HTML_BYTES)
		result = strip_generated_covers(tmp_path, dry_run=False, generated=None, invalid=None, min_size=300)
		assert result.small_baks == []
		assert (tmp_path / "cover.jpg").is_file()

	def test_bak_and_tmp_untouched(self, tmp_path: Path) -> None:
		bak = tmp_path / "cover.jpg.bak"
		bak.write_bytes(_gradient_jpeg_bytes(size=(120, 180)))
		tmp = tmp_path / "other.jpg.tmp"
		tmp.write_bytes(_gradient_jpeg_bytes(size=(120, 180)))
		result = strip_generated_covers(tmp_path, dry_run=False, generated=None, invalid=None, min_size=300)
		assert result.small_baks == []
		assert bak.is_file() and tmp.is_file()

	def test_embedded_small_epub_cover_kept(self, tmp_path: Path) -> None:
		# The embedded cover is the recovery FALLBACK for the re-fetch —
		# min_size deliberately never strips it (external-only selector).
		epub = _make_epub(tmp_path / "b.epub", cover_bytes=_gradient_jpeg_bytes(size=(120, 180)))
		result = strip_generated_covers(tmp_path, dry_run=False, generated=None, invalid=None, min_size=300)
		assert result.touched is False
		assert epub_cover_image(epub) is not None

	def test_generated_sidecar_not_double_reported(self, tmp_path: Path) -> None:
		# A tiny solid cover fires the generated pass first; under dry-run the
		# file is still present and small must not claim it a second time.
		cover = tmp_path / "cover.jpg"
		_solid_cover(cover, size=(100, 150))
		result = strip_generated_covers(tmp_path, dry_run=True, min_size=300)
		assert result.cover_bak is True
		assert result.small_baks == []


class TestCoverAnalysisCache:
	"""analyze_cover memoizes its verdict per (path, mtime_ns, size) and — with
	a persistent store attached — carries it across the in-run memo. One
	analyze run asks for the same cover up to 4x (OK-filter, worker detect,
	two review-writer projections); unchanged covers must decode once."""

	def setup_method(self) -> None:
		from book_meta_fix import covers as covers_mod

		covers_mod.clear_cover_cache()
		covers_mod.set_cover_cache(None)

	def teardown_method(self) -> None:
		from book_meta_fix import covers as covers_mod

		covers_mod.clear_cover_cache()
		covers_mod.set_cover_cache(None)

	@staticmethod
	def _counting_decode(monkeypatch):
		"""Patch the uncached core with a call counter; returns the counter."""
		from book_meta_fix import covers as covers_mod

		calls = {"n": 0}
		orig = covers_mod._analyze_cover_uncached

		def counting(p):
			calls["n"] += 1
			return orig(p)

		monkeypatch.setattr(covers_mod, "_analyze_cover_uncached", counting)
		return calls

	def test_second_call_hits_memo(self, tmp_path: Path, monkeypatch) -> None:
		cover = tmp_path / "cover.jpg"
		_solid_cover(cover)
		calls = self._counting_decode(monkeypatch)
		a = analyze_cover(cover)
		b = analyze_cover(cover)
		assert calls["n"] == 1
		assert (a.width, a.height, a.is_generated) == (b.width, b.height, b.is_generated)

	def test_mtime_change_recomputes(self, tmp_path: Path, monkeypatch) -> None:
		import os

		cover = tmp_path / "cover.jpg"
		_solid_cover(cover)
		calls = self._counting_decode(monkeypatch)
		analyze_cover(cover)
		st = cover.stat()
		os.utime(cover, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
		b = analyze_cover(cover)
		assert calls["n"] == 2
		assert b.width != 0  # genuinely re-analyzed

	def test_content_change_new_size_recomputes(self, tmp_path: Path, monkeypatch) -> None:
		cover = tmp_path / "cover.jpg"
		_solid_cover(cover)
		calls = self._counting_decode(monkeypatch)
		analyze_cover(cover)
		_solid_cover(cover, size=(600, 800))  # different bytes -> different size
		analyze_cover(cover)
		assert calls["n"] == 2

	def test_returned_info_is_a_copy(self, tmp_path: Path) -> None:
		# Mutating a returned CoverInfo (its signals list) must not poison the
		# cached verdict other callers receive.
		cover = tmp_path / "cover.jpg"
		_solid_cover(cover)
		a = analyze_cover(cover)
		a.signals.append("poison")
		b = analyze_cover(cover)
		assert "poison" not in b.signals

	def test_persistent_store_serves_across_memo_clear(self, tmp_path: Path, monkeypatch) -> None:
		"""With a library.Cache attached, a verdict survives even after the
		in-run memo is dropped (the cross-run case)."""
		from book_meta_fix import covers as covers_mod
		from book_meta_fix.library import Cache

		cover = tmp_path / "cover.jpg"
		_solid_cover(cover)
		calls = self._counting_decode(monkeypatch)
		cache = Cache(tmp_path / "cache.db")
		try:
			covers_mod.set_cover_cache(cache)
			first = analyze_cover(cover)
			assert calls["n"] == 1
			# Simulate a fresh process: memo empty, SQLite warm.
			covers_mod.clear_cover_cache()
			second = analyze_cover(cover)
			assert calls["n"] == 1  # served from the covers table, no decode
			assert second.is_generated == first.is_generated
			assert second.width == first.width
		finally:
			covers_mod.set_cover_cache(None)
			cache.close()

	def test_failed_decode_never_cached(self, tmp_path: Path, monkeypatch) -> None:
		"""A transient read failure (width == 0) must not freeze the verdict:
		the next call decodes again."""
		cover = tmp_path / "cover.jpg"
		cover.write_text("<html>not an image</html>", encoding="utf-8")
		calls = self._counting_decode(monkeypatch)
		assert analyze_cover(cover).width == 0
		assert analyze_cover(cover).width == 0
		assert calls["n"] == 2

	def test_stale_version_verdict_recomputes(self, tmp_path: Path, monkeypatch) -> None:
		"""A "real" verdict persisted under an older heuristic version must not
		stay frozen — the v4 bump exists exactly for the calibre-marked covers
		measured as "real" under v3 (their marker went unread)."""
		from book_meta_fix import covers as covers_mod
		from book_meta_fix.library import Cache

		cover = tmp_path / "cover.jpg"
		cover.write_bytes(_calibre_marked_jpeg_bytes())
		st = cover.stat()
		calls = self._counting_decode(monkeypatch)
		cache = Cache(tmp_path / "cache.db")
		try:
			covers_mod.set_cover_cache(cache)
			cache.put_cover(str(cover), st.st_mtime_ns, st.st_size, {
				"v": 3, "width": 1200, "height": 1600,
				"is_generated": False, "confidence": 0.2, "signals": ["dominant_bg (63%)"],
			})
			info = analyze_cover(cover)
			assert calls["n"] == 1  # the v3 row was a miss, not a hit
			assert info.is_generated is True
			assert any(s.startswith("calibre_comment") for s in info.signals)
		finally:
			covers_mod.set_cover_cache(None)
			cache.close()


class TestTextPageSignal:
	"""Signal 4: calibre's page-1 render (white sheet of text lines).

	The colour signals are blind to it — antialiased ink smears over 6+ grey
	buckets — so the page SHAPE is measured: near-white, colourless, many
	separated ink BANDS. Calibrated against the real library: minimalist real
	covers sit at 1-9 bands (Susanna Clarke's cream cover = 9), text scans
	start at 11.
	"""

	def test_text_page_render_is_generated(self, tmp_path: Path) -> None:
		cover = tmp_path / "cover.jpg"
		_text_page_cover(cover)
		info = analyze_cover(cover)
		assert info.is_generated is True
		assert any(s.startswith("text_page") for s in info.signals)

	def test_minimalist_white_cover_gets_no_text_page_signal(self, tmp_path: Path) -> None:
		cover = tmp_path / "cover.jpg"
		_minimalist_white_cover(cover)
		info = analyze_cover(cover)
		assert not any(s.startswith("text_page") for s in info.signals)

	def test_real_artwork_untouched(self, tmp_path: Path) -> None:
		cover = tmp_path / "cover.jpg"
		_gradient_cover(cover)
		info = analyze_cover(cover)
		assert info.is_generated is False
		assert info.signals == []


class TestAnalyzeCoverBytes:
	def test_gradient_bytes_classified_not_generated(self) -> None:
		from book_meta_fix.covers import analyze_cover_bytes

		info = analyze_cover_bytes(_gradient_jpeg_bytes(size=(300, 400)))
		assert info.width == 300 and info.height == 400
		assert info.is_generated is False

	def test_garbage_bytes_yield_empty_info(self) -> None:
		from book_meta_fix.covers import analyze_cover_bytes

		info = analyze_cover_bytes(b"not an image at all")
		assert info.width == 0
		assert info.is_generated is False

	def test_text_page_bytes_rejected(self) -> None:
		"""The bytes gate used by download_cover and the EPUB recovery path."""
		from book_meta_fix.covers import analyze_cover_bytes

		buf = io.BytesIO()
		img = Image.new("RGB", (600, 900), color=(250, 250, 250))
		px = img.load()
		y = 60
		while y + 8 < 860:
			for dy in range(8):
				for x in range(60, 540):
					px[x, y + dy] = (40, 40, 40)
			y += 26
		img.save(buf, format="JPEG")
		info = analyze_cover_bytes(buf.getvalue())
		assert info.is_generated is True
		assert any(s.startswith("text_page") for s in info.signals)


class TestVendorPlaceholder:
	"""databazeknih's no-cover branding image (light-gray "D" sheet): refused
	by URL at every entry point, and byte-identified by the hash registry
	(seeds + hashes learned from the live URL, see TestPlaceholderRegistry-
	Refresh) so already-downloaded copies flag as generated. The hash tier is
	load-bearing: the former 38050 B generation is INVISIBLE to the pixel
	math (JPEG noise spreads it over 6+ quantized buckets)."""

	def setup_method(self) -> None:
		from book_meta_fix import covers as covers_mod

		covers_mod.clear_cover_cache()
		covers_mod.set_cover_cache(None)

	def teardown_method(self) -> None:
		from book_meta_fix import covers as covers_mod

		covers_mod.clear_cover_cache()
		covers_mod.set_cover_cache(None)

	def test_url_filter(self) -> None:
		from book_meta_fix.covers import is_placeholder_cover_url

		assert is_placeholder_cover_url("https://www.databazeknih.cz/img/books/empty_bmid.jpg")
		assert is_placeholder_cover_url("HTTP://WWW.DATABAZEKNIH.CZ/IMG/BOOKS/EMPTY_BMID.JPG")
		assert not is_placeholder_cover_url("https://www.databazeknih.cz/img/books/28_/283/bmid_1984.png")

	def test_seed_hashes_flag_without_store(self) -> None:
		"""Both measured generations are known offline — no store attached."""
		from book_meta_fix import covers as covers_mod

		known = covers_mod._known_placeholder_md5s()
		assert "bbc424ed9a848cd409f5294c45000d9e" in known  # current, 3625 B
		assert "a5b62142ebda74fa3f96e9064ff072d5" in known  # former, 38050 B

	def test_learned_hash_flags_bytes_and_file(self, tmp_path: Path) -> None:
		"""A colour-rich gradient (pixel math says NOT generated) whose md5 was
		learned into the persistent store flags via the hash alone — both the
		bytes gate and the path-based analyzer."""
		import hashlib

		from book_meta_fix import covers as covers_mod
		from book_meta_fix.covers import analyze_cover, analyze_cover_bytes
		from book_meta_fix.library import Cache

		data = _gradient_jpeg_bytes(size=(300, 460))
		md5 = hashlib.md5(data).hexdigest()
		assert analyze_cover_bytes(data).is_generated is False  # baseline: pixel-clean
		cache = Cache(tmp_path / "cache.db")
		try:
			covers_mod.set_cover_cache(cache)
			cache.add_placeholder_md5(md5)
			covers_mod.clear_cover_cache()  # drop the memo, reload from the store
			info = analyze_cover_bytes(data)
			assert info.is_generated is True
			assert info.confidence == 1.0
			assert any(s.startswith("vendor_placeholder") for s in info.signals)
			cover = tmp_path / "cover.jpg"
			cover.write_bytes(data)
			assert analyze_cover(cover).is_generated is True
		finally:
			covers_mod.set_cover_cache(None)
			cache.close()

	def test_download_refuses_placeholder_url_without_network(self, tmp_path: Path) -> None:
		dest = tmp_path / "cover.jpg"

		def _must_not_fetch(url, *args, **kwargs):
			raise AssertionError(f"network must not be touched for a placeholder URL: {url}")

		with patch("requests.get", _must_not_fetch):
			ok = download_cover("https://www.databazeknih.cz/img/books/empty_bmid.jpg", dest)
		assert ok is False
		assert not dest.exists()

	def test_download_refuses_learned_placeholder_bytes(self, tmp_path: Path) -> None:
		"""Byte-level gate: the placeholder arriving under an UNrecognized URL
		(a mirror/proxy path) is discarded by its learned hash."""
		import hashlib

		from book_meta_fix import covers as covers_mod
		from book_meta_fix.library import Cache

		data = _gradient_jpeg_bytes(size=(300, 460))
		md5 = hashlib.md5(data).hexdigest()
		cache = Cache(tmp_path / "cache.db")
		try:
			covers_mod.set_cover_cache(cache)
			cache.add_placeholder_md5(md5)
			mock_response = type("R", (), {"status_code": 200, "content": data})
			dest = tmp_path / "cover.jpg"
			with patch("requests.get", return_value=mock_response):
				ok = download_cover("https://mirror.example.com/img/empty.jpg", dest)
			assert ok is False
			assert not dest.exists()
		finally:
			covers_mod.set_cover_cache(None)
			cache.close()


class TestPlaceholderRegistryRefresh:
	"""refresh_placeholder_registry re-learns the CURRENT bytes behind the
	known placeholder URLs once per process — databazeknih swaps its branding
	image from time to time, and a stale hash registry would miss on-disk
	copies of the new generation."""

	def setup_method(self) -> None:
		from book_meta_fix import covers as covers_mod

		covers_mod.clear_cover_cache()
		covers_mod.set_cover_cache(None)

	def teardown_method(self) -> None:
		from book_meta_fix import covers as covers_mod

		covers_mod.clear_cover_cache()
		covers_mod.set_cover_cache(None)

	def test_learns_new_generation_once_per_process(self, tmp_path: Path) -> None:
		import hashlib

		from book_meta_fix import covers as covers_mod
		from book_meta_fix.covers import analyze_cover_bytes, refresh_placeholder_registry
		from book_meta_fix.library import Cache

		new_gen = _gradient_jpeg_bytes(size=(250, 360))
		md5 = hashlib.md5(new_gen).hexdigest()
		mock_response = type("R", (), {"status_code": 200, "content": new_gen})
		calls: list[str] = []
		cache = Cache(tmp_path / "cache.db")
		try:
			covers_mod.set_cover_cache(cache)
			with patch("requests.get", side_effect=lambda url, *a, **k: (calls.append(url), mock_response)[1]):
				refresh_placeholder_registry()
				refresh_placeholder_registry()  # second call is a no-op
			assert len(calls) == len(covers_mod._PLACEHOLDER_COVER_URLS)
			# Persisted for later runs AND live in this run's registry.
			assert md5 in cache.get_placeholder_md5s()
			assert analyze_cover_bytes(new_gen).is_generated is True
		finally:
			covers_mod.set_cover_cache(None)
			cache.close()

	def test_non_image_content_not_learned(self, tmp_path: Path) -> None:
		"""An error page or redirect body under the placeholder URL must not
		enter the registry (it would flag whatever book shares its hash)."""
		from book_meta_fix import covers as covers_mod
		from book_meta_fix.covers import refresh_placeholder_registry
		from book_meta_fix.library import Cache

		mock_response = type("R", (), {"status_code": 200, "content": b"<html>error page</html>"})
		cache = Cache(tmp_path / "cache.db")
		try:
			covers_mod.set_cover_cache(cache)
			with patch("requests.get", return_value=mock_response):
				refresh_placeholder_registry()
			assert cache.get_placeholder_md5s() == set()
		finally:
			covers_mod.set_cover_cache(None)
			cache.close()

	def test_offline_refresh_keeps_seeds(self) -> None:
		import requests as requests_mod

		from book_meta_fix import covers as covers_mod
		from book_meta_fix.covers import refresh_placeholder_registry

		def _offline(*args, **kwargs):
			raise requests_mod.RequestException("offline")

		with patch("requests.get", _offline):
			refresh_placeholder_registry()  # must not raise
		assert covers_mod._PLACEHOLDER_COVER_MD5 <= covers_mod._known_placeholder_md5s()


class TestCoverCacheVersion:
	"""A stored verdict from an OLDER heuristic version is a cache miss.

	Without the version stamp, a detector change would leave every pre-change
	"not generated" verdict frozen forever — unchanged files never re-analyze.
	"""

	def test_old_version_payload_recomputed(self, tmp_path: Path, monkeypatch) -> None:
		from book_meta_fix import covers as covers_mod
		from book_meta_fix.library import Cache

		cover = tmp_path / "cover.jpg"
		_text_page_cover(cover)
		calls = self._counting_decode(monkeypatch)
		cache = Cache(tmp_path / "cache.db")
		try:
			covers_mod.set_cover_cache(cache)
			assert analyze_cover(cover).is_generated is True
			assert calls["n"] == 1
			# Forge a stale row WITHOUT the heuristic version key, as every
			# pre-change row in the wild looks, then force a fresh-process read.
			st = cover.stat()
			cache.put_cover(str(cover), st.st_mtime_ns, st.st_size, {
				"width": 600, "height": 900, "is_generated": False,
				"confidence": 0.0, "signals": [],
			})
			covers_mod.clear_cover_cache()
			second = analyze_cover(cover)
			assert calls["n"] == 2  # the stale row did NOT serve
			assert second.is_generated is True
		finally:
			covers_mod.set_cover_cache(None)
			cache.close()

	def _counting_decode(self, monkeypatch) -> dict:  # noqa: ANN001
		from book_meta_fix import covers as covers_mod

		calls = {"n": 0}
		orig = covers_mod._analyze_cover_uncached

		def counted(path):
			calls["n"] += 1
			return orig(path)

		monkeypatch.setattr(covers_mod, "_analyze_cover_uncached", counted)
		return calls


class TestRecoverEpubFirst:
	"""The EPUB recovery path reads OPF-wired bytes and never renders page 1.

	ebook-meta --get-cover fabricates a "default cover" (a 1240x1752 page
	render) for a coverless EPUB — the measured producer of the library's
	screenshot covers. A wired cover is the truth; an EPUB without one has
	nothing to extract.
	"""

	def test_epub_with_real_wired_cover_written(self, tmp_path: Path) -> None:
		epub = _make_epub(tmp_path / "b.epub", cover_bytes=_gradient_jpeg_bytes())
		dest = tmp_path / "cover.jpg"
		with patch("book_meta_fix.covers.extract_cover_from_book") as ex:
			assert recover_cover_from_book(epub, dest) is True
		ex.assert_not_called()  # wired bytes, no calibre subprocess
		assert dest.is_file()
		assert analyze_cover(dest).is_generated is False

	def test_epub_without_wired_cover_returns_false(self, tmp_path: Path) -> None:
		"""No wired cover → nothing to extract — ebook-meta's page render must
		NOT be produced as a 'cover'."""
		epub = _make_epub(tmp_path / "b.epub", opf_tmpl=_OPF_NO_COVER)
		dest = tmp_path / "cover.jpg"
		with patch("book_meta_fix.covers.extract_cover_from_book") as ex:
			assert recover_cover_from_book(epub, dest) is False
		ex.assert_not_called()
		assert not dest.exists()

	def test_non_epub_still_uses_calibre_gate(self, tmp_path: Path) -> None:
		"""Non-EPUB formats keep the ebook-meta path, gated by the pixel math."""
		dest = tmp_path / "cover.jpg"
		with patch("book_meta_fix.covers.extract_cover_from_book", side_effect=_extractor_writing(_gradient_cover)):
			assert recover_cover_from_book(tmp_path / "book.mobi", dest) is True
		assert dest.is_file()


class TestZeroByteExtractRejected:
	"""calibre may exit 0 writing NOTHING; mkstemp pre-created the empty file,
	so the old is_file() check passed it and 746 zero-byte cover.jpg files
	landed in the library (masking MISSING_COVER forever)."""

	def test_empty_extract_is_rejected(self, tmp_path: Path) -> None:
		dest = tmp_path / "cover.jpg"

		def _empty_extractor(book_path, dest=None):  # noqa: ANN001
			Path(dest).write_bytes(b"")
			return Path(dest)

		with patch("book_meta_fix.covers.extract_cover_from_book", side_effect=_empty_extractor):
			assert recover_cover_from_book(tmp_path / "book.mobi", dest) is False
		assert not dest.exists()

	def test_extract_cover_from_book_rejects_empty_output(self, tmp_path: Path) -> None:
		"""The primitive itself: exit 0 + empty pre-created dest → None."""
		from book_meta_fix.covers import extract_cover_from_book

		out = tmp_path / "out.jpg"
		out.write_bytes(b"")  # pre-created by the caller's mkstemp

		class _Proc:
			returncode = 0

		with patch("book_meta_fix.covers.subprocess.run", return_value=_Proc()), \
				patch("book_meta_fix.covers.shutil.which", return_value="/usr/bin/ebook-meta"):
			assert extract_cover_from_book(tmp_path / "b.mobi", dest=out) is None


class TestDownloadCoverTextPageGate:
	def test_text_page_download_rejected(self, tmp_path: Path) -> None:
		"""Only real covers: a downloaded page-1 render is refused."""
		buf = io.BytesIO()
		img = Image.new("RGB", (600, 900), color=(250, 250, 250))
		px = img.load()
		y = 60
		while y + 8 < 860:
			for dy in range(8):
				for x in range(60, 540):
					px[x, y + dy] = (40, 40, 40)
			y += 26
		img.save(buf, format="JPEG")

		mock_response = type("R", (), {"status_code": 200, "content": buf.getvalue()})
		dest = tmp_path / "cover.jpg"
		with patch("requests.get", return_value=mock_response):
			assert download_cover("https://example.com/cover.jpg", dest) is False
		assert not dest.exists()

	def test_minimalist_solid_download_accepted(self, tmp_path: Path) -> None:
		"""A minimalist (few-colours) real cover from the source is ACCEPTED —
		the download gate refuses only text-page renders, not sparse artwork
		(the book's own cover beats none)."""
		buf = io.BytesIO()
		Image.new("RGB", (300, 450), color=(240, 238, 230)).save(buf, format="JPEG")
		mock_response = type("R", (), {"status_code": 200, "content": buf.getvalue()})
		dest = tmp_path / "cover.jpg"
		with patch("requests.get", return_value=mock_response):
			assert download_cover("https://example.com/cover.jpg", dest) is True
		assert dest.is_file()


class TestSidecarCoverUsable:
	def test_zero_byte_cover_is_not_usable(self, tmp_path: Path) -> None:
		from book_meta_fix.covers import sidecar_cover_usable

		cover = tmp_path / "cover.jpg"
		cover.write_bytes(b"")
		assert sidecar_cover_usable(cover) is False

	def test_undecodable_cover_is_not_usable(self, tmp_path: Path) -> None:
		from book_meta_fix.covers import sidecar_cover_usable

		cover = tmp_path / "cover.jpg"
		cover.write_bytes(b"not an image")
		assert sidecar_cover_usable(cover) is False

	def test_real_cover_is_usable(self, tmp_path: Path) -> None:
		from book_meta_fix.covers import sidecar_cover_usable

		cover = tmp_path / "cover.jpg"
		_real_cover(cover)
		assert sidecar_cover_usable(cover) is True

	def test_missing_file_is_not_usable(self, tmp_path: Path) -> None:
		from book_meta_fix.covers import sidecar_cover_usable

		assert sidecar_cover_usable(tmp_path / "nope.jpg") is False


class TestRuleMissingCoverUsable:
	"""MISSING_COVER fires for a 0-byte/corrupt cover.jpg too — the file is
	not a cover, and treating it as one froze the book with no recovery."""

	def test_zero_byte_cover_fires_missing_cover(self, tmp_path: Path) -> None:
		from book_meta_fix.detectors import rule_missing_cover

		(tmp_path / "cover.jpg").write_bytes(b"")
		meta = BookMeta(authors=["A"], title="T", path=str(tmp_path))
		diag = rule_missing_cover(meta)
		assert diag is not None and diag.category == "MISSING_COVER"

	def test_real_cover_does_not_fire(self, tmp_path: Path) -> None:
		from book_meta_fix.detectors import rule_missing_cover

		_real_cover(tmp_path / "cover.jpg")
		meta = BookMeta(authors=["A"], title="T", path=str(tmp_path))
		assert rule_missing_cover(meta) is None
