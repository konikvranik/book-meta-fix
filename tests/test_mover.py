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
	DEFAULT_NEEDFIX_DIR,
	DEFAULT_PATH_PATTERN,
	compute_needfix_path,
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
