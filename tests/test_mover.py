"""Tests for the organize mover: target-path computation and verdict-driven moves.

Covers three regressions:
  1. compute_needfix_path produced needfix/needfix/... for books already in
     needfix/ (re-diagnosis runs).
  2. organize skipped every book located in needfix/, so a book fixed by
     `apply` could never move back out to the main tree.
  3. The fix is verdict-driven: OK books leave needfix/, broken books enter it.
"""
from __future__ import annotations

from pathlib import Path

from book_meta_fix.models import BookMeta, Verdict
from book_meta_fix.mover import (
	ANONYM_AUTHOR_NAME,
	DEFAULT_NEEDFIX_DIR,
	DEFAULT_PATH_PATTERN,
	compute_needfix_path,
	compute_target_path,
	organize,
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
