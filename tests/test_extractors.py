"""Tests for extractor helpers, focused on the catdoc fallback for .doc files.

catdoc handles legacy MS Word .doc files (Composite Document File) which
calibre's ebook-convert cannot read (no DOC input plugin). These are common
in the CZ/SK library — ~340 .doc files, many with filename-as-title C2
corruption that text_meta can only fix once the page text is available.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

from book_meta_fix.extractors import _catdoc_to_text, _ebook_convert_to_text


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
