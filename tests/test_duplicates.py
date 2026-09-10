"""Tests for the C19 duplicate-folder pass (duplicates.py).

scan_duplicates is a pure engine (BookMeta lists, no I/O); the proposal
writer mirrors filecheck.merge_file_deletions's three-way contract
(fresh pre-filled / pending overlay / decided untouched).
"""
from __future__ import annotations

from pathlib import Path

from book_meta_fix.duplicates import (
	DuplicateFinding,
	merge_duplicate_proposals,
	scan_duplicates,
)
from book_meta_fix.models import BookMeta


def _meta(
	cid: int,
	path: str,
	*,
	title: str = "Babička",
	author: str = "Božena Němcová",
	isbn: str | None = None,
	year: int | None = None,
	uuid: str | None = None,
	formats: list[str] | None = None,
) -> BookMeta:
	"""Minimal scanned-book stand-in: formats non-empty (a live record)."""
	return BookMeta(
		calibre_id=cid,
		title=title,
		authors=[author],
		path=path,
		isbn=isbn,
		year=year,
		uuid=uuid or f"u{cid}",
		formats=formats if formats is not None else [".epub"],
	)


class TestScanDuplicates:
	def test_folded_title_author_pair(self, tmp_path: Path) -> None:
		"""Same author+title after diacritics/case folding, different ids →
		one finding; the LOWEST calibre_id survives (no ISBN on either)."""
		a = _meta(100, str(tmp_path / "Author/Babička (100)"))
		b = _meta(200, str(tmp_path / "author/babicka (200)"), author="Bozena Nemcova")
		findings = scan_duplicates([a, b], library_root=tmp_path)
		assert len(findings) == 1
		f = findings[0]
		assert f.uuid == "u200"
		assert f.merge_into == "Author/Babička (100)"
		assert f.survivor_uuid == "u100"
		assert not f.isbn_confirmed

	def test_survivor_prefers_valid_isbn_over_lower_id(self, tmp_path: Path) -> None:
		"""_pick_base semantics: a record with a valid ISBN survives even
		against a lower calibre_id."""
		a = _meta(100, str(tmp_path / "A/Babička (100)"))
		b = _meta(200, str(tmp_path / "A/Babička (200)"), isbn="9780306406157")
		findings = scan_duplicates([a, b], library_root=tmp_path)
		assert len(findings) == 1
		assert findings[0].uuid == "u100"
		assert findings[0].survivor_uuid == "u200"
		assert findings[0].isbn_confirmed is False  # loser has no ISBN to confirm

	def test_both_isbn_equal_confirms(self, tmp_path: Path) -> None:
		a = _meta(100, str(tmp_path / "A/Babička (100)"), isbn="978-0-306-40615-7")
		b = _meta(200, str(tmp_path / "A/Babička (200)"), isbn="9780306406157")
		findings = scan_duplicates([a, b], library_root=tmp_path)
		assert len(findings) == 1
		assert findings[0].isbn_confirmed is True

	def test_isbn_groups_different_titles(self, tmp_path: Path) -> None:
		"""The ISBN key catches a duplicate whose title was corrupted in one
		folder — same_book treats ISBN as the strongest signal."""
		a = _meta(100, str(tmp_path / "A/Saturnin (100)"), title="Saturnin", isbn="9780306406157")
		b = _meta(200, str(tmp_path / "A/Babička (200)"), isbn="9780306406157")
		findings = scan_duplicates([a, b], library_root=tmp_path)
		assert len(findings) == 1
		assert findings[0].isbn_confirmed is True

	def test_different_editions_not_proposed(self, tmp_path: Path) -> None:
		"""Both years present and different → different editions, same_book's
		year tie-breaker keeps them apart — no finding at all."""
		a = _meta(100, str(tmp_path / "A/Babička (100)"), year=1995)
		b = _meta(200, str(tmp_path / "A/Babička (200)"), year=2010)
		assert scan_duplicates([a, b], library_root=tmp_path) == []

	def test_one_year_missing_merges(self, tmp_path: Path) -> None:
		a = _meta(100, str(tmp_path / "A/Babička (100)"))
		b = _meta(200, str(tmp_path / "A/Babička (200)"), year=2010)
		assert len(scan_duplicates([a, b], library_root=tmp_path)) == 1

	def test_dead_records_excluded(self, tmp_path: Path) -> None:
		"""A folder without format files is an EMPTY_BOOK dead record — it
		must neither be a loser nor a survivor."""
		a = _meta(100, str(tmp_path / "A/Babička (100)"))
		dead = _meta(200, str(tmp_path / "A/Babička (200)"), formats=[])
		assert scan_duplicates([a, dead], library_root=tmp_path) == []

	def test_three_books_two_findings_one_survivor(self, tmp_path: Path) -> None:
		a = _meta(100, str(tmp_path / "A/Babička (100)"))
		b = _meta(200, str(tmp_path / "A/Babička (200)"))
		c = _meta(300, str(tmp_path / "A/Babička (300)"))
		findings = scan_duplicates([a, b, c], library_root=tmp_path)
		assert sorted(f.uuid for f in findings) == ["u200", "u300"]
		assert {f.merge_into for f in findings} == {"A/Babička (100)"}

	def test_pair_via_both_keys_reported_once(self, tmp_path: Path) -> None:
		"""Same title+author AND same ISBN — the loser is proposed once, not
		twice through the two grouping keys."""
		a = _meta(100, str(tmp_path / "A/Babička (100)"), isbn="9780306406157")
		b = _meta(200, str(tmp_path / "A/Babička (200)"), isbn="9780306406157")
		findings = scan_duplicates([a, b], library_root=tmp_path)
		assert len(findings) == 1

	def test_unrelated_books_untouched(self, tmp_path: Path) -> None:
		a = _meta(100, str(tmp_path / "A/Babička (100)"))
		b = _meta(200, str(tmp_path / "A/Saturnin (200)"), title="Saturnin")
		assert scan_duplicates([a, b], library_root=tmp_path) == []


class TestMergeDuplicateProposals:
	def _write_review(self, path: Path, entries: list[dict]) -> None:
		import yaml

		body = "".join(f"---\n{yaml.safe_dump(e, sort_keys=False)}" for e in entries)
		path.write_text(body, encoding="utf-8")

	def test_fresh_isbn_confirmed_entry_prefilled_merge(self, tmp_path: Path) -> None:
		from book_meta_fix.review import parse_review

		review = tmp_path / "review.yaml"
		loser = _meta(200, str(tmp_path / "A/Babička (200)"), isbn="9780306406157")
		finding = DuplicateFinding(
			path=Path(loser.path), uuid="u200",
			merge_into="A/Babička (100)", survivor_uuid="u100",
			reason="duplicate of A/Babička (100): identical ISBN 9780306406157",
			isbn_confirmed=True,
		)
		summary = merge_duplicate_proposals(review, [finding], [loser], library_root=tmp_path)
		assert summary == {"added": 1, "updated": 0, "skipped_decided": 0}
		items = parse_review(review)
		assert len(items) == 1
		assert items[0].action == "merge"
		assert items[0].proposed["merge_into"] == "A/Babička (100)"
		assert items[0].diagnosis["category"] == "C19"
		assert items[0].diagnosis["confidence"] == "HIGH"
		assert not items[0].verified

	def test_fresh_unconfirmed_entry_stays_pending(self, tmp_path: Path) -> None:
		from book_meta_fix.review import parse_review

		review = tmp_path / "review.yaml"
		loser = _meta(200, str(tmp_path / "A/Babička (200)"))
		finding = DuplicateFinding(
			path=Path(loser.path), uuid="u200",
			merge_into="A/Babička (100)", survivor_uuid="u100",
			reason="duplicate of A/Babička (100): identical author + title",
			isbn_confirmed=False,
		)
		summary = merge_duplicate_proposals(review, [finding], [loser], library_root=tmp_path)
		assert summary["added"] == 1
		items = parse_review(review)
		assert items[0].action is None
		assert items[0].diagnosis["confidence"] == "MEDIUM"

	def test_pending_entry_gets_overlay_not_decision(self, tmp_path: Path) -> None:
		from book_meta_fix.review import parse_review

		review = tmp_path / "review.yaml"
		self._write_review(review, [{
			"id": 200, "uuid": "u200", "path": "A/Babička (200)",
			"diagnosis": {"category": "C2", "reason": "filename as title"},
			"current": {"title": "Babička"}, "proposed": {"title": "Babička"},
			"action": None,
		}])
		loser = _meta(200, str(tmp_path / "A/Babička (200)"))
		finding = DuplicateFinding(
			path=Path(loser.path), uuid="u200",
			merge_into="A/Babička (100)", survivor_uuid="u100",
			reason="duplicate of A/Babička (100): identical author + title",
		)
		summary = merge_duplicate_proposals(review, [finding], [loser], library_root=tmp_path)
		assert summary == {"added": 0, "updated": 1, "skipped_decided": 0}
		items = parse_review(review)
		assert len(items) == 1
		assert items[0].action is None  # decision never injected into a pending entry
		assert items[0].proposed["merge_into"] == "A/Babička (100)"
		assert items[0].proposed["title"] == "Babička"  # prior overlay survives
		assert [d["category"] for d in items[0].diagnoses] == ["C2", "C19"]

	def test_decided_entry_is_never_touched(self, tmp_path: Path) -> None:
		review = tmp_path / "review.yaml"
		self._write_review(review, [{
			"id": 200, "uuid": "u200", "path": "A/Babička (200)",
			"diagnosis": {"category": "C2", "reason": "x"},
			"current": {"title": "Babička"}, "action": "keep",
		}])
		before = review.read_text(encoding="utf-8")
		loser = _meta(200, str(tmp_path / "A/Babička (200)"))
		finding = DuplicateFinding(
			path=Path(loser.path), uuid="u200",
			merge_into="A/Babička (100)", survivor_uuid="u100",
			reason="duplicate of A/Babička (100): identical author + title",
			isbn_confirmed=True,
		)
		summary = merge_duplicate_proposals(review, [finding], [loser], library_root=tmp_path)
		assert summary == {"added": 0, "updated": 0, "skipped_decided": 1}
		assert review.read_text(encoding="utf-8") == before

	def test_no_findings_leaves_file_alone(self, tmp_path: Path) -> None:
		review = tmp_path / "review.yaml"
		review.write_text("---\nid: 1\nuuid: u1\npath: x\naction: null\n", encoding="utf-8")
		before = review.read_text(encoding="utf-8")
		summary = merge_duplicate_proposals(review, [], [], library_root=tmp_path)
		assert summary == {"added": 0, "updated": 0, "skipped_decided": 0}
		assert review.read_text(encoding="utf-8") == before
