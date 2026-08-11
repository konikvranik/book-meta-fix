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
