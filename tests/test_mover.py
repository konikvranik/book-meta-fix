"""Tests for the organize mover: target-path computation and verdict-driven moves.

Covers three regressions:
  1. compute_needfix_path produced needfix/needfix/... for books already in
     needfix/ (re-diagnosis runs).
  2. organize skipped every book located in needfix/, so a book fixed by
     `apply` could never move back out to the main tree.
  3. The fix is verdict-driven: OK books leave needfix/, broken books enter it.
"""
from __future__ import annotations

import json
from pathlib import Path

from book_meta_fix.library import Cache
from book_meta_fix.models import BookMeta, Verdict
from book_meta_fix.mover import (
	ANONYM_AUTHOR_NAME,
	DEFAULT_NEEDFIX_DIR,
	DEFAULT_PATH_PATTERN,
	_disambiguated_dest,
	compute_needfix_path,
	compute_target_path,
	merge_meta,
	organize,
	same_book,
)


def _book(
	calibre_id: int,
	*,
	path: str,
	title: str = "Title",
	authors: list[str] | None = None,
) -> BookMeta:
	"""Minimal BookMeta for path/target-path tests (no I/O)."""
	return BookMeta(
		calibre_id=calibre_id,
		title=title,
		authors=authors or ["Author"],
		path=path,
		primary_file=None,
	)


class TestComputeNeedfixPath:
	def test_outside_needfix_gets_prefixed(self, tmp_path: Path) -> None:
		"""A book in the main tree goes to <lib>/needfix/<rel>."""
		meta = _book(1, path=str(tmp_path / "Author" / "Title (1)"))
		dest = compute_needfix_path(meta, tmp_path, DEFAULT_NEEDFIX_DIR)
		assert dest == tmp_path / "needfix" / "Author" / "Title (1)"

	def test_inside_needfix_no_double_prefix(self, tmp_path: Path) -> None:
		"""Regression: a book already in needfix/ must not become needfix/needfix/."""
		meta = _book(2, path=str(tmp_path / "needfix" / "Author" / "Title (2)"))
		dest = compute_needfix_path(meta, tmp_path, DEFAULT_NEEDFIX_DIR)
		assert dest == tmp_path / "needfix" / "Author" / "Title (2)"
		assert "needfix/needfix" not in str(dest)

	def test_custom_needfix_dir_also_stripped(self, tmp_path: Path) -> None:
		"""A custom needfix dir name is handled the same way."""
		meta = _book(3, path=str(tmp_path / "_broken" / "Author" / "Title (3)"))
		dest = compute_needfix_path(meta, tmp_path, "_broken")
		assert dest == tmp_path / "_broken" / "Author" / "Title (3)"
		assert "_broken/_broken" not in str(dest)


class TestOrganize:
	def test_broken_book_moves_to_needfix(self, tmp_path: Path) -> None:
		"""A broken book in the main tree is moved into needfix/."""
		src = tmp_path / "Author" / "Title (1)"
		src.mkdir(parents=True)
		(src / "metadata.opf").touch()
		meta = _book(1, path=str(src))

		results = organize([(meta, Verdict.NEEDS_REVIEW)], tmp_path, dry_run=False)
		assert len(results) == 1
		assert results[0].action == "moved"
		assert results[0].destination == str(tmp_path / "needfix" / "Author" / "Title (1)")
		assert Path(results[0].destination).is_dir()
		assert not src.exists()

	def test_ok_book_in_needfix_moves_out(self, tmp_path: Path) -> None:
		"""Regression (the main bug): a book fixed by `apply` (verdict OK) that
		still lives in needfix/ must move back out to the main tree.
		"""
		src = tmp_path / "needfix" / "Author" / "Title (2)"
		src.mkdir(parents=True)
		(src / "metadata.opf").touch()
		meta = _book(2, path=str(src), title="Title", authors=["Author"])

		results = organize([(meta, Verdict.OK)], tmp_path, dry_run=False)
		assert len(results) == 1
		assert results[0].action == "moved"
		# OK target uses the path pattern, NOT the needfix path.
		assert results[0].destination == str(tmp_path / "Author" / "Title (2)")
		assert Path(results[0].destination).is_dir()
		assert not src.exists()

	def test_broken_book_already_in_needfix_is_already_correct(self, tmp_path: Path) -> None:
		"""A broken book already at the right place inside needfix/ is not moved."""
		src = tmp_path / "needfix" / "Author" / "Title (3)"
		src.mkdir(parents=True)
		(src / "metadata.opf").touch()
		meta = _book(3, path=str(src))

		results = organize([(meta, Verdict.NEEDS_REVIEW)], tmp_path, dry_run=False)
		assert len(results) == 1
		assert results[0].action == "already_correct"
		assert results[0].destination == str(src)
		assert src.is_dir()

	def test_ok_book_at_correct_path_is_already_correct(self, tmp_path: Path) -> None:
		"""An OK book whose current path already matches the pattern stays."""
		src = tmp_path / "Author" / "Title (4)"
		src.mkdir(parents=True)
		(src / "metadata.opf").touch()
		meta = _book(4, path=str(src), title="Title", authors=["Author"])

		results = organize([(meta, Verdict.OK)], tmp_path, dry_run=False)
		assert results[0].action == "already_correct"
		assert src.is_dir()

	def test_verified_book_treated_as_ok(self, tmp_path: Path) -> None:
		"""VERIFIED is in the default ok_verdicts set, so it leaves needfix/."""
		src = tmp_path / "needfix" / "Author" / "Title (5)"
		src.mkdir(parents=True)
		(src / "metadata.opf").touch()
		meta = _book(5, path=str(src), title="Title", authors=["Author"])

		results = organize([(meta, Verdict.VERIFIED)], tmp_path, dry_run=False)
		assert results[0].action == "moved"
		assert results[0].destination == str(tmp_path / "Author" / "Title (5)")

	def test_mixed_batch_routes_by_verdict(self, tmp_path: Path) -> None:
		"""A single organize call routes each book by its verdict."""
		ok_src = tmp_path / "needfix" / "Author" / "Fixed (1)"
		ok_src.mkdir(parents=True)
		(ok_src / "metadata.opf").touch()
		ok_meta = _book(1, path=str(ok_src), title="Fixed", authors=["Author"])

		broken_src = tmp_path / "Author" / "Broken (2)"
		broken_src.mkdir(parents=True)
		(broken_src / "metadata.opf").touch()
		broken_meta = _book(2, path=str(broken_src), title="Broken", authors=["Author"])

		results = organize(
			[(ok_meta, Verdict.OK), (broken_meta, Verdict.UNFIXABLE)],
			tmp_path, dry_run=True,
		)
		actions = {r.source: r.destination for r in results}
		assert actions[str(ok_src)] == str(tmp_path / "Author" / "Fixed (1)")
		assert actions[str(broken_src)] == str(tmp_path / "needfix" / "Author" / "Broken (2)")


class TestPruneEmptyParents:
	"""Regression: organize() left behind empty author folders after moving
	a book (e.g. 'adams/', 'Alexander Dumas/' — 61 of them in the real library).
	move_book() must now remove newly-empty parent dirs up to the library root."""

	def test_empty_author_folder_removed_after_move(self, tmp_path: Path) -> None:
		"""When a book is the only child of its author folder, the author
		folder must disappear after the move."""
		src = tmp_path / "Bad Author" / "Book (1)"
		src.mkdir(parents=True)
		(src / "metadata.opf").touch()
		meta = _book(1, path=str(src), title="Book", authors=["Good Author"])

		organize([(meta, Verdict.OK)], tmp_path, dry_run=False)

		# Book moved to the new author; old author folder gone.
		assert (tmp_path / "Good Author" / "Book (1)").is_dir()
		assert not (tmp_path / "Bad Author").exists()

	def test_nonempty_author_folder_kept_after_move(self, tmp_path: Path) -> None:
		"""If the author folder still holds other books, it must stay."""
		keep = tmp_path / "Author" / "Stays (2)"
		keep.mkdir(parents=True)
		(keep / "metadata.opf").touch()
		src = tmp_path / "Author" / "Moves (1)"
		src.mkdir(parents=True)
		(src / "metadata.opf").touch()
		meta = _book(1, path=str(src), title="Moves", authors=["New Author"])

		organize([(meta, Verdict.OK)], tmp_path, dry_run=False)

		assert (tmp_path / "New Author" / "Moves (1)").is_dir()
		# Old author folder still has Stays (2), so it must remain.
		assert (tmp_path / "Author").is_dir()

	def test_library_root_not_removed(self, tmp_path: Path) -> None:
		"""The library root itself must never be deleted, even if it becomes empty."""
		src = tmp_path / "Author" / "Book (1)"
		src.mkdir(parents=True)
		(src / "metadata.opf").touch()
		meta = _book(1, path=str(src), title="Book", authors=["Author"])

		organize([(meta, Verdict.OK)], tmp_path, dry_run=False)

		assert tmp_path.is_dir()  # library root survives

	def test_dry_run_leaves_empty_folders(self, tmp_path: Path) -> None:
		"""Dry-run must NOT clean up — it doesn't move anything."""
		src = tmp_path / "Bad Author" / "Book (1)"
		src.mkdir(parents=True)
		(src / "metadata.opf").touch()
		meta = _book(1, path=str(src), title="Book", authors=["Good Author"])

		organize([(meta, Verdict.OK)], tmp_path, dry_run=True)

		# Nothing moved, nothing pruned.
		assert src.is_dir()
		assert (tmp_path / "Bad Author").is_dir()


class TestAnonymCanonicalFolder:
	"""All anonym spellings ('Neznamy', 'neznámý - neuveden', 'Unknown',
	'Anonymous', 'autor neuveden') must collapse into a single canonical
	'Anonym/' folder during organize(), so the library has one anonym tree
	instead of one per spelling variant."""

	def test_target_path_canonicalizes_anonym_variants(self, tmp_path: Path) -> None:
		"""compute_target_path maps every anonym spelling to <lib>/Anonym/..."""
		for variant in ("Neznamy", "neznámý - neuveden", "Unknown",
		                 "Anonymous", "anonym", "autor neuveden"):
			meta = _book(1, path=str(tmp_path / variant / "Book (1)"),
			             title="Book", authors=[variant])
			dest = compute_target_path(meta, DEFAULT_PATH_PATTERN, tmp_path)
			assert dest == tmp_path / ANONYM_AUTHOR_NAME / "Book (1)", (
				f"variant {variant!r} should map to {ANONYM_AUTHOR_NAME}/")

	def test_needfix_path_canonicalizes_anonym_variants(self, tmp_path: Path) -> None:
		"""compute_needfix_path maps every anonym spelling to needfix/Anonym/..."""
		for variant in ("Neznamy", "neznámý - neuveden", "Unknown", "Anonymous"):
			meta = _book(1, path=str(tmp_path / variant / "Book (1)"),
			             title="Book", authors=[variant])
			# BookMeta needs author_folder/title_folder for the needfix fallback path
			meta.author_folder = variant
			meta.title_folder = "Book (1)"
			dest = compute_needfix_path(meta, tmp_path, DEFAULT_NEEDFIX_DIR)
			assert dest == tmp_path / DEFAULT_NEEDFIX_DIR / ANONYM_AUTHOR_NAME / "Book (1)", (
				f"variant {variant!r} should map to {DEFAULT_NEEDFIX_DIR}/{ANONYM_AUTHOR_NAME}/")

	def test_real_author_not_canonicalized(self, tmp_path: Path) -> None:
		"""A real author name must NOT be rewritten to 'Anonym'."""
		meta = _book(1, path=str(tmp_path / "Karel Čapek" / "R.U.R. (1)"),
		             title="R.U.R.", authors=["Karel Čapek"])
		dest = compute_target_path(meta, DEFAULT_PATH_PATTERN, tmp_path)
		assert "Karel Čapek" in dest.parts
		assert ANONYM_AUTHOR_NAME not in dest.parts

	def test_organize_moves_neznamy_book_to_anonym(self, tmp_path: Path) -> None:
		"""End-to-end: an OK book in a 'Neznamy/' folder lands in 'Anonym/'."""
		src = tmp_path / "Neznamy" / "Some Book (1)"
		src.mkdir(parents=True)
		(src / "metadata.opf").touch()
		meta = _book(1, path=str(src), title="Some Book", authors=["Neznamy"])

		organize([(meta, Verdict.OK)], tmp_path, dry_run=False)

		assert (tmp_path / ANONYM_AUTHOR_NAME / "Some Book (1)").is_dir()
		assert not (tmp_path / "Neznamy").exists()


class TestCacheInvalidation:
	"""organize invalidates the cache for folders it actually moves (both the
	source it moved from and the destination it moved to), so a later scan
	re-parses them. Dry runs and no-op moves leave the cache untouched."""

	def _has_row(self, cache: Cache, path: str | Path) -> bool:
		return cache.conn.execute("SELECT 1 FROM books WHERE path = ?", (str(Path(path)),)).fetchone() is not None

	def _prime(self, cache: Cache, path: Path) -> None:
		from book_meta_fix.readers import read_book_folder

		path.mkdir(parents=True, exist_ok=True)
		# metadata.json (not an empty .opf) so read_book_folder parses cleanly.
		(path / "metadata.json").write_text("{}\n", encoding="utf-8")
		cache.put(read_book_folder(path))
		cache.commit()

	def _seed_stale_row(self, cache: Cache, path: Path) -> None:
		"""Insert a cache row for *path* without a real folder (simulates a stale
		entry left by a previous occupant of that path, e.g. a needfix round-trip)."""
		cache.conn.execute(
			"INSERT INTO books(path, mtime, size, payload, scanned_at) VALUES (?,?,?,?,?)",
			(str(path), 0.0, 0, "{}", 0.0),
		)
		cache.commit()

	def test_move_invalidates_source_and_destination(self, tmp_path: Path) -> None:
		cache = Cache(tmp_path / "cache.db")
		src = tmp_path / "Author" / "Title (1)"
		self._prime(cache, src)
		# Stale entry at the destination the book will move to.
		dest = tmp_path / "needfix" / "Author" / "Title (1)"
		self._seed_stale_row(cache, dest)
		assert self._has_row(cache, src)
		assert self._has_row(cache, dest)

		meta = _book(1, path=str(src))
		results = organize([(meta, Verdict.NEEDS_REVIEW)], tmp_path, dry_run=False, cache=cache)

		assert results[0].action == "moved"
		assert not self._has_row(cache, src)  # moved-from entry dropped
		assert not self._has_row(cache, dest)  # stale moved-to entry cleared
		cache.close()

	def test_dry_run_does_not_invalidate(self, tmp_path: Path) -> None:
		cache = Cache(tmp_path / "cache.db")
		src = tmp_path / "Author" / "Title (1)"
		self._prime(cache, src)
		meta = _book(1, path=str(src))

		organize([(meta, Verdict.NEEDS_REVIEW)], tmp_path, dry_run=True, cache=cache)

		assert self._has_row(cache, src)  # nothing moved → entry kept
		cache.close()

	def test_already_correct_does_not_invalidate(self, tmp_path: Path) -> None:
		"""A book already at its OK destination yields 'already_correct' and must
		leave the cache untouched."""
		cache = Cache(tmp_path / "cache.db")
		# Default OK pattern {author}/{title} ({id}) resolves to src itself.
		src = tmp_path / "Author" / "Title (1)"
		self._prime(cache, src)
		meta = _book(1, path=str(src))

		results = organize([(meta, Verdict.OK)], tmp_path, dry_run=False, cache=cache)

		assert results[0].action == "already_correct"
		assert self._has_row(cache, src)
		cache.close()


# ---------------------------------------------------------------------------
# Same-book detection + collision resolution (merge / disambiguate).
#
# Detection compares metadata records (title/author/isbn/year), NOT file
# content, so the format files here are plain dummy bytes — merge just moves
# them by extension (it never parses content except for an identical-bytes
# check on name collisions).
# ---------------------------------------------------------------------------


def _make_book(
	lib: Path,
	author: str,
	title: str,
	cid: int,
	*,
	isbn: str | None = None,
	year: int | None = None,
	fmt_files: tuple[str, ...] = (".epub",),
	file_stem: str | None = None,
) -> tuple[Path, BookMeta]:
	"""Create <lib>/<author>/<title> (<cid>)/ with metadata.json + dummy format files.

	Returns (folder, BookMeta) where BookMeta is read back by read_book_folder
	(so it carries the realistic formats/path the way organize sees it).
	"""
	from book_meta_fix.readers import read_book_folder

	folder = lib / author / f"{title} ({cid})"
	folder.mkdir(parents=True)
	md: dict = {"title": title, "authors": [author]}
	if isbn:
		md["isbn"] = isbn
	if year is not None:
		md["publishedYear"] = str(year)
	(folder / "metadata.json").write_text(json.dumps(md), encoding="utf-8")
	# metadata.opf so the folder is recognized even if json is read first.
	stem = file_stem or f"{title} - {author}"
	for ext in fmt_files:
		(folder / f"{stem}{ext}").write_bytes(f"{stem}{ext}:{cid}".encode())
	(folder / "metadata.opf").write_text(
		'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
		'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
		f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator></metadata></package>",
		encoding="utf-8",
	)
	return folder, read_book_folder(folder)


class TestSameBook:
	def test_isbn_equal_is_same(self):
		a = BookMeta(title="X", authors=["A"], isbn="9780306406157")
		b = BookMeta(title="X", authors=["A"], isbn="9780306406157")
		assert same_book(a, b)

	def test_isbn_differ_but_title_author_match_no_year_is_same(self):
		# No year to disambiguate → assume same work even if ISBN differs/absent.
		a = BookMeta(title="Babička", authors=["Božena Němcová"])
		b = BookMeta(title="Babička", authors=["Božena Němcová"])
		assert same_book(a, b)

	def test_title_author_match_but_year_differs_is_different(self):
		a = BookMeta(title="Babička", authors=["Božena Němcová"], year=1995)
		b = BookMeta(title="Babička", authors=["Božena Němcová"], year=2010)
		assert not same_book(a, b)  # different edition

	def test_title_differs_is_different(self):
		a = BookMeta(title="Babička", authors=["A"])
		b = BookMeta(title="Saturnin", authors=["A"])
		assert not same_book(a, b)

	def test_year_differs_but_isbn_equal_still_same(self):
		# ISBN is the strongest signal — it overrides the year tie-breaker.
		a = BookMeta(title="X", authors=["A"], isbn="9780306406157", year=1995)
		b = BookMeta(title="X", authors=["A"], isbn="9780306406157", year=2010)
		assert same_book(a, b)


class TestDisambiguatedDest:
	def test_year_suffix(self, tmp_path):
		d = _disambiguated_dest(tmp_path / "Title", 2026, kind="year")
		assert d.name == "Title (2026)"

	def test_id_suffix_has_id_prefix(self, tmp_path):
		d = _disambiguated_dest(tmp_path / "Title", 123, kind="id")
		assert d.name == "Title (id123)"

	def test_returns_none_when_value_already_in_name(self, tmp_path):
		# Pattern already produced "Title (123)"; id 123 can't disambiguate.
		assert _disambiguated_dest(tmp_path / "Title (123)", 123, kind="id") is None
		assert _disambiguated_dest(tmp_path / "Title (2026)", 2026, kind="year") is None

	def test_returns_none_for_empty_value(self, tmp_path):
		assert _disambiguated_dest(tmp_path / "Title", None, kind="id") is None


class TestOrganizeCollisions:
	"""Collision resolution: merge same-book, disambiguate different-book."""

	PAT = "{author}/{title}"  # id-less → same author+title books collide

	def test_same_isbn_merges_into_one_folder(self, tmp_path):
		f1, m1 = _make_book(tmp_path, "A", "T", 100, isbn="9780306406157", fmt_files=(".epub",))
		f2, m2 = _make_book(tmp_path, "A", "T", 200, isbn="9780306406157", fmt_files=(".pdb",))
		results = organize([(m1, Verdict.OK), (m2, Verdict.OK)], tmp_path, path_pattern=self.PAT, dry_run=False)
		actions = [r.action for r in results]
		# One move (base → dest) + one merge (loser into dest).
		assert "merged" in actions
		dest = tmp_path / "A" / "T"
		assert dest.is_dir()
		# Both formats present in the merged folder.
		assert (dest / "T - A.epub").is_file()
		assert (dest / "T - A.pdb").is_file()
		# Exactly one source folder survived (the winner's, now at dest); the
		# other was removed by the merge.
		assert not f1.exists() and not f2.exists()

	def test_dry_run_merges_nothing(self, tmp_path):
		f1, m1 = _make_book(tmp_path, "A", "T", 100, isbn="9780306406157")
		f2, m2 = _make_book(tmp_path, "A", "T", 200, isbn="9780306406157")
		organize([(m1, Verdict.OK), (m2, Verdict.OK)], tmp_path, path_pattern=self.PAT, dry_run=True)
		# Nothing moved/merged: both source folders intact with their files,
		# and no bare dest folder created.
		assert (f1 / "T - A.epub").is_file()
		assert (f2 / "T - A.epub").is_file()
		assert not (tmp_path / "A" / "T").is_dir()

	def test_same_title_author_year_no_isbn_merges(self, tmp_path):
		_make_book(tmp_path, "A", "T", 100, year=1999, fmt_files=(".epub",))
		_, m2 = _make_book(tmp_path, "A", "T", 200, year=1999, fmt_files=(".pdb",))
		from book_meta_fix.readers import read_book_folder

		m1 = read_book_folder(tmp_path / "A" / "T (100)")
		organize([(m1, Verdict.OK), (m2, Verdict.OK)], tmp_path, path_pattern=self.PAT, dry_run=False)
		dest = tmp_path / "A" / "T"
		assert (dest / "T - A.epub").is_file() and (dest / "T - A.pdb").is_file()

	def test_different_year_disambiguates_by_year(self, tmp_path):
		_, m1 = _make_book(tmp_path, "A", "T", 100, year=1995)
		_, m2 = _make_book(tmp_path, "A", "T", 200, year=2010)
		organize([(m1, Verdict.OK), (m2, Verdict.OK)], tmp_path, path_pattern=self.PAT, dry_run=False)
		assert (tmp_path / "A" / "T (1995)").is_dir()
		assert (tmp_path / "A" / "T (2010)").is_dir()
		# Bare dest NOT used (both disambiguated for consistency).
		assert not (tmp_path / "A" / "T").is_dir()

	def test_different_books_disambiguate_by_id(self, tmp_path):
		# Coarse pattern {author} → different titles by same author collide.
		_, m1 = _make_book(tmp_path, "A", "One", 100)
		_, m2 = _make_book(tmp_path, "A", "Two", 200)
		organize([(m1, Verdict.OK), (m2, Verdict.OK)], tmp_path, path_pattern="{author}", dry_run=False)
		# No years → id suffix; the "id" prefix distinguishes from a year.
		assert (tmp_path / "A (id100)").is_dir()
		assert (tmp_path / "A (id200)").is_dir()

	def test_duplicate_id_different_books_falls_back_to_dup_n(self, tmp_path):
		# Two different books sharing a (corrupt) calibre_id, colliding under a
		# coarse pattern: id-disambiguation yields the same path for both, so the
		# second lands via move_book's (dup N) safety net.
		_, m1 = _make_book(tmp_path, "A", "One", 100)
		_, m2 = _make_book(tmp_path, "A", "Two", 100)  # same id, different title
		organize([(m1, Verdict.OK), (m2, Verdict.OK)], tmp_path, path_pattern="{author}", dry_run=False)
		assert (tmp_path / "A (id100)").is_dir()
		assert (tmp_path / "A (id100) (dup 1)").is_dir()

	def test_field_merge_fills_missing_from_loser(self, tmp_path):
		# Base (id 100) lacks year; loser (id 200) has year. After merge the
		# winner metadata carries the year, and calibre_id stays the base's.
		from book_meta_fix.readers import read_book_folder

		_make_book(tmp_path, "A", "T", 100, isbn="9780306406157")  # base, no year
		_make_book(tmp_path, "A", "T", 200, isbn="9780306406157", year=2005)  # loser w/ year
		m1 = read_book_folder(tmp_path / "A" / "T (100)")
		m2 = read_book_folder(tmp_path / "A" / "T (200)")
		organize([(m1, Verdict.OK), (m2, Verdict.OK)], tmp_path, path_pattern=self.PAT, dry_run=False)
		merged = read_book_folder(tmp_path / "A" / "T")
		assert merged.year == 2005  # filled from loser
		# calibre_id retention (base's id) is covered by TestMergeMeta; under an
		# id-less pattern it isn't encoded in the path/json so we don't assert it here.

	def test_filename_collision_renames_with_loser_id(self, tmp_path):
		# Both folders have a file named identically but with different bytes.
		f1, m1 = _make_book(tmp_path, "A", "T", 100, isbn="9780306406157", fmt_files=(".epub",), file_stem="book")
		_, m2 = _make_book(tmp_path, "A", "T", 200, isbn="9780306406157", fmt_files=(".epub",), file_stem="book")
		organize([(m1, Verdict.OK), (m2, Verdict.OK)], tmp_path, path_pattern=self.PAT, dry_run=False)
		dest = tmp_path / "A" / "T"
		# Original kept; loser's renamed with its id.
		assert (dest / "book.epub").is_file()
		assert (dest / "book (id200).epub").is_file()

	def test_filename_identical_bytes_skipped(self, tmp_path):
		# Same filename AND identical bytes → loser's skipped (not duplicated).
		f1, m1 = _make_book(tmp_path, "A", "T", 100, isbn="9780306406157", fmt_files=(".epub",), file_stem="book")
		f2, m2 = _make_book(tmp_path, "A", "T", 200, isbn="9780306406157", fmt_files=(".epub",), file_stem="book")
		identical = b"samebytes"
		(f1 / "book.epub").write_bytes(identical)
		(f2 / "book.epub").write_bytes(identical)
		organize([(m1, Verdict.OK), (m2, Verdict.OK)], tmp_path, path_pattern=self.PAT, dry_run=False)
		dest = tmp_path / "A" / "T"
		# Only one book.epub (the identical loser was skipped, not renamed).
		assert list(dest.glob("book*.epub")) == [dest / "book.epub"]

	def test_pre_existing_occupant_same_book_merges_into_it(self, tmp_path):
		# A book already at dest from a previous run; a new duplicate arrives.
		_make_book(tmp_path, "A", "T", 100, isbn="9780306406157", fmt_files=(".epub",))
		# Move it into place first (simulate a prior organize).
		from book_meta_fix.readers import read_book_folder

		organize([(read_book_folder(tmp_path / "A" / "T (100)"), Verdict.OK)], tmp_path, path_pattern=self.PAT, dry_run=False)
		# Now a second book with same ISBN appears elsewhere.
		_, m2 = _make_book(tmp_path, "A", "T", 200, isbn="9780306406157", fmt_files=(".pdb",))
		organize([(m2, Verdict.OK)], tmp_path, path_pattern=self.PAT, dry_run=False)
		dest = tmp_path / "A" / "T"
		assert (dest / "T - A.epub").is_file() and (dest / "T - A.pdb").is_file()

	def test_three_book_mixed_merge_and_disambiguate(self, tmp_path):
		# A=B same (1995), C different edition (2010): {A,B} merge into
		# "T (1995)", C goes to "T (2010)".
		_, m1 = _make_book(tmp_path, "A", "T", 100, year=1995, fmt_files=(".epub",))
		_, m2 = _make_book(tmp_path, "A", "T", 200, year=1995, fmt_files=(".pdb",))
		_, m3 = _make_book(tmp_path, "A", "T", 300, year=2010, fmt_files=(".pdf",))
		organize([(m1, Verdict.OK), (m2, Verdict.OK), (m3, Verdict.OK)], tmp_path, path_pattern=self.PAT, dry_run=False)
		y1995 = tmp_path / "A" / "T (1995)"
		y2010 = tmp_path / "A" / "T (2010)"
		# A,B merged in 1995 folder (both formats); C alone in 2010.
		assert (y1995 / "T - A.epub").is_file() and (y1995 / "T - A.pdb").is_file()
		assert (y2010 / "T - A.pdf").is_file()


class TestMergeMeta:
	def test_base_kept_loser_fills_missing(self):
		base = BookMeta(calibre_id=100, title="T", authors=["A"], isbn="9780306406157")
		other = BookMeta(calibre_id=200, title="T", authors=["A"], year=2001, publisher="P")
		m = merge_meta(base, other)
		assert m.calibre_id == 100  # base id
		assert m.isbn == "9780306406157"  # base
		assert m.year == 2001 and m.publisher == "P"  # filled from loser

	def test_authors_unioned_base_first(self):
		base = BookMeta(calibre_id=1, title="T", authors=["A", "B"])
		other = BookMeta(calibre_id=2, title="T", authors=["B", "C"])
		m = merge_meta(base, other)
		assert m.authors == ["A", "B", "C"]


class TestOrganizeCacheInvalidationMerge:
	def test_real_merge_drops_cache_rows_for_both_folders(self, tmp_path):
		cache = Cache(tmp_path / "cache.db")
		f1, m1 = _make_book(tmp_path, "A", "T", 100, isbn="9780306406157")
		f2, m2 = _make_book(tmp_path, "A", "T", 200, isbn="9780306406157")
		cache.put(m1)
		cache.put(m2)
		cache.commit()
		assert self._has_row(cache, f1) and self._has_row(cache, f2)

		organize([(m1, Verdict.OK), (m2, Verdict.OK)], tmp_path, path_pattern="{author}/{title}", dry_run=False, cache=cache)
		# Both source folders' rows dropped (one moved, one merged+deleted); the
		# destination row cleared too.
		assert not self._has_row(cache, f1)
		assert not self._has_row(cache, f2)
		cache.close()

	@staticmethod
	def _has_row(cache: Cache, path: Path) -> bool:
		return cache.conn.execute("SELECT 1 FROM books WHERE path = ?", (str(Path(path)),)).fetchone() is not None

