"""Tests for apply_review, focusing on the action: delete path.

Covers: dry-run reports without removing; write archives folders into a tar.gz
snapshot and then removes them; summary counts.
"""
from __future__ import annotations

import tarfile
from pathlib import Path
from unittest.mock import patch

from book_meta_fix.covers import CoverInfo
from book_meta_fix.pipeline import apply_review
from book_meta_fix.review import parse_review


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


class TestCoverDownloadGate:
	"""Covers are downloaded only for C11 / MISSING_COVER, and a re-run of apply
	must not re-download a cover that's already been fixed (idempotent)."""

	def _seed_book(self, library: Path, bid: int = 1) -> Path:
		folder = library / f"author_{bid}" / f"book_{bid}"
		folder.mkdir(parents=True)
		(folder / "metadata.json").write_text("{}\n", encoding="utf-8")
		(folder / f"book_{bid}.epub").write_text("x", encoding="utf-8")
		return folder

	def _entry(self, bid: int, category: str) -> dict:
		return {
			"id": bid, "path": f"author_{bid}/book_{bid}",
			"diagnosis": {"category": category, "reason": "r"},
			"current": {"title": "T"},
			"proposed": {"cover_url": "https://example.com/cover.jpg"},
			"action": "accept",
		}

	def test_no_download_for_non_cover_category(self, tmp_path):
		"""A C2 book with a cover_url in proposed (legacy review.yaml) must not
		trigger a download — the diagnosis isn't about the cover."""
		library = tmp_path / "lib"
		self._seed_book(library)
		review = tmp_path / "review.yaml"
		_write_review(review, [self._entry(1, "C2")])
		with patch("book_meta_fix.covers.download_cover") as dl:
			apply_review(review, library, dry_run=False)
		dl.assert_not_called()

	def test_download_for_c11_when_no_cover(self, tmp_path):
		library = tmp_path / "lib"
		self._seed_book(library)
		review = tmp_path / "review.yaml"
		_write_review(review, [self._entry(1, "C11")])
		with patch("book_meta_fix.covers.download_cover") as dl:
			apply_review(review, library, dry_run=False)
		dl.assert_called_once()

	def test_download_for_missing_cover(self, tmp_path):
		library = tmp_path / "lib"
		self._seed_book(library)
		review = tmp_path / "review.yaml"
		_write_review(review, [self._entry(1, "MISSING_COVER")])
		with patch("book_meta_fix.covers.download_cover") as dl:
			apply_review(review, library, dry_run=False)
		dl.assert_called_once()

	def test_skip_c11_when_cover_already_real(self, tmp_path):
		"""C11 + cover.jpg present + analyze says NOT a placeholder → already
		replaced, skip the download (idempotent re-run)."""
		library = tmp_path / "lib"
		book = self._seed_book(library)
		(book / "cover.jpg").write_bytes(b"real-cover-bytes")
		review = tmp_path / "review.yaml"
		_write_review(review, [self._entry(1, "C11")])
		with patch("book_meta_fix.covers.analyze_cover", return_value=CoverInfo(is_generated=False)) as ac, \
			patch("book_meta_fix.covers.download_cover") as dl:
			apply_review(review, library, dry_run=False)
		ac.assert_called_once()
		dl.assert_not_called()

	def test_download_c11_when_cover_still_placeholder(self, tmp_path):
		"""C11 + cover.jpg present + analyze says still a placeholder → download."""
		library = tmp_path / "lib"
		book = self._seed_book(library)
		(book / "cover.jpg").write_bytes(b"placeholder")
		review = tmp_path / "review.yaml"
		_write_review(review, [self._entry(1, "C11")])
		with patch("book_meta_fix.covers.analyze_cover", return_value=CoverInfo(is_generated=True)), \
			patch("book_meta_fix.covers.download_cover") as dl:
			apply_review(review, library, dry_run=False)
		dl.assert_called_once()

	def test_skip_missing_cover_when_cover_exists(self, tmp_path):
		"""MISSING_COVER + cover.jpg now exists → already filled, skip."""
		library = tmp_path / "lib"
		book = self._seed_book(library)
		(book / "cover.jpg").write_bytes(b"filled")
		review = tmp_path / "review.yaml"
		_write_review(review, [self._entry(1, "MISSING_COVER")])
		with patch("book_meta_fix.covers.download_cover") as dl:
			apply_review(review, library, dry_run=False)
		dl.assert_not_called()

	def test_download_when_c11_is_secondary_diagnosis(self, tmp_path):
		"""A book whose primary diagnosis is C2 but that also has C11 (in the
		`diagnoses` list) gets its cover downloaded — multi-problem, one apply."""
		library = tmp_path / "lib"
		self._seed_book(library)
		review = tmp_path / "review.yaml"
		_write_review(review, [{
			"id": 1, "path": "author_1/book_1",
			"diagnosis": {"category": "C2", "reason": "r"},
			"diagnoses": [
				{"category": "C2", "reason": "r"},
				{"category": "C11", "reason": "generated cover"},
			],
			"current": {"title": "T"},
			"proposed": {"cover_url": "https://example.com/cover.jpg"},
			"action": "accept",
		}])
		with patch("book_meta_fix.covers.download_cover") as dl:
			apply_review(review, library, dry_run=False)
		dl.assert_called_once()

	def test_no_download_legacy_entry_only_primary_c2(self, tmp_path):
		"""Backward compat: an old entry with only `diagnosis: C2` (no diagnoses
		list) and a stray cover_url must not download — C2 isn't a cover issue."""
		library = tmp_path / "lib"
		self._seed_book(library)
		review = tmp_path / "review.yaml"
		_write_review(review, [{
			"id": 1, "path": "author_1/book_1",
			"diagnosis": {"category": "C2", "reason": "r"},
			"current": {"title": "T"},
			"proposed": {"cover_url": "https://example.com/cover.jpg"},
			"action": "accept",
		}])
		with patch("book_meta_fix.covers.download_cover") as dl:
			apply_review(review, library, dry_run=False)
		dl.assert_not_called()


class TestPruning:
	"""Successfully-applied entries are pruned from review.yaml; pending,
	rejected, and errored entries are kept. Dry-run never prunes."""

	def _seed_book(self, library: Path, bid: int) -> Path:
		folder = library / f"a{bid}" / f"b{bid}"
		folder.mkdir(parents=True)
		(folder / "metadata.json").write_text("{}\n", encoding="utf-8")
		(folder / f"b{bid}.epub").write_text("x", encoding="utf-8")
		return folder

	def test_applied_entries_pruned(self, tmp_path):
		library = tmp_path / "lib"
		self._seed_book(library, 1)
		self._seed_book(library, 2)
		review = tmp_path / "review.yaml"
		_write_review(review, [
			{"id": 1, "path": "a1/b1", "current": {"title": "A"}, "proposed": {}, "action": "accept"},
			{"id": 2, "path": "a2/b2", "current": {"title": "B"}, "proposed": {}, "action": "accept"},
		])
		summary = apply_review(review, library, dry_run=False)
		assert summary["applied"] == 2
		assert summary["pruned"] == 2
		# Both applied → both removed; file is now header-only.
		assert parse_review(review) == []

	def test_pending_and_rejected_kept(self, tmp_path):
		library = tmp_path / "lib"
		self._seed_book(library, 1)
		self._seed_book(library, 2)
		self._seed_book(library, 3)
		review = tmp_path / "review.yaml"
		_write_review(review, [
			{"id": 1, "path": "a1/b1", "current": {"title": "A"}, "proposed": {}, "action": "accept"},
			{"id": 2, "path": "a2/b2", "current": {"title": "B"}, "proposed": {}, "action": None},
			{"id": 3, "path": "a3/b3", "current": {"title": "C"}, "proposed": {}, "action": "reject"},
		])
		summary = apply_review(review, library, dry_run=False)
		assert summary["applied"] == 1
		assert summary["pruned"] == 1
		remaining = {r.id for r in parse_review(review)}
		assert remaining == {2, 3}  # pending + rejected kept

	def test_errored_entries_kept(self, tmp_path):
		"""An accept whose folder is missing errors out → not pruned, so the
		user can fix it and re-run."""
		library = tmp_path / "lib"
		library.mkdir()
		self._seed_book(library, 1)
		review = tmp_path / "review.yaml"
		_write_review(review, [
			{"id": 1, "path": "a1/b1", "current": {"title": "A"}, "proposed": {}, "action": "accept"},
			{"id": 9, "path": "nope/missing", "current": {"title": "X"}, "proposed": {}, "action": "accept"},
		])
		summary = apply_review(review, library, dry_run=False)
		assert summary["applied"] == 1
		assert summary["pruned"] == 1
		remaining = {r.id for r in parse_review(review)}
		assert remaining == {9}

	def test_dry_run_does_not_prune(self, tmp_path):
		library = tmp_path / "lib"
		self._seed_book(library, 1)
		review = tmp_path / "review.yaml"
		_write_review(review, [
			{"id": 1, "path": "a1/b1", "current": {"title": "A"}, "proposed": {}, "action": "accept"},
		])
		before = review.read_text(encoding="utf-8")
		summary = apply_review(review, library, dry_run=True)
		assert summary["pruned"] == 0
		assert review.read_text(encoding="utf-8") == before
