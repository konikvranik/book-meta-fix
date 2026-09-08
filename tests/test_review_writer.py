"""Tests for the streaming ReviewWriter.

Covers: append-on-complete, .bak move on construct, prior user-action merge,
carry-over of unprocessed prior entries, .bak deletion on success, .bak kept
on simulated crash, and legacy-format prior loading.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from book_meta_fix.enrichers import EnrichedMeta
from book_meta_fix.models import BookMeta, Confidence, Diagnosis, Verdict
from book_meta_fix.review import parse_review
from book_meta_fix.review_writer import ReviewWriter


def _meta(calibre_id: int, title: str = "T", author: str = "A") -> BookMeta:
	return BookMeta(calibre_id=calibre_id, uuid=f"u{calibre_id}", title=title, authors=[author], path=f"/lib/A/{title} ({calibre_id})", primary_file=None)


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


class TestMediumConfidencePrefill:
	"""Medium-confidence proposals (llm:flash/loop, openlibrary, content) get
	action: accept pre-filled ONLY when they don't change title or author.
	Proposals that change title/author stay action=None for individual review.
	"""

	def test_flash_adding_only_isbn_gets_accept(self, tmp_path):
		"""llm:flash proposal that keeps title/author and only adds ISBN ->
		prefilled accept (the added metadata is easy to bulk-verify)."""
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		enriched = EnrichedMeta(
			title="T", authors=["A"], isbn="9781111111111", source="llm:flash",
		)
		_submit_all_and_finish(w, [_result(1, title="T", enriched=enriched)])
		parsed = parse_review(out)
		assert parsed[0].action == "accept"

	def test_flash_adding_only_genres_gets_accept(self, tmp_path):
		"""llm:flash that adds only genres (no title/author/isbn change) is the
		most common case in the real library (~700 books) — prefill accept."""
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		enriched = EnrichedMeta(
			title="T", authors=["A"], genres=["fantasy"], source="llm:flash",
		)
		_submit_all_and_finish(w, [_result(1, title="T", enriched=enriched)])
		parsed = parse_review(out)
		assert parsed[0].action == "accept"

	def test_flash_changing_title_stays_none(self, tmp_path):
		"""llm:flash proposing ONLY a different title (no additive data) -> action
		stays None for review — nothing safe to auto-apply."""
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		enriched = EnrichedMeta(
			title="New Title", authors=["A"], source="llm:flash",
		)
		_submit_all_and_finish(w, [_result(1, title="Old Title", enriched=enriched)])
		parsed = parse_review(out)
		assert parsed[0].action is None

	def test_flash_changing_title_with_additive_stays_none(self, tmp_path):
		"""llm:flash proposing a title change AND an isbn: the match is on an
		unconfirmed identity (query built on the old title), so we cannot trust
		the additive isbn either — it may belong to the wrong book. Whole
		proposal stays action=None for review."""
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		enriched = EnrichedMeta(
			title="New Title", authors=["A"], isbn="9782222222222", source="llm:flash",
		)
		_submit_all_and_finish(w, [_result(1, title="Old Title", enriched=enriched)])
		parsed = parse_review(out)
		entry = parsed[0]
		assert entry.action is None
		# Nothing stripped — the full proposal (title + isbn) is preserved for
		# the human to review together.
		assert entry.proposed.get("title") == "New Title"
		assert entry.proposed.get("isbn") == "9782222222222"

	def test_openlibrary_changing_title_with_year_stays_none(self, tmp_path):
		"""openlibrary (medium) proposing a title fix for a broken current title
		+ adding year: the match is on the (broken) current title, so identity is
		unconfirmed → defer everything (additive year not trustworthy either)."""
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		enriched = EnrichedMeta(
			title="New Title", authors=["A"], year=2005, source="openlibrary",
		)
		# Broken current title (underscore) so _looks_better lets the enriched
		# title into the proposal — otherwise openlibrary (not trust-blindly)
		# wouldn't propose a title change at all.
		_submit_all_and_finish(w, [_result(1, title="Old_Title", enriched=enriched)])
		parsed = parse_review(out)
		entry = parsed[0]
		assert entry.action is None
		assert entry.proposed.get("title") == "New Title"

	def test_identity_confirmed_auto_accepts_identity_change(self, tmp_path):
		"""An identity_confirmed proposal (verified against the book's content)
		auto-accepts even when it changes title/author — we know the book."""
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		enriched = EnrichedMeta(
			title="New Title", authors=["New Author"], isbn="9782222222222",
			source="openlibrary", identity_confirmed=True,
		)
		_submit_all_and_finish(w, [_result(1, title="Old Title", enriched=enriched)])
		parsed = parse_review(out)
		entry = parsed[0]
		assert entry.action == "accept"
		# The identity change is present in proposed (not stripped).
		assert entry.proposed.get("title") == "New Title"

	def test_flash_changing_author_stays_none(self, tmp_path):
		"""llm:flash proposing a different author -> action stays None."""
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		enriched = EnrichedMeta(
			title="T", authors=["New Author"], source="llm:flash",
		)
		_submit_all_and_finish(w, [_result(1, title="T", enriched=enriched)])
		parsed = parse_review(out)
		assert parsed[0].action is None

	def test_openlibrary_preserving_identity_gets_accept(self, tmp_path):
		"""openlibrary is medium-confidence; same rule applies."""
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		enriched = EnrichedMeta(
			title="T", authors=["A"], isbn="9782222222222", source="openlibrary",
		)
		_submit_all_and_finish(w, [_result(1, title="T", enriched=enriched)])
		parsed = parse_review(out)
		assert parsed[0].action == "accept"

	def test_llm_low_never_prefilled(self, tmp_path):
		"""llm:low (failed verify_proposal) is always action=None, even when
		it preserves identity — we don't trust a proposal the LLM's own verify
		already rejected."""
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		enriched = EnrichedMeta(
			title="T", authors=["A"], genres=["x"], source="llm:low",
		)
		_submit_all_and_finish(w, [_result(1, title="T", enriched=enriched)])
		parsed = parse_review(out)
		assert parsed[0].action is None


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


class TestCarryOverPathRefresh:
	def test_prior_decision_refreshes_path_after_move(self, tmp_path):
		"""A prior user decision is matched by uuid (so it survives an organize
		move), and on re-analyze the entry's path is refreshed to the book's
		current on-disk location — otherwise apply could not find the book."""
		out = tmp_path / "review.yaml"
		# Prior run: book u1 reviewed at its OLD path, user set action.
		seed = "---\nid: 1\nuuid: u1\npath: old/A (1)\ncurrent: {title: T}\naction: accept\n"
		out.write_text(seed, encoding="utf-8")
		w = ReviewWriter(out)
		# Same book (uuid u1) now lives at a new path (organize relocated it).
		meta = BookMeta(calibre_id=1, uuid="u1", title="T", authors=["A"], path="/lib/needfix/A (1)", primary_file=None)
		diag = Diagnosis(category="C2", reason="r", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)
		_submit_all_and_finish(w, [(meta, diag, None, None)])
		parsed = parse_review(out)
		by_uuid = {p.uuid: p for p in parsed}
		assert by_uuid["u1"].action == "accept"   # prior decision preserved
		assert "needfix" in by_uuid["u1"].path    # path refreshed to current location


class TestCarryOverUnprocessed:
	def test_unprocessed_prior_entries_carried_over(self, tmp_path):
		"""A run with --limit processes only some books; the rest must be carried
		over from .bak so user decisions aren't dropped."""
		out = tmp_path / "review.yaml"
		# Seed a review.yaml with 3 books, one of which the user acted on.
		seed = "---\nid: 1\nuuid: u1\ncurrent: {title: A}\naction: accept\n---\nid: 2\nuuid: u2\ncurrent: {title: B}\naction: null\n---\nid: 3\nuuid: u3\ncurrent: {title: C}\naction: null\n"
		out.write_text(seed, encoding="utf-8")
		# New run processes ONLY book 2 (e.g. --limit). Books 1 and 3 are not
		# submitted — they must be carried over from .bak.
		w = ReviewWriter(out)
		_submit_all_and_finish(w, [_result(2, title="B-new")])
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


class TestReviewOnlyNoMetadataWrites:
	"""auto-apply was removed: ReviewWriter must never write metadata files,
	and ``apply_threshold`` is no longer a constructor parameter."""

	def test_apply_threshold_param_removed(self, tmp_path):
		out = tmp_path / "review.yaml"
		with pytest.raises(TypeError):
			ReviewWriter(out, apply_threshold="high")  # type: ignore[misc]

	def test_never_writes_metadata(self, tmp_path):
		"""Even a high-confidence proposal must land in review.yaml, not on disk."""
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		enriched = EnrichedMeta(title="Fixed", source="llm:high")
		with patch("book_meta_fix.writers.write_book_meta") as fake_write:
			summary = _submit_all_and_finish(w, [_result(1, enriched=enriched)])
		assert fake_write.call_count == 0
		assert summary["written"] == 1
		parsed = parse_review(out)
		assert len(parsed) == 1 and parsed[0].id == 1


class TestAutoFixable:
	"""AUTO_FIXABLE books (C6 Word lock-file -> delete; MISSING_* -> no action)
	must be appended to review.yaml instead of being silently dropped."""

	def test_c6_delete_action_pre_filled(self, tmp_path):
		"""C6 carries proposed={"action": "delete"} on the Diagnosis; the writer
		pre-fills entry.action so the user only has to confirm."""
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		meta = _meta(1, title="~$doc")
		diag = Diagnosis(
			category="C6", reason="word lockfile", confidence=Confidence.HIGH,
			verdict=Verdict.AUTO_FIXABLE,
			proposed={"action": "delete", "reason": "duplicate of a Word lock file"},
		)
		summary = _submit_all_and_finish(w, [(meta, diag, None, None)])
		assert summary["written"] == 1
		parsed = parse_review(out)
		assert len(parsed) == 1
		assert parsed[0].action == "delete"
		# Extra keys from diag.proposed merged into the proposed block.
		assert parsed[0].proposed is not None
		assert parsed[0].proposed.get("reason") == "duplicate of a Word lock file"

	def test_missing_isbn_no_proposal_pre_fills_accept(self, tmp_path):
		"""MISSING_ISBN with no proposal and no NEEDS_REVIEW co-diagnosis gets
		action: accept pre-filled — unactionable noise for the user (nothing
		to apply); accept is a no-op prune in bmf apply."""
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		meta = _meta(1, title="Real Book")
		diag = Diagnosis(category="MISSING_ISBN", reason="no isbn", confidence=Confidence.LOW, verdict=Verdict.AUTO_FIXABLE)
		summary = _submit_all_and_finish(w, [(meta, diag, None, None)])
		assert summary["written"] == 1
		parsed = parse_review(out)
		assert len(parsed) == 1 and parsed[0].action == "accept"

	def test_missing_isbn_with_needs_review_codiag_stays_null(self, tmp_path):
		"""MISSING_ISBN blocked by a NEEDS_REVIEW co-diagnosis must NOT get
		auto-accept — the user still needs to deal with the co-diagnosis."""
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		meta = _meta(1, title="Real Book")
		cover_diag = Diagnosis(
			category="C11", reason="generated cover", confidence=Confidence.HIGH,
			verdict=Verdict.NEEDS_REVIEW,
		)
		diag = Diagnosis(
			category="MISSING_ISBN", reason="no isbn", confidence=Confidence.LOW,
			verdict=Verdict.AUTO_FIXABLE, additional=[cover_diag],
		)
		summary = _submit_all_and_finish(w, [(meta, diag, None, None)])
		assert summary["written"] == 1
		parsed = parse_review(out)
		assert len(parsed) == 1 and parsed[0].action is None

	def test_prior_user_decision_overrides_pre_filled_action(self, tmp_path):
		"""If the user already set action on a C6 book in a prior run, that
		decision wins over the pre-filled delete."""
		out = tmp_path / "review.yaml"
		seed = "---\nid: 1\nuuid: u1\ncurrent: {title: A}\naction: accept\n"
		out.write_text(seed, encoding="utf-8")
		w = ReviewWriter(out)
		meta = _meta(1, title="~$doc")
		diag = Diagnosis(
			category="C6", reason="word lockfile", confidence=Confidence.HIGH,
			verdict=Verdict.AUTO_FIXABLE,
			proposed={"action": "delete"},
		)
		summary = _submit_all_and_finish(w, [(meta, diag, None, None)])
		assert summary["skipped_user_decided"] == 1
		parsed = parse_review(out)
		assert parsed[0].action == "accept"  # user decision preserved


class TestLegacyPriorLoading:
	def test_loads_single_list_bak_keyed_by_uuid(self, tmp_path):
		"""A .bak in the single-list format (vs the multi-doc stream) still loads
		as a prior map, now keyed by uuid (the carry-over identity)."""
		out = tmp_path / "review.yaml"
		legacy = "# header\n- id: 1\n  uuid: u1\n  current: {title: A}\n  action: accept\n- id: 2\n  uuid: u2\n  current: {title: B}\n  action: null\n"
		out.write_text(legacy, encoding="utf-8")
		w = ReviewWriter(out)
		assert set(w._prior) == {"u1", "u2"}
		assert w._prior["u1"]["action"] == "accept"
		# Submit only book 2; book 1 carried over with its action.
		_submit_all_and_finish(w, [_result(2, title="B-new")])
		parsed = parse_review(out)
		by_id = {p.id: p for p in parsed}
		assert by_id[1].action == "accept"  # carried
		assert by_id[2].current["title"] == "B-new"  # refreshed

	def test_truly_uuidless_legacy_entries_skipped(self, tmp_path):
		"""Clean break: a genuine legacy .bak whose entries predate uuid keying
		(carry no uuid) cannot be matched, so they are dropped from the prior map
		and re-decided fresh rather than guessed wrong."""
		out = tmp_path / "review.yaml"
		legacy = "# header\n- id: 1\n  current: {title: A}\n  action: accept\n"
		out.write_text(legacy, encoding="utf-8")
		w = ReviewWriter(out)
		assert w._prior == {}  # uuid-less entry not carryable


class TestIdentityConfirmedAccept:
	"""MISSING_* books whose identity was confirmed against the book's content
	get action: accept pre-filled so `bmf apply` prunes them. Covers both the
	no-proposal accept-as-is case (new branch) and the with-other-metadata case
	(existing path — nothing is thrown away)."""

	def test_identity_confirmed_no_proposal_prefills_accept(self, tmp_path):
		"""A minimal identity_confirmed EnrichedMeta with no fields -> empty
		proposed, but action: accept is still pre-filled (accept-as-is)."""
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		em = EnrichedMeta(identity_confirmed=True, source="content")
		_submit_all_and_finish(w, [_result(1, verdict=Verdict.AUTO_FIXABLE, category="MISSING_ISBN", enriched=em)])
		parsed = parse_review(out)
		assert len(parsed) == 1
		assert parsed[0].action == "accept"
		# No fake proposed fields — it's an accept-as-is, not a change.
		assert not parsed[0].proposed

	def test_identity_confirmed_with_other_metadata_is_proposed(self, tmp_path):
		"""When an enricher DID return data (publisher here) alongside an
		identity confirmation, that metadata is proposed and action: accept is
		pre-filled (existing path) — it is applied, not discarded."""
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		em = EnrichedMeta(identity_confirmed=True, source="databazeknih", publisher="Argo")
		_submit_all_and_finish(w, [_result(1, verdict=Verdict.AUTO_FIXABLE, category="MISSING_ISBN", enriched=em)])
		parsed = parse_review(out)
		assert len(parsed) == 1
		assert parsed[0].action == "accept"
		assert parsed[0].proposed and parsed[0].proposed.get("publisher") == "Argo"

	def test_identity_confirmed_missing_year_also_accepted(self, tmp_path):
		"""The accept-as-is path applies to any MISSING_* category, not just
		MISSING_ISBN. MISSING_YEAR + identity_confirmed -> accept."""
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		em = EnrichedMeta(identity_confirmed=True, source="content")
		_submit_all_and_finish(w, [_result(1, verdict=Verdict.AUTO_FIXABLE, category="MISSING_YEAR", enriched=em)])
		parsed = parse_review(out)
		assert len(parsed) == 1 and parsed[0].action == "accept"


class TestCoverOnlyAccept:
	"""A cover-diagnosis entry (C11 / MISSING_COVER) with no other proposed
	change pre-fills action: accept so the cover is recovered in bulk —
	downloaded if a cover_url exists, otherwise extracted from the book file by
	_apply_action. The book's own cover carries no identity risk, so this
	pre-fills even without an identity confirmation."""

	def test_missing_cover_empty_proposal_prefills_accept(self, tmp_path):
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		_submit_all_and_finish(w, [_result(1, verdict=Verdict.AUTO_FIXABLE, category="MISSING_COVER")])
		parsed = parse_review(out)
		assert len(parsed) == 1
		assert parsed[0].action == "accept"

	def test_c11_empty_proposal_prefills_accept(self, tmp_path):
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		_submit_all_and_finish(w, [_result(1, verdict=Verdict.NEEDS_REVIEW, category="C11")])
		parsed = parse_review(out)
		assert len(parsed) == 1
		assert parsed[0].action == "accept"


class TestLocationPrefill:
	"""C13 (location mismatch) with benign extras pre-fills accept: the
	metadata is fine, apply just moves the folder. A cover-diagnosis extra
	(C11 / MISSING_COVER) is tolerated too — apply's cover recovery runs for
	a cover category anywhere in the entry's list, and refusing would freeze
	the book (C13 outranks the enrichment rules, so the location rule would
	shadow the cover problem on every run). A real metadata problem, or a
	proposal that changes title/author, keeps action: null."""

	def _c13_result(self, calibre_id: int, *, additional: list[Diagnosis] | None = None, enriched: EnrichedMeta | None = None):
		meta = _meta(calibre_id)
		diag = Diagnosis(
			category="C13", reason="umístění", confidence=Confidence.HIGH,
			verdict=Verdict.AUTO_FIXABLE, proposed={"location": f"A/T ({calibre_id})"},
			additional=additional or [],
		)
		return (meta, diag, None, enriched)

	def test_c13_only_prefills_accept_with_location(self, tmp_path):
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		_submit_all_and_finish(w, [self._c13_result(1)])
		parsed = parse_review(out)
		assert parsed[0].action == "accept"
		assert parsed[0].proposed["location"] == "A/T (1)"

	def test_c13_with_acceptable_missing_prefills_accept(self, tmp_path):
		extra = Diagnosis(category="MISSING_COVER", reason="no cover", confidence=Confidence.LOW, verdict=Verdict.AUTO_FIXABLE)
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		_submit_all_and_finish(w, [self._c13_result(1, additional=[extra])])
		parsed = parse_review(out)
		assert parsed[0].action == "accept"

	def test_c13_with_generated_cover_prefills_accept(self, tmp_path):
		"""C13 + C11 (the dominant real-world combination: a misplaced book
		with a calibre-generated cover): accept pre-fills — apply moves the
		folder AND retries the cover in the same pass."""
		extra = Diagnosis(category="C11", reason="generated cover", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		_submit_all_and_finish(w, [self._c13_result(1, additional=[extra])])
		parsed = parse_review(out)
		assert parsed[0].action == "accept"

	def test_c13_with_real_problem_stays_null(self, tmp_path):
		extra = Diagnosis(category="C2", reason="filename as title", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		_submit_all_and_finish(w, [self._c13_result(1, additional=[extra])])
		parsed = parse_review(out)
		assert parsed[0].action is None

	def test_c13_cover_extra_with_identity_change_stays_null(self, tmp_path):
		"""A C13+C11 entry whose proposal CHANGES the title must not accept:
		a mere-move accept must not apply an unconfirmed identity change.
		(llm:low is below the pre-fill thresholds, so the ladder above leaves
		the action to this branch.)"""
		extra = Diagnosis(category="C11", reason="generated cover", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)
		enriched = EnrichedMeta(title="Úplně jiný titul", authors=["Jiný Autor"], source="llm:low")
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		_submit_all_and_finish(w, [self._c13_result(1, additional=[extra], enriched=enriched)])
		parsed = parse_review(out)
		assert parsed[0].action is None

	def test_empty_book_prefills_accept_despite_needs_review_extras(self, tmp_path):
		"""EMPTY_BOOK pre-fills accept unconditionally: with the book file
		gone, the other rules fire on the leftover sidecar metadata and none
		of those verdicts matters (the accept only parks the record in
		needfix/empty/). Without this, an empty Anonym-authored record kept
		its C9 NEEDS_REVIEW extra and never left review."""
		meta = _meta(1)
		diag = Diagnosis(
			category="EMPTY_BOOK", reason="no ebook file", confidence=Confidence.HIGH,
			verdict=Verdict.AUTO_FIXABLE,
			additional=[
				Diagnosis(category="C9", reason="anonym", confidence=Confidence.MEDIUM, verdict=Verdict.NEEDS_REVIEW),
				Diagnosis(category="C13", reason="umístění", confidence=Confidence.HIGH, verdict=Verdict.AUTO_FIXABLE),
				Diagnosis(category="MISSING_ISBN", reason="no isbn", confidence=Confidence.LOW, verdict=Verdict.AUTO_FIXABLE),
			],
		)
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		_submit_all_and_finish(w, [(meta, diag, None, None)])
		parsed = parse_review(out)
		assert parsed[0].action == "accept"

	def test_prior_verified_carried_onto_fresh_entry(self, tmp_path):
		"""An UNDECIDED prior entry carrying verified: true keeps the mark when
		the book is re-analyzed (decided priors are carried verbatim anyway)."""
		import yaml

		out = tmp_path / "review.yaml"
		out.write_text("---\n" + yaml.safe_dump({
			"id": 1, "uuid": "u1", "path": "a", "diagnosis": {"category": "C2"},
			"current": {}, "verified": True, "action": None,
		}, sort_keys=False), encoding="utf-8")
		w = ReviewWriter(out)
		_submit_all_and_finish(w, [self._c13_result(1)])
		parsed = parse_review(out)
		assert parsed[0].verified is True



class TestProjectedVerified:
	"""Auto `verified` pre-fill: when the analyzer's own proposal completes
	the book (the projected post-apply state is detector-clean), the entry
	carries verified: true so apply fixes AND closes it in one pass."""

	def _book_folder(self, tmp_path, *, with_year=False, title="Kniha"):
		import json as _json

		from book_meta_fix.readers import read_book_folder

		folder = tmp_path / "lib" / "Jan Novak" / "Kniha (7)"
		folder.mkdir(parents=True)
		manifest = {"title": title, "authors": ["Jan Novak"], "isbn": "9788020403117"}
		if with_year:
			manifest["publishedYear"] = "2001"
		(folder / "metadata.json").write_text(_json.dumps(manifest), encoding="utf-8")
		(folder / "book.epub").write_text("x", encoding="utf-8")
		(folder / "cover.jpg").write_bytes(b"cover")
		return read_book_folder(folder)

	def test_completing_proposal_prefills_verified(self, tmp_path):
		"""MISSING_YEAR + a databazeknih year proposal: after apply the book
		is complete → accept AND verified are pre-filled."""
		meta = self._book_folder(tmp_path)  # no year
		diag = Diagnosis(category="MISSING_YEAR", reason="no year", confidence=Confidence.LOW, verdict=Verdict.AUTO_FIXABLE)
		enriched = EnrichedMeta(title=None, authors=[], year=2001, source="databazeknih")
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		_submit_all_and_finish(w, [(meta, diag, None, enriched)])
		parsed = parse_review(out)
		assert parsed[0].action == "accept"
		assert parsed[0].proposed["year"] == 2001
		assert parsed[0].verified is True

	def test_unresolved_problem_no_verified(self, tmp_path):
		"""A proposal that leaves a NEEDS_REVIEW problem (C2 title) standing
		does NOT pre-fill verified — the book stays open for review."""
		meta = self._book_folder(tmp_path, title="soubor_epub.epub", with_year=True)
		diag = Diagnosis(category="C2", reason="filename title", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		_submit_all_and_finish(w, [(meta, diag, None, None)])
		parsed = parse_review(out)
		assert parsed[0].verified is False

	def test_cover_url_credited(self, tmp_path):
		"""A proposed cover_url counts as resolving MISSING_COVER — the
		download happens at apply."""
		meta = self._book_folder(tmp_path, with_year=True)
		(folder := tmp_path / "lib" / "Jan Novak" / "Kniha (7)")
		(folder / "cover.jpg").unlink()
		diag = Diagnosis(category="MISSING_COVER", reason="no cover", confidence=Confidence.LOW, verdict=Verdict.AUTO_FIXABLE)
		enriched = EnrichedMeta(title=None, authors=[], cover_url="http://x/cover.jpg", source="databazeknih")
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		_submit_all_and_finish(w, [(meta, diag, None, enriched)])
		parsed = parse_review(out)
		assert parsed[0].verified is True


class TestIdentityVerified:
	"""Auto `verified` via the identity gate: an accepted entry whose FINAL
	identity was confirmed against the book's content AND an online source
	(enriched.identity_confirmed from an _ONLINE_SOURCES source) is closed
	even when benign fields stay missing — no source has them, so re-running
	analyze would only re-fire the same accept forever."""

	def _book_folder(self, tmp_path, *, title="Kniha", isbn=None, with_year=True, with_cover=True):
		import json as _json

		from book_meta_fix.readers import read_book_folder

		folder = tmp_path / "lib" / "Jan Novak" / "Kniha (7)"
		folder.mkdir(parents=True)
		manifest = {"title": title, "authors": ["Jan Novak"]}
		if isbn:
			manifest["isbn"] = isbn
		if with_year:
			manifest["publishedYear"] = "2001"
		(folder / "metadata.json").write_text(_json.dumps(manifest), encoding="utf-8")
		(folder / "book.epub").write_text("x", encoding="utf-8")
		if with_cover:
			(folder / "cover.jpg").write_bytes(b"cover")
		return read_book_folder(folder)

	def test_online_identity_confirmed_verifies_despite_missing_isbn(self, tmp_path):
		"""databazeknih confirmed the identity (content + online) but has no
		ISBN for the edition: MISSING_ISBN survives the projection — a benign
		leftover — and the entry is accepted AND closed in one apply."""
		meta = self._book_folder(tmp_path, isbn=None)
		diag = Diagnosis(category="MISSING_ISBN", reason="no isbn", confidence=Confidence.LOW, verdict=Verdict.AUTO_FIXABLE)
		enriched = EnrichedMeta(identity_confirmed=True, source="databazeknih", publisher="Argo")
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		summary = _submit_all_and_finish(w, [(meta, diag, None, enriched)])
		parsed = parse_review(out)
		assert parsed[0].action == "accept"
		assert parsed[0].verified is True
		assert summary["verified_prefilled"] == 1

	def test_openlibrary_identity_change_verifies_enriched_final(self, tmp_path):
		"""An identity_confirmed online hit that CHANGES the title: the FINAL
		(enriched) identity is the confirmed one, so the book may be closed —
		the check is on the post-proposal metadata, not the current one."""
		meta = self._book_folder(tmp_path, isbn=None)
		diag = Diagnosis(category="MISSING_ISBN", reason="no isbn", confidence=Confidence.LOW, verdict=Verdict.AUTO_FIXABLE)
		enriched = EnrichedMeta(title="Správný titul", authors=["Jan Novak"], identity_confirmed=True, source="openlibrary")
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		_submit_all_and_finish(w, [(meta, diag, None, enriched)])
		parsed = parse_review(out)
		assert parsed[0].action == "accept"
		assert parsed[0].proposed["title"] == "Správný titul"
		assert parsed[0].verified is True

	def test_llm_is_not_an_online_source(self, tmp_path):
		"""llm:high confirms against content only — the LLM reasons from
		memory, it is not a bibliographic database. No auto-verified."""
		meta = self._book_folder(tmp_path, isbn=None)
		diag = Diagnosis(category="MISSING_ISBN", reason="no isbn", confidence=Confidence.LOW, verdict=Verdict.AUTO_FIXABLE)
		enriched = EnrichedMeta(identity_confirmed=True, source="llm:high", publisher="Argo")
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		summary = _submit_all_and_finish(w, [(meta, diag, None, enriched)])
		parsed = parse_review(out)
		assert parsed[0].action == "accept"
		assert parsed[0].verified is False
		assert summary["verified_prefilled"] == 0

	def test_content_stamp_is_not_an_online_source(self, tmp_path):
		"""The accept-missing stamp (source='content') has no online evidence:
		those books keep cycling as accept-as-is until closed manually — the
		documented trade-off of requiring an online confirmation."""
		meta = self._book_folder(tmp_path, isbn=None)
		diag = Diagnosis(category="MISSING_ISBN", reason="no isbn", confidence=Confidence.LOW, verdict=Verdict.AUTO_FIXABLE)
		enriched = EnrichedMeta(identity_confirmed=True, source="content")
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		summary = _submit_all_and_finish(w, [(meta, diag, None, enriched)])
		parsed = parse_review(out)
		assert parsed[0].action == "accept"
		assert not parsed[0].proposed
		assert parsed[0].verified is False
		assert summary["verified_prefilled"] == 0

	def test_needs_review_leftover_blocks_verified(self, tmp_path):
		"""A C2 (filename-as-title) the proposal does not fix survives the
		projection — a known defect must stay visible in review, not be closed
		behind the skip."""
		meta = self._book_folder(tmp_path, isbn=None, title="soubor_epub.epub")
		diag = Diagnosis(category="C2", reason="filename title", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)
		enriched = EnrichedMeta(identity_confirmed=True, source="databazeknih", publisher="Argo")
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		summary = _submit_all_and_finish(w, [(meta, diag, None, enriched)])
		parsed = parse_review(out)
		assert parsed[0].action == "accept"
		assert parsed[0].verified is False
		assert summary["verified_prefilled"] == 0

	def test_swap_overriding_online_title_blocks_verified(self, tmp_path):
		"""A C1-swap merged from diag.proposed overrides the online-confirmed
		title; the FINAL identity then disagrees with the online record, so the
		book is not auto-closed."""
		meta = self._book_folder(tmp_path, isbn=None)
		diag = Diagnosis(
			category="C1", reason="swap", confidence=Confidence.HIGH, verdict=Verdict.AUTO_FIXABLE,
			proposed={"title": "Novak", "author": "Kniha"},
		)
		enriched = EnrichedMeta(title="Kniha", authors=["Jan Novak"], identity_confirmed=True, source="databazeknih")
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		summary = _submit_all_and_finish(w, [(meta, diag, None, enriched)])
		parsed = parse_review(out)
		assert parsed[0].action == "accept"
		assert parsed[0].proposed["title"] == "Novak"
		assert parsed[0].verified is False
		assert summary["verified_prefilled"] == 0

	def test_missing_cover_leftover_allows_verified(self, tmp_path):
		"""No cover.jpg at all + identity confirmed (content + online): the
		missing cover is a benign leftover — the book may be closed even when
		apply's cover recovery will find nothing to extract."""
		meta = self._book_folder(tmp_path, isbn=None, with_cover=False)
		diag = Diagnosis(category="MISSING_COVER", reason="no cover.jpg sidecar", confidence=Confidence.LOW, verdict=Verdict.AUTO_FIXABLE)
		enriched = EnrichedMeta(identity_confirmed=True, source="databazeknih", publisher="Argo")
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		summary = _submit_all_and_finish(w, [(meta, diag, None, enriched)])
		parsed = parse_review(out)
		assert parsed[0].action == "accept"
		assert parsed[0].verified is True
		assert summary["verified_prefilled"] == 1

	def test_generated_cover_suspicion_blocks_verified(self, tmp_path):
		"""A cover that EXISTS but is a suspected Calibre placeholder (C11,
		NEEDS_REVIEW) with no replacement cover_url: a known defect — the
		book stays visible in review instead of being closed behind the
		skip. The mirror of test_missing_cover_leftover_allows_verified."""
		from pathlib import Path

		from PIL import Image

		meta = self._book_folder(tmp_path, isbn=None)
		# Solid 1200x1600 fill = the classic generated-cover signature
		# (same construction as test_covers.test_solid_1200x1600_...).
		Image.new("RGB", (1200, 1600), color=(50, 50, 50)).save(Path(meta.path) / "cover.jpg")
		diag = Diagnosis(category="C11", reason="generated cover (solid)", confidence=Confidence.HIGH, verdict=Verdict.NEEDS_REVIEW)
		enriched = EnrichedMeta(identity_confirmed=True, source="databazeknih", publisher="Argo")
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		summary = _submit_all_and_finish(w, [(meta, diag, None, enriched)])
		parsed = parse_review(out)
		assert parsed[0].action == "accept"
		assert parsed[0].verified is False
		assert summary["verified_prefilled"] == 0


class TestDecidedPriorCarry:
	"""A DECIDED prior entry is carried verbatim on re-run — and two behaviors
	make the analyze→apply loop converge instead of re-burning LLM tokens:

	1. decided_ids(): the pipeline skips the LLM for those books (the fresh
	   proposal would be discarded unread — the writer carries the prior).
	2. An accepted prior whose proposal projects detector-clean gets
	   verified: true stamped AT CARRY TIME, so one apply fixes AND closes
	   the book — the carried twin of the fresh-entry pre-fill.
	"""

	def _book_folder(self, tmp_path, *, with_year=False, uuid="book-u7"):
		import json as _json

		from book_meta_fix.readers import read_book_folder

		folder = tmp_path / "lib" / "Jan Novak" / "Kniha (7)"
		folder.mkdir(parents=True)
		manifest = {"title": "Kniha", "authors": ["Jan Novak"], "isbn": "9788020403117"}
		if uuid:
			manifest["uuid"] = uuid
		if with_year:
			manifest["publishedYear"] = "2001"
		(folder / "metadata.json").write_text(_json.dumps(manifest), encoding="utf-8")
		(folder / "book.epub").write_text("x", encoding="utf-8")
		(folder / "cover.jpg").write_bytes(b"cover")
		return read_book_folder(folder)

	def _seed_prior(self, tmp_path, body: str) -> None:
		(tmp_path / "review.yaml").write_text(body, encoding="utf-8")

	def test_decided_ids_lists_only_action_entries(self, tmp_path):
		self._seed_prior(tmp_path, (
			"---\nid: 1\nuuid: u1\ncurrent: {title: A}\naction: accept\n"
			"---\nid: 2\nuuid: u2\ncurrent: {title: B}\naction: null\n"
			"---\nid: 3\nuuid: u3\ncurrent: {title: C}\naction: keep\n"
		))
		w = ReviewWriter(tmp_path / "review.yaml")
		assert w.decided_ids() == {"u1", "u3"}
		w.finish()

	def test_carried_accept_completing_proposal_gets_verified(self, tmp_path):
		"""Prior: decided accept with a year proposal for a MISSING_YEAR book.
		The projection is detector-clean once applied → carried entry is born
		verified, so apply fixes AND closes it in one pass."""
		meta = self._book_folder(tmp_path)  # no year on disk
		self._seed_prior(tmp_path, (
			"---\nid: 7\nuuid: book-u7\ncurrent: {title: Kniha}\naction: accept\n"
			"proposed:\n  year: 2001\n"
		))
		w = ReviewWriter(tmp_path / "review.yaml")
		diag = Diagnosis(category="MISSING_YEAR", reason="no year", confidence=Confidence.LOW, verdict=Verdict.AUTO_FIXABLE)
		summary = _submit_all_and_finish(w, [(meta, diag, None, None)])
		parsed = parse_review(tmp_path / "review.yaml")
		assert parsed[0].action == "accept"
		assert parsed[0].verified is True
		assert summary["verified_prefilled"] == 1

	def test_carried_accept_llm_proposal_stays_unverified(self, tmp_path):
		"""Same as above but the prior proposal carries an llm: source: the
		fresh-entry pre-fill refuses to auto-close unconfirmed LLM identity
		changes, and so must the carried one (many accepts are analyzer
		pre-fills, not human decisions)."""
		meta = self._book_folder(tmp_path)
		self._seed_prior(tmp_path, (
			"---\nid: 7\nuuid: book-u7\ncurrent: {title: Kniha}\naction: accept\n"
			"proposed:\n  year: 2001\n  source: llm:flash\n"
		))
		w = ReviewWriter(tmp_path / "review.yaml")
		diag = Diagnosis(category="MISSING_YEAR", reason="no year", confidence=Confidence.LOW, verdict=Verdict.AUTO_FIXABLE)
		summary = _submit_all_and_finish(w, [(meta, diag, None, None)])
		parsed = parse_review(tmp_path / "review.yaml")
		assert parsed[0].action == "accept"
		assert parsed[0].verified is False
		assert summary["verified_prefilled"] == 0

	def test_carried_keep_stays_unverified(self, tmp_path):
		"""keep = apply-but-stay-in-review; auto-freezing it behind the
		verified skip would defeat the action's whole point."""
		meta = self._book_folder(tmp_path)
		self._seed_prior(tmp_path, (
			"---\nid: 7\nuuid: book-u7\ncurrent: {title: Kniha}\naction: keep\n"
			"proposed:\n  year: 2001\n"
		))
		w = ReviewWriter(tmp_path / "review.yaml")
		diag = Diagnosis(category="MISSING_YEAR", reason="no year", confidence=Confidence.LOW, verdict=Verdict.AUTO_FIXABLE)
		_submit_all_and_finish(w, [(meta, diag, None, None)])
		parsed = parse_review(tmp_path / "review.yaml")
		assert parsed[0].action == "keep"
		assert parsed[0].verified is False

	def test_carried_accept_incomplete_proposal_stays_open(self, tmp_path):
		"""A decided accept whose proposal does NOT clean the projection (the
		book stays flagged after apply) must not be verified — it would hide
		a standing problem behind the analyze skip."""
		meta = self._book_folder(tmp_path)  # MISSING_YEAR stays missing
		self._seed_prior(tmp_path, (
			"---\nid: 7\nuuid: book-u7\ncurrent: {title: Kniha}\naction: accept\n"
		))
		w = ReviewWriter(tmp_path / "review.yaml")
		diag = Diagnosis(category="MISSING_YEAR", reason="no year", confidence=Confidence.LOW, verdict=Verdict.AUTO_FIXABLE)
		summary = _submit_all_and_finish(w, [(meta, diag, None, None)])
		parsed = parse_review(tmp_path / "review.yaml")
		assert parsed[0].verified is False
		assert summary["verified_prefilled"] == 0


class TestC14PrefillAndTolerance:
	"""C14 (series order glued into the name): pre-fills accept via
	diag.proposed (the C6 mechanism), and is tolerated as a C13 extra
	(identity-preserving split, same class of fix as the move itself)."""

	def _c14_result(self, calibre_id: int):
		meta = _meta(calibre_id)
		meta.series = ["Mark Stone #73"]
		diag = Diagnosis(
			category="C14", reason="series carries its order in the name",
			confidence=Confidence.HIGH, verdict=Verdict.AUTO_FIXABLE,
			proposed={"action": "accept", "series": "Mark Stone", "series_index": "73"},
		)
		return (meta, diag, None, None)

	def test_c14_prefills_accept_with_split(self, tmp_path):
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		_submit_all_and_finish(w, [self._c14_result(1)])
		parsed = parse_review(out)
		assert parsed[0].action == "accept"
		assert parsed[0].proposed["series"] == "Mark Stone"
		assert parsed[0].proposed["series_index"] == "73"

	def test_c13_primary_with_c14_extra_prefills_accept(self, tmp_path):
		meta = _meta(1)
		meta.series = ["Mark Stone #73"]
		c13 = Diagnosis(
			category="C13", reason="umístění", confidence=Confidence.HIGH,
			verdict=Verdict.AUTO_FIXABLE, proposed={"location": "A/T (1)"},
			additional=[Diagnosis(
				category="C14", reason="series carries its order in the name",
				confidence=Confidence.HIGH, verdict=Verdict.AUTO_FIXABLE,
				proposed={"action": "accept", "series": "Mark Stone", "series_index": "73"},
			)],
		)
		out = tmp_path / "review.yaml"
		w = ReviewWriter(out)
		_submit_all_and_finish(w, [(meta, c13, None, None)])
		parsed = parse_review(out)
		# The move AND the mechanical series split are both bulk-approvable;
		# the split proposal rides along (it does not change title/author).
		assert parsed[0].action == "accept"
		assert parsed[0].proposed["series"] == "Mark Stone"


class TestFsyncBatching:
	"""fsync is paced (at most every _FSYNC_MIN_ENTRIES entries / _FSYNC_MIN_SEC
	seconds) with one unconditional sync in finish() — an fsync on NFS is a
	synchronous COMMIT RPC, so one per entry meant thousands of round trips
	per run. Everything must still be on disk after finish()."""

	def test_many_entries_fewer_fsyncs_than_entries(self, tmp_path, monkeypatch):
		import book_meta_fix.review_writer as rwmod

		calls = {"n": 0}
		real_fsync = rwmod.os.fsync

		def counting_fsync(fd):
			calls["n"] += 1
			return real_fsync(fd)

		monkeypatch.setattr(rwmod.os, "fsync", counting_fsync)
		out = tmp_path / "review.yaml"
		writer = ReviewWriter(out, library_root=tmp_path)
		n = 60
		for i in range(n):
			writer.submit(_result(i))
		writer.finish()
		assert calls["n"] < n  # paced mid-run + exactly one final sync
		# Durability contract: every entry is in the file after finish().
		entries = [e for e in parse_review(out) if e is not None]
		assert len(entries) == n

	def test_small_run_still_flushes_everything(self, tmp_path):
		out = tmp_path / "review.yaml"
		writer = ReviewWriter(out, library_root=tmp_path)
		for i in range(3):
			writer.submit(_result(i))
		writer.finish()
		entries = [e for e in parse_review(out) if e is not None]
		assert len(entries) == 3
