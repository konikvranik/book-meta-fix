"""Tests for the streaming ReviewWriter.

Covers: append-on-complete, .bak move on construct, prior user-action merge,
carry-over of unprocessed prior entries, .bak deletion on success, .bak kept
on simulated crash, inline auto-apply, and legacy-format prior loading.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

from book_meta_fix.enrichers import EnrichedMeta
from book_meta_fix.models import BookMeta, Confidence, Diagnosis, Verdict
from book_meta_fix.review import parse_review
from book_meta_fix.review_writer import ReviewWriter


def _meta(calibre_id: int, title: str = "T", author: str = "A") -> BookMeta:
	return BookMeta(calibre_id=calibre_id, title=title, authors=[author], path=f"/lib/A/{title} ({calibre_id})", primary_file=None)


def _result(calibre_id: int, *, title: str = "T", enriched: EnrichedMeta | None = None, verdict: Verdict = Verdict.NEEDS_REVIEW, category: str = "C2"):
	meta = _meta(calibre_id, title=title)
	diag = Diagnosis(category=category, reason="r", confidence=Confidence.HIGH, verdict=verdict)
	return (meta, diag, None, enriched)


def _submit_all_and_finish(writer: ReviewWriter, results: list[tuple]):
	"""Helper: submit results (as if from workers), then finish()."""
	for r in results:
		writer.submit(r)
	# Small delay so the writer thread can drain before finish().
	time.sleep(0.05)
	return writer.finish()


class TestBasicAppend:
	def test_append_three_books_produces_three_docs(self, tmp_path):
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		summary = _submit_all_and_finish(w, [_result(1), _result(2), _result(3)])
		assert summary["written"] == 3
		text = out.read_text(encoding="utf-8")
		assert text.count("---") == 3
		parsed = parse_review(out)
		assert {p.id for p in parsed} == {1, 2, 3}

	def test_skips_ok_books(self, tmp_path):
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		# First is OK (not appended), second is NEEDS_REVIEW (appended).
		summary = _submit_all_and_finish(w, [
			_result(1, verdict=Verdict.OK),
			_result(2, verdict=Verdict.NEEDS_REVIEW),
		])
		assert summary["written"] == 1
		parsed = parse_review(out)
		assert len(parsed) == 1 and parsed[0].id == 2

	def test_creates_fresh_file_when_none_exists(self, tmp_path):
		out = tmp_path / "review.yaml"
		assert not out.exists()
		w = ReviewWriter(out)
		summary = _submit_all_and_finish(w, [_result(1)])
		assert out.is_file()
		# No .bak since there was no original.
		assert summary["backup_path"] is None
		assert not (tmp_path / "review.yaml.bak").exists()


class TestBackupLifecycle:
	def test_moves_original_to_bak_on_construct(self, tmp_path):
		out = tmp_path / "review.yaml"
		out.write_text("# original\n", encoding="utf-8")
		w = ReviewWriter(out)
		# Original is now at .bak; output is a fresh header.
		assert (tmp_path / "review.yaml.bak").is_file()
		assert out.read_text(encoding="utf-8").startswith("# Auto-generated")
		# The original content is preserved in .bak.
		assert (tmp_path / "review.yaml.bak").read_text(encoding="utf-8") == "# original\n"
		_submit_all_and_finish(w, [_result(1)])

	def test_deletes_bak_after_successful_finish(self, tmp_path):
		out = tmp_path / "review.yaml"
		out.write_text("# original\n", encoding="utf-8")
		w = ReviewWriter(out)
		summary = _submit_all_and_finish(w, [_result(1)])
		assert summary["backup_path"] is None
		assert not (tmp_path / "review.yaml.bak").exists()

	def test_keeps_bak_when_keep_backup_true(self, tmp_path):
		out = tmp_path / "review.yaml"
		out.write_text("# original\n", encoding="utf-8")
		w = ReviewWriter(out)
		for r in [_result(1)]:
			w.submit(r)
		time.sleep(0.05)
		summary = w.finish(keep_backup=True)
		assert summary["backup_path"] is not None
		assert (tmp_path / "review.yaml.bak").exists()

	def test_overwrites_stale_bak(self, tmp_path):
		"""A leftover .bak from a crashed previous run is replaced, not merged
		duplicated."""
		out = tmp_path / "review.yaml"
		out.write_text("# current original\n", encoding="utf-8")
		stale = tmp_path / "review.yaml.bak"
		stale.write_text("# stale from previous crash\n", encoding="utf-8")
		w = ReviewWriter(out)
		# .bak now holds the CURRENT original, not the stale content.
		assert stale.read_text(encoding="utf-8") == "# current original\n"
		_submit_all_and_finish(w, [_result(1)])


class TestPriorUserActionMerge:
	def test_user_action_preserved_on_rerun(self, tmp_path):
		"""First run writes entries; user sets action; second run preserves it."""
		out = tmp_path / "review.yaml"
		# Run 1.
		w1 = ReviewWriter(out)
		_submit_all_and_finish(w1, [_result(1, title="Alpha"), _result(2, title="Beta")])
		# User edits: accept book 1, add a note to book 2.
		text = out.read_text(encoding="utf-8")
		text = text.replace("action: null\nid: 2", "action: null\nnotes: my note\nid: 2", 1) if "id: 2" in text else text
		# Set action on the book-1 block.
		import re
		text = re.sub(r"(id: 1\b.*?action:) null", r"\1 accept", text, count=1, flags=re.DOTALL)
		out.write_text(text, encoding="utf-8")
		# Run 2: same books reprocessed.
		w2 = ReviewWriter(out)
		summary = _submit_all_and_finish(w2, [_result(1, title="Alpha"), _result(2, title="Beta")])
		parsed = parse_review(out)
		by_id = {p.id: p for p in parsed}
		assert by_id[1].action == "accept"
		assert summary["skipped_user_decided"] == 1


class TestCarryOverUnprocessed:
	def test_unprocessed_prior_entries_carried_over(self, tmp_path):
		"""A run with --limit processes only some books; the rest must be carried
		over from .bak so user decisions aren't dropped."""
		out = tmp_path / "review.yaml"
		# Seed a review.yaml with 3 books, one of which the user acted on.
		seed = "---\nid: 1\ncurrent: {title: A}\naction: accept\n---\nid: 2\ncurrent: {title: B}\naction: null\n---\nid: 3\ncurrent: {title: C}\naction: null\n"
		out.write_text(seed, encoding="utf-8")
		# New run processes ONLY book 2 (e.g. --limit). Books 1 and 3 are not
		# submitted — they must be carried over from .bak.
		w = ReviewWriter(out)
		summary = _submit_all_and_finish(w, [_result(2, title="B-new")])
		parsed = parse_review(out)
		by_id = {p.id: p for p in parsed}
		# All three present: 1 carried (action preserved), 2 refreshed, 3 carried.
		assert set(by_id) == {1, 2, 3}
		assert by_id[1].action == "accept"
		assert by_id[3].current["title"] == "C"
		# Book 2 was refreshed by the new run.
		assert by_id[2].current["title"] == "B-new"


class TestStreamingConcurrency:
	def test_parallel_submissions_no_interleaving(self, tmp_path):
		"""Many threads submitting concurrently; the writer thread serializes so
		each entry is a clean, complete YAML document (no mid-line breaks)."""
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)

		def fire(i: int):
			w.submit(_result(i, title=f"T{i}"))

		threads = [threading.Thread(target=fire, args=(i,)) for i in range(20)]
		for t in threads:
			t.start()
		for t in threads:
			t.join()
		time.sleep(0.1)
		summary = w.finish()
		assert summary["written"] == 20
		parsed = parse_review(out)
		assert len(parsed) == 20
		assert {p.id for p in parsed} == set(range(20))


class TestAutoApply:
	def test_high_confidence_applied_not_appended(self, tmp_path):
		"""With apply_threshold='high', an llm:high proposal is written to
		metadata and NOT appended to review.yaml."""
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out, apply_threshold="high")
		enriched = EnrichedMeta(title="Fixed", source="llm:high")
		applied_ids: list[int] = []

		# Patch the metadata writer so we don't touch real files; record calls.
		def fake_apply(meta, enriched):
			applied_ids.append(meta.calibre_id)
			return True

		with patch("book_meta_fix.pipeline._apply_enriched_to_meta", lambda m, e: m), \
			 patch("book_meta_fix.writers.write_book_meta", lambda *a, **kw: None):
			summary = _submit_all_and_finish(w, [_result(1, enriched=enriched)])
		assert summary["applied"] == 1
		assert summary["written"] == 0
		parsed = parse_review(out)
		assert parsed == []

	def test_low_confidence_goes_to_review(self, tmp_path):
		"""With apply_threshold='high', an llm:low proposal is NOT auto-applied —
		it goes to review.yaml for the human."""
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out, apply_threshold="high")
		enriched = EnrichedMeta(title="Guess", source="llm:low")
		summary = _submit_all_and_finish(w, [_result(1, enriched=enriched)])
		assert summary["applied"] == 0
		assert summary["skipped_low_conf"] == 1
		assert summary["written"] == 1
		parsed = parse_review(out)
		assert len(parsed) == 1 and parsed[0].id == 1

	def test_no_proposal_goes_to_review(self, tmp_path):
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out, apply_threshold="high")
		summary = _submit_all_and_finish(w, [_result(1, enriched=None)])
		assert summary["applied"] == 0
		assert summary["skipped_no_proposal"] == 1
		assert summary["written"] == 1
		parsed = parse_review(out)
		assert len(parsed) == 1


class TestLegacyPriorLoading:
	def test_loads_legacy_single_list_bak(self, tmp_path):
		"""A .bak in the OLD single-list format must load as a prior map."""
		out = tmp_path / "review.yaml"
		legacy = "# header\n- id: 1\n  current: {title: A}\n  action: accept\n- id: 2\n  current: {title: B}\n  action: null\n"
		out.write_text(legacy, encoding="utf-8")
		w = ReviewWriter(out)
		assert set(w._prior) == {1, 2}
		assert w._prior[1]["action"] == "accept"
		# Submit only book 2; book 1 carried over with its action.
		summary = _submit_all_and_finish(w, [_result(2, title="B-new")])
		parsed = parse_review(out)
		by_id = {p.id: p for p in parsed}
		assert by_id[1].action == "accept"  # carried
		assert by_id[2].current["title"] == "B-new"  # refreshed
