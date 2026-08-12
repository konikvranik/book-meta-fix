"""Tests for library traversal (iter_book_folders).

Covers the regression that excluded `needfix/` from scanning, hiding ~84% of
the library from every bmf command. Also covers the recursive descent, since
books relocated by `organize` end up at variable depth (needfix/<author>/...).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from book_meta_fix.library import Cache, iter_book_folders
from book_meta_fix.readers import read_book_folder


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


def _prime_cache(cache: Cache, folder: Path) -> None:
	"""Create a real book folder and put its BookMeta into the cache."""
	folder.mkdir(parents=True, exist_ok=True)
	(folder / "metadata.json").write_text("{}\n", encoding="utf-8")
	cache.put(read_book_folder(folder))
	cache.commit()


class TestCacheInvalidation:
	"""Cache.invalidate / invalidate_many / clear drop rows so the next scan
	re-parses — the guarantee apply/organize rely on to avoid serving stale
	BookMeta after a write/move (the mtime heuristic alone is unreliable on NFS).
	"""

	def _has_row(self, cache: Cache, path: Path) -> bool:
		return cache.conn.execute("SELECT 1 FROM books WHERE path = ?", (str(path),)).fetchone() is not None

	def test_invalidate_drops_entry(self, tmp_path: Path) -> None:
		cache = Cache(tmp_path / "cache.db")
		folder = tmp_path / "Author" / "Title (1)"
		_prime_cache(cache, folder)
		assert cache.get(folder) is not None  # served from cache while valid
		cache.invalidate(folder)
		cache.commit()
		assert not self._has_row(cache, folder)
		assert cache.get(folder) is None
		cache.close()

	def test_invalidate_accepts_str_or_path(self, tmp_path: Path) -> None:
		"""str and Path forms must both match the key put() wrote."""
		cache = Cache(tmp_path / "cache.db")
		folder = tmp_path / "Author" / "Title (1)"
		_prime_cache(cache, folder)
		cache.invalidate(str(folder))
		cache.commit()
		assert not self._has_row(cache, folder)
		cache.close()

	def test_invalidate_missing_path_is_noop(self, tmp_path: Path) -> None:
		"""Invalidating a path that was never cached must not raise."""
		cache = Cache(tmp_path / "cache.db")
		cache.invalidate(tmp_path / "never" / "seen (9)")
		cache.commit()
		cache.close()

	def test_invalidate_many(self, tmp_path: Path) -> None:
		cache = Cache(tmp_path / "cache.db")
		paths = [tmp_path / "A" / "One (1)", tmp_path / "B" / "Two (2)", tmp_path / "C" / "Three (3)"]
		for p in paths:
			_prime_cache(cache, p)
		cache.invalidate_many([paths[0], paths[2]])
		cache.commit()
		assert not self._has_row(cache, paths[0])
		assert self._has_row(cache, paths[1])  # untouched
		assert not self._has_row(cache, paths[2])
		cache.close()

	def test_clear_drops_all(self, tmp_path: Path) -> None:
		cache = Cache(tmp_path / "cache.db")
		for name in ("A/One (1)", "B/Two (2)"):
			_prime_cache(cache, tmp_path / name)
		cache.clear()
		cache.commit()
		n = cache.conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
		assert n == 0
		cache.close()


class TestScanProgressCallback:
	"""scan_library(progress_callback=cb) reports strictly-increasing done
	counts, ending at the total number of book folders."""

	@staticmethod
	def _valid_book(folder: Path) -> None:
		"""A book folder with a parseable metadata.json (empty manifest is fine)."""
		folder.mkdir(parents=True)
		(folder / "metadata.json").write_text("{}\n", encoding="utf-8")

	def test_callback_reports_increasing_counts(self, tmp_path: Path) -> None:
		from book_meta_fix.library import scan_library

		for i in range(3):
			self._valid_book(tmp_path / "Author" / f"Title {i} ({i})")
		seen: list[int] = []
		books = scan_library(tmp_path, use_cache=False, progress_callback=lambda done: seen.append(done))
		assert len(books) == 3
		assert seen == [1, 2, 3]  # strictly increasing, final == folder count

	def test_callback_fires_per_folder_with_cache(self, tmp_path: Path) -> None:
		from book_meta_fix.library import scan_library

		self._valid_book(tmp_path / "A" / "One (1)")
		self._valid_book(tmp_path / "A" / "Two (2)")
		cache = Cache(tmp_path / "cache.db")
		seen: list[int] = []
		# First run: fresh parses.
		scan_library(tmp_path, cache=cache, progress_callback=lambda d: seen.append(d))
		# Second run: all cached, but callback still fires per folder.
		seen.clear()
		scan_library(tmp_path, cache=cache, progress_callback=lambda d: seen.append(d))
		assert seen == [1, 2]
		cache.close()

	def test_no_callback_is_default(self, tmp_path: Path) -> None:
		from book_meta_fix.library import scan_library

		self._valid_book(tmp_path / "A" / "One (1)")
		# Must not raise when progress_callback is omitted.
		books = scan_library(tmp_path, use_cache=False)
		assert len(books) == 1
