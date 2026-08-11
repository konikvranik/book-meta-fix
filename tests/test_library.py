"""Tests for library traversal (iter_book_folders).

Covers the regression that excluded `needfix/` from scanning, hiding ~84% of
the library from every bmf command. Also covers the recursive descent, since
books relocated by `organize` end up at variable depth (needfix/<author>/...).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from book_meta_fix.library import iter_book_folders


def _make_book(folder: Path) -> None:
	"""Create a minimal book folder: just an empty metadata.opf."""
	folder.mkdir(parents=True)
	(folder / "metadata.opf").touch()


class TestIterBookFolders:
	def test_finds_books_in_main_tree(self, tmp_path: Path) -> None:
		"""Standard <lib>/<Author>/<Title> (<id>)/ layout is discovered."""
		_make_book(tmp_path / "Author A" / "Title One (1)")
		found = list(iter_book_folders(tmp_path))
		assert len(found) == 1
		assert found[0].name == "Title One (1)"

	def test_finds_books_in_needfix(self, tmp_path: Path) -> None:
		"""Regression: needfix/ must NOT be excluded — books there are the ones
		`organize` produced and that later runs must be able to re-diagnose and
		move back out once fixed.
		"""
		_make_book(tmp_path / "Author A" / "Title One (1)")
		_make_book(tmp_path / "needfix" / "Author B" / "Title Two (2)")
		found = {p.name for p in iter_book_folders(tmp_path)}
		assert found == {"Title One (1)", "Title Two (2)"}

	def test_descends_arbitrary_depth(self, tmp_path: Path) -> None:
		"""A book nested several levels deep is still found."""
		_make_book(tmp_path / "a" / "b" / "c" / "Deep Book (3)")
		found = list(iter_book_folders(tmp_path))
		assert len(found) == 1
		assert found[0].name == "Deep Book (3)"

	def test_does_not_descend_into_book_folder(self, tmp_path: Path) -> None:
		"""A book folder's own subdirectories are not scanned for nested books
		(a book is a leaf). A stray metadata.opf inside a book's data dir must
		not register as a second book.
		"""
		book = tmp_path / "Author" / "Real Book (1)"
		_make_book(book)
		# A nested directory that also has a metadata file — should be ignored
		# because its parent is already a book.
		nested = book / "subdir"
		nested.mkdir()
		(nested / "metadata.opf").touch()
		found = list(iter_book_folders(tmp_path))
		assert len(found) == 1

	def test_excludes_temp_calibre(self, tmp_path: Path) -> None:
		_make_book(tmp_path / "Author A" / "Title (1)")
		_make_book(tmp_path / "temp_calibre" / "Author B" / "Title (2)")
		found = list(iter_book_folders(tmp_path))
		assert [p.name for p in found] == ["Title (1)"]

	def test_excludes_dotfiles(self, tmp_path: Path) -> None:
		"""Hidden directories (e.g. .git) are pruned at any depth."""
		_make_book(tmp_path / "Author A" / "Title (1)")
		_make_book(tmp_path / ".hidden" / "Author B" / "Title (2)")
		found = list(iter_book_folders(tmp_path))
		assert [p.name for p in found] == ["Title (1)"]

	def test_excludes_calibre_scratch(self, tmp_path: Path) -> None:
		_make_book(tmp_path / "Author A" / "Title (1)")
		_make_book(tmp_path / "calibre-server-mnt" / "Author B" / "Title (2)")
		found = list(iter_book_folders(tmp_path))
		assert [p.name for p in found] == ["Title (1)"]

	def test_deterministic_order(self, tmp_path: Path) -> None:
		"""Yield order is name-sorted so reports are reproducible."""
		for i in [3, 1, 2]:
			_make_book(tmp_path / "Author" / f"Title ({i})")
		found = [int(p.name.strip("Title ()")) for p in iter_book_folders(tmp_path)]
		assert found == [1, 2, 3]

	def test_raises_when_library_missing(self, tmp_path: Path) -> None:
		missing = tmp_path / "does-not-exist"
		with pytest.raises(FileNotFoundError):
			list(iter_book_folders(missing))

	def test_accepts_opf_or_json(self, tmp_path: Path) -> None:
		"""A folder is a book if it has metadata.opf OR metadata.json."""
		_make_book(tmp_path / "Author" / "Opf Book (1)")  # has .opf
		json_book = tmp_path / "Author" / "Json Book (2)"
		json_book.mkdir(parents=True)
		(json_book / "metadata.json").touch()
		found = {p.name for p in iter_book_folders(tmp_path)}
		assert found == {"Opf Book (1)", "Json Book (2)"}

	def test_sees_tilde_dollar_author_folder(self, tmp_path: Path) -> None:
		"""Regression: a book whose author metadata was polluted to '~$Foo'
		lives under a '~$Foo/' author folder. The walker must NOT prune such
		directories, otherwise the C6 detector can never see them and the book
		is invisible to every bmf command. (Only concrete '~$ lock FILES are
		pruned, not directories.)"""
		_make_book(tmp_path / "Author" / "Normal (1)")
		_make_book(tmp_path / "~$N. Shearer" / "Accident Report (2)")
		found = {p.name for p in iter_book_folders(tmp_path)}
		assert "Accident Report (2)" in found
		assert "Normal (1)" in found
