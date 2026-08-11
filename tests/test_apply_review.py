"""Tests for apply_review, focusing on the action: delete path.

Covers: dry-run reports without removing; write archives folders into a tar.gz
snapshot and then removes them; summary counts.
"""
from __future__ import annotations

import tarfile
from pathlib import Path

from book_meta_fix.pipeline import apply_review


def _seed_library(library: Path, ids: list[int]) -> None:
	"""Create a fake book folder per id with a marker file inside."""
	for bid in ids:
		folder = library / f"author_{bid}" / f"book_{bid}"
		folder.mkdir(parents=True)
		(folder / "metadata.json").write_text("{}\n", encoding="utf-8")
		(folder / f"book_{bid}.epub").write_text("not a real epub", encoding="utf-8")


def _write_review(path: Path, entries: list[dict]) -> None:
	"""Write a multi-document review.yaml from plain dicts."""
	import yaml

	body = "".join(f"---\n{yaml.safe_dump(e, sort_keys=False)}" for e in entries)
	path.write_text(body, encoding="utf-8")


class TestDeleteAction:
	def test_dry_run_does_not_remove(self, tmp_path):
		library = tmp_path / "lib"
		_seed_library(library, [1])
		review = tmp_path / "review.yaml"
		_write_review(review, [{
			"id": 1, "path": f"author_1/book_1",
			"current": {"title": "~$doc"}, "proposed": {"reason": "word lock"},
			"action": "delete",
		}])
		summary = apply_review(review, library, dry_run=True)
		assert summary["deleted"] == 1
		assert summary["applied"] == 0
		# Folder still present in dry-run.
		assert (library / "author_1" / "book_1").is_dir()
		# No snapshot in dry-run.
		assert summary["snapshot"] is None
		# No tar.gz written.
		assert not list(tmp_path.glob("deletion_snapshot_*.tar.gz"))

	def test_write_archives_then_removes(self, tmp_path):
		library = tmp_path / "lib"
		_seed_library(library, [1, 2])
		review = tmp_path / "review.yaml"
		_write_review(review, [
			{"id": 1, "path": "author_1/book_1", "current": {"title": "~$a"}, "proposed": {}, "action": "delete"},
			{"id": 2, "path": "author_2/book_2", "current": {"title": "~$b"}, "proposed": {}, "action": "delete"},
		])
		# Run from tmp_path so the snapshot lands there (not in the repo).
		import os
		cwd = os.getcwd()
		os.chdir(tmp_path)
		try:
			summary = apply_review(review, library, dry_run=False)
		finally:
			os.chdir(cwd)
		assert summary["deleted"] == 2
		# Both folders removed.
		assert not (library / "author_1" / "book_1").exists()
		assert not (library / "author_2" / "book_2").exists()
		# Snapshot exists and contains both folders. The snapshot path is
		# relative to the cwd at apply time (tmp_path), so resolve it there.
		assert summary["snapshot"] is not None
		snap = tmp_path / Path(summary["snapshot"]).name
		assert snap.is_file()
		with tarfile.open(snap, "r:gz") as tar:
			names = tar.getnames()
		assert any("author_1/book_1" in n for n in names)
		assert any("author_2/book_2" in n for n in names)

	def test_missing_folder_recorded_as_error(self, tmp_path):
		library = tmp_path / "lib"
		library.mkdir()
		review = tmp_path / "review.yaml"
		_write_review(review, [{
			"id": 9, "path": "nope/missing",
			"current": {"title": "x"}, "proposed": {}, "action": "delete",
		}])
		summary = apply_review(review, library, dry_run=False)
		assert summary["deleted"] == 0
		assert any("folder not found" in e for e in summary["errors"])

	def test_unknown_action_recorded_as_error(self, tmp_path):
		library = tmp_path / "lib"
		library.mkdir()
		review = tmp_path / "review.yaml"
		_write_review(review, [{
			"id": 1, "path": "a/b",
			"current": {"title": "x"}, "proposed": {}, "action": "bogus",
		}])
		summary = apply_review(review, library, dry_run=False)
		assert any("unknown action" in e for e in summary["errors"])
