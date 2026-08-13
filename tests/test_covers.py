"""Tests for cover detection, download, and the C11/MISSING_COVER detector rules.

Covers two layers:
  - covers.py: pixel-analysis classification + atomic download
  - detectors.py: rule_generated_cover / rule_missing_cover
"""
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from book_meta_fix.covers import analyze_cover, download_cover, recover_cover_from_book
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
			ok = recover_cover_from_book(tmp_path / "book.epub", dest)
		assert ok is True
		assert dest.is_file()

	def test_generated_extract_is_rejected(self, tmp_path: Path) -> None:
		"""A solid-color (generated) extract never becomes cover.jpg."""
		dest = tmp_path / "cover.jpg"
		with patch("book_meta_fix.covers.extract_cover_from_book", side_effect=_extractor_writing(_solid_cover)):
			ok = recover_cover_from_book(tmp_path / "book.epub", dest)
		assert ok is False
		assert not dest.exists()

	def test_generated_extract_does_not_overwrite_existing(self, tmp_path: Path) -> None:
		"""A real cover already at dest survives a generated extraction attempt —
		regression guard against replacing artwork with a placeholder."""
		dest = tmp_path / "cover.jpg"
		_gradient_cover(dest)
		original = dest.read_bytes()
		with patch("book_meta_fix.covers.extract_cover_from_book", side_effect=_extractor_writing(_solid_cover)):
			ok = recover_cover_from_book(tmp_path / "book.epub", dest)
		assert ok is False
		assert dest.read_bytes() == original  # untouched

	def test_extraction_failure_returns_false(self, tmp_path: Path) -> None:
		"""calibre absent / no embedded cover (extract returns None) -> False."""
		dest = tmp_path / "cover.jpg"
		with patch("book_meta_fix.covers.extract_cover_from_book", return_value=None):
			ok = recover_cover_from_book(tmp_path / "book.epub", dest)
		assert ok is False
		assert not dest.exists()

	def test_existing_dest_backed_up_on_success(self, tmp_path: Path) -> None:
		"""A successful overwrite mirrors download_cover: prior cover -> .bak."""
		dest = tmp_path / "cover.jpg"
		_solid_cover(dest)  # existing cover (will be replaced by the gradient)
		old = dest.read_bytes()
		with patch("book_meta_fix.covers.extract_cover_from_book", side_effect=_extractor_writing(_gradient_cover)):
			ok = recover_cover_from_book(tmp_path / "book.epub", dest)
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
			ok = recover_cover_from_book(tmp_path / "book.epub", dest)
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
