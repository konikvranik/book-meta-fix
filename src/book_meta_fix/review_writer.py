"""Streaming review.yaml writer (queue + writer thread).

As books are processed by the pipeline, their results are appended to
review.yaml one entry at a time — Unix-pipe style — instead of buffering the
whole run and dumping it at the end. This gives:

  * live visibility (tail -f the file as the run progresses),
  * crash/Ctrl-C safety (no result older than the last flush is lost),
  * preservation of prior user decisions (action/edited/notes) via an in-memory
    merge against the .bak taken at start.

Lifecycle
---------
1. ``__init__``: MOVE ``output`` -> ``output.bak`` (the original ceases to exist
   so there's never a window where both files disagree). Load ``.bak`` into an
   in-memory ``prior`` map (id -> entry dict). Open a fresh ``output`` with the
   header. Start the writer thread consuming a ``queue.Queue``.
2. ``submit(result)``: called from a worker thread after each book finishes.
   Cheap (just enqueues); the worker is never blocked on I/O.
3. ``finish()``: signal the writer to drain, then append any prior entries the
   new run did NOT process (so a ``--limit`` run doesn't drop user decisions
   for untouched books). On success, delete ``.bak``. Returns a summary.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
from pathlib import Path
from typing import Any

from .detectors import all_diagnoses
from .models import Verdict
from .review import _COVER_CATEGORIES, _build_current, _build_proposed, _header, _relative_path, _render_entry

log = logging.getLogger(__name__)

# Sentinel pushed onto the queue to tell the writer thread to stop after drain.
_SENTINEL: Any = object()


class ReviewWriter:
	"""Streaming writer for review.yaml: append one entry per processed book.

	Thread-safe: many worker threads call ``submit()``; a single dedicated
	writer thread consumes the queue and appends to the file, so entries never
	interleave.
	"""

	def __init__(
		self,
		output: Path,
		library_root: Path | None = None,
	) -> None:
		"""Set up the streaming writer.

		*output*: review.yaml path. Moved to ``output + ".bak"`` on construction
		(the original no longer exists at this path after construction).
		*library_root*: used to render relative paths in entries.
		"""
		self.output = output
		self.library_root = library_root
		self._confidence_rank = {"high": 3, "medium": 2, "low": 1}

		# 1. Move output -> output.bak (overwrite a stale .bak if present).
		bak = output.with_suffix(output.suffix + ".bak")
		if output.is_file():
			if bak.exists():
				log.warning("overwriting stale %s (previous run did not finish cleanly)", bak)
				bak.unlink()
			os.replace(output, bak)
			self._bak = bak
		else:
			# First run (no prior review.yaml). No backup, no prior to merge.
			self._bak = None

		# 2. Load prior into an in-memory map keyed by id.
		self._prior: dict[Any, dict] = {}
		if self._bak is not None:
			self._prior = self._load_prior(self._bak)
			log.info("review writer: loaded %d prior entries from %s", len(self._prior), self._bak)

		# 3. Start a fresh output with the header.
		self._processed: set[Any] = set()
		self._written = 0
		# Books skipped because the user already decided on them in a prior
		# review.yaml (their `action` is preserved as-is on rerun).
		self._skipped_user_decided = 0
		output.write_text(_header(0), encoding="utf-8")

		# 4. Writer thread + queue. The file is opened in append-binary mode and
		#    kept open for the writer thread only; workers never touch it.
		self._queue: "queue.Queue[Any]" = queue.Queue()
		self._fh = open(output, "a", encoding="utf-8")
		self._write_lock = threading.Lock()  # guards _fh between writer + finish()
		self._writer_thread = threading.Thread(target=self._loop, name="review-writer", daemon=True)
		self._writer_thread.start()

	# ------------------------------------------------------------------
	# Worker-facing API
	# ------------------------------------------------------------------

	def submit(self, result: tuple) -> None:
		"""Enqueue a processed-book result tuple for streaming append.

		*result* is the ``(meta, diag, verification, enriched)`` tuple produced
		by ``_process_book``. Cheap; safe to call from any worker thread.
		"""
		self._queue.put(result)

	# ------------------------------------------------------------------
	# Writer thread
	# ------------------------------------------------------------------

	def _loop(self) -> None:
		"""Consume the queue, appending each entry. Stops on _SENTINEL."""
		while True:
			item = self._queue.get()
			try:
				if item is _SENTINEL:
					return
				self._handle(item)
			except Exception:  # noqa: BLE001
				# A single malformed entry must not kill the writer thread —
				# that would stall finish() and lose all subsequent appends.
				log.exception("review writer: error handling entry, skipping")
			finally:
				self._queue.task_done()

	def _handle(self, result: tuple) -> None:
		"""Append one book result to review.yaml (prior user decisions preserved)."""
		meta, diag, verification = result[0], result[1], result[2]
		enriched = result[3] if len(result) > 3 else None
		extracted = verification.extracted if verification else None
		# NEEDS_REVIEW/UNFIXABLE/AUTO_FIXABLE books (or content-mismatch) go to
		# review. AUTO_FIXABLE covers C6 (Word lock-file -> proposed delete).
		include = diag.verdict.value in ("NEEDS_REVIEW", "UNFIXABLE", "AUTO_FIXABLE")
		if verification and verification.result == "MISMATCH":
			include = True
		if not include:
			return

		bid = meta.calibre_id
		prior_entry = self._prior.get(bid)

		# Respect a prior user decision: if they already set action, keep the
		# prior entry verbatim (refresh current) and append it unchanged.
		if prior_entry is not None and prior_entry.get("action") is not None:
			self._skipped_user_decided += 1
			entry = dict(prior_entry)
			entry["current"] = _build_current(meta)
			self._append_entry(entry)
			self._processed.add(bid)
			return

		# Build the review entry, carrying prior user edits if any.
		entry = self._build_entry(meta, diag, extracted, enriched, prior_entry)
		self._append_entry(entry)
		self._processed.add(bid)

	def _confidence(self, enriched: Any) -> str:
		"""Confidence label from an EnrichedMeta.source ('llm:high'|'embedded'|...).

		Returns one of the three rank labels used by _confidence_rank:
		'high' | 'medium' | 'low'. The label drives the action: accept pre-fill
		in review.yaml (see _build_entry): high pre-fills unconditionally,
		medium only when the proposal preserves the book's identity.

		High-confidence sources (pre-fill action: accept):
		  - 'embedded'    — OPF metadata, deterministic
		  - 'databazeknih' — authoritative CZ/SK source, fuzzy-match-gated
		  - 'llm:high'    — paid reasoning model, verify_proposal-validated

		Medium-confidence (pre-fill accept only when title/author are unchanged):
		  - 'openlibrary' / 'google_books' — international, weaker CZ coverage
		  - 'llm:flash' / 'llm:loop' / 'llm:medium' — Flash model that passed
		    verify_proposal (title+author fuzzy-match the book's page text)
		  - 'content'     — text_meta heuristic; identity must be preserved

		Low (never pre-filled):
		  - 'llm:low'     — proposal that failed verify_proposal
		  - unknown sources
		"""
		src = getattr(enriched, "source", "") or ""
		if src.startswith("llm:"):
			tier = src.split(":", 1)[1] if ":" in src else "low"
			# Flash and the loop variants are medium (passed verify_proposal);
			# 'high' and 'low' pass through unchanged.
			if tier in ("flash", "loop", "medium"):
				return "medium"
			return tier  # 'high' or 'low'
		if src in ("embedded", "databazeknih"):
			return "high"
		if src in ("openlibrary", "google_books", "content"):
			return "medium"
		return "low"

	def _build_entry(self, meta: Any, diag: Any, extracted: Any, enriched: Any, prior_entry: dict | None) -> dict:
		"""Build a review entry dict, carrying over prior user edits."""
		proposed = _build_proposed(meta, extracted, enriched, diag)
		# AUTO_FIXABLE detectors may carry an explicit action/proposal on
		# diag.proposed (e.g. C6 Word lock-file -> {"action": "delete"}).
		# Pre-fill it so the user only has to confirm. Merge any extra keys
		# from diag.proposed into the entry's proposed block.
		diag_proposed = getattr(diag, "proposed", None) or {}
		action = diag_proposed.get("action") if diag.verdict == Verdict.AUTO_FIXABLE else None
		if diag_proposed and diag.verdict == Verdict.AUTO_FIXABLE:
			extra = {k: v for k, v in diag_proposed.items() if k != "action"}
			if extra:
				proposed = {**(proposed or {}), **extra}
		# High-confidence enriched proposal (offline extraction, databazeknih,
		# LLM high) -> pre-fill action: accept so the user can approve it in
		# bulk via `bmf apply`. Without a proposal there is nothing to accept.
		# Threshold is fixed at 'high' (rank 3).
		if action is None and enriched is not None and proposed:
			conf = self._confidence(enriched)
			if getattr(enriched, "identity_confirmed", False) or self._confidence_rank.get(conf, 0) >= self._confidence_rank["high"]:
				# identity_confirmed: the book's identity was verified against its
				# own content (ISBN agreement or title+author in the page text),
				# independent of the online match — so the proposal is safe to
				# auto-accept even when it changes title/author. We know the book.
				action = "accept"
			elif self._confidence_rank.get(conf, 0) >= self._confidence_rank["medium"]:
				# Medium-confidence (llm:flash/loop, openlibrary, google_books,
				# content): pre-fill accept ONLY when the proposal confirms the
				# book's identity (leaves title/author unchanged). When the match
				# AGREES with the existing title/author, the enrichment is about
				# the right book, so its additive fields (isbn/year/genres) are
				# safe to bulk-apply. When the proposal CHANGES title/author the
				# match is on an unconfirmed identity — and because the query was
				# built on those (possibly wrong) title/author values, we cannot
				# trust the additive fields either: they may belong to the wrong
				# book. So the whole proposal stays action=None for review.
				if self._proposal_preserves_identity(proposed, meta):
					action = "accept"
		# Cover replacement: when the diagnosis is C11 (generated cover) or
		# MISSING_COVER and we have a cover_url from an enricher, pre-fill
		# accept so the user can bulk-approve. The enricher already fuzzy-
		# matched the book (databazeknih score >= 70), so the cover belongs to
		# the right book. No title/author change risk — just a cover download.
		if action is None and diag.category in _COVER_CATEGORIES and proposed and proposed.get("cover_url"):
			action = "accept"
		entry: dict[str, Any] = {
			"id": meta.calibre_id,
			"path": _relative_path(meta, self.library_root),
			"diagnosis": {
				"category": diag.category,
				"reason": diag.reason,
				"confidence": diag.confidence.value,
			},
			"current": _build_current(meta),
			"proposed": proposed,
			"action": action,
		}
		# Expose the full diagnosis list when the book has additional problems,
		# so apply and the reviewer see every issue (primary is already in
		# `diagnosis` above). Single-issue books keep the legacy shape.
		if diag.additional:
			entry["diagnoses"] = [
				{"category": d.category, "reason": d.reason, "confidence": d.confidence.value}
				for d in all_diagnoses(diag)
			]
		if prior_entry is not None:
			if prior_entry.get("edited"):
				entry["edited"] = prior_entry["edited"]
			if prior_entry.get("notes"):
				entry["notes"] = prior_entry["notes"]
		return entry

	def _append_entry(self, entry: dict) -> None:
		"""Append one entry's multi-doc YAML chunk to the file (thread-safe)."""
		chunk = _render_entry(entry)
		# Ensure trailing newline so the next --- starts on its own line.
		if not chunk.endswith("\n"):
			chunk += "\n"
		with self._write_lock:
			self._fh.write(chunk)
			self._fh.flush()
			os.fsync(self._fh.fileno())
		self._written += 1

	@staticmethod
	def _proposal_preserves_identity(proposed: dict, meta: Any) -> bool:
		"""Does *proposed* leave the book's title and author unchanged?

		A medium-confidence proposal (e.g. llm:flash) that agrees with the
		existing title/author is safe to pre-fill as accept: the only changes
		are *added* metadata (isbn/year/genres/series), which a human can
		bulk-verify. A proposal that changes title or author must stay
		action=None for individual review.
		"""
		pt = proposed.get("title")
		pa = proposed.get("author")
		cur_title = getattr(meta, "title", None)
		cur_authors = getattr(meta, "authors", None) or []
		cur_author = cur_authors[0] if cur_authors else None
		# Absent in the proposal means "no change" for that field.
		title_ok = pt is None or pt == cur_title
		author_ok = pa is None or pa == cur_author
		return title_ok and author_ok

	# ------------------------------------------------------------------
	# Lifecycle: finish
	# ------------------------------------------------------------------

	def finish(self, *, keep_backup: bool = False) -> dict:
		"""Drain the queue, carry over unprocessed prior entries, finalize.

		After finish() the writer thread has stopped and the file is closed.
		On success (no exception), the ``.bak`` is deleted unless *keep_backup*.

		Returns a summary dict: {written, skipped_user_decided,
		remaining_count, backup_path}.
		"""
		# Tell the writer to drain and exit.
		self._queue.put(_SENTINEL)
		self._writer_thread.join()

		# Carry over prior entries the new run did NOT process (--limit, OK
		# books, etc.) so user decisions are never silently dropped.
		carried = 0
		for bid, entry in self._prior.items():
			if bid in self._processed:
				continue
			self._append_entry(entry)
			carried += 1
		if carried:
			log.info("review writer: carried over %d unprocessed prior entries", carried)

		# Rewrite the header line with the final count (best-effort; the body
		# is already on disk and safe).
		try:
			self._rewrite_header_count(self._written)
		except Exception:  # noqa: BLE001
			log.debug("could not update review header count", exc_info=True)

		# Close the file handle.
		with self._write_lock:
			self._fh.close()

		backup_path = str(self._bak) if self._bak is not None else None
		if not keep_backup and self._bak is not None:
			try:
				self._bak.unlink()
				backup_path = None
			except OSError as e:
				log.warning("could not delete %s: %s", self._bak, e)

		return {
			"written": self._written,
			"skipped_user_decided": self._skipped_user_decided,
			"remaining_count": self._written,
			"backup_path": backup_path,
		}

	def _rewrite_header_count(self, count: int) -> None:
		"""Update the '# N books need review.' line in the header."""
		text = self.output.read_text(encoding="utf-8")
		lines = text.split("\n")
		for i, ln in enumerate(lines):
			if ln.startswith("# ") and "books need review" in ln:
				lines[i] = f"# {count} books need review."
				break
		self.output.write_text("\n".join(lines), encoding="utf-8")

	# ------------------------------------------------------------------
	# Prior loading
	# ------------------------------------------------------------------

	@staticmethod
	def _load_prior(bak: Path) -> dict[Any, dict]:
		"""Load a backup review.yaml into {id: entry_dict}, best-effort."""
		import yaml

		try:
			docs = list(yaml.safe_load_all(bak.read_text(encoding="utf-8")))
		except (yaml.YAMLError, OSError) as e:
			log.warning("could not parse %s (%s); starting fresh", bak, e)
			return {}
		prior: dict[Any, dict] = {}
		for doc in docs:
			if doc is None:
				continue
			if isinstance(doc, list):
				items = doc
			elif isinstance(doc, dict):
				items = [doc]
			else:
				continue
			for entry in items:
				if isinstance(entry, dict) and entry.get("id") is not None:
					prior[entry["id"]] = entry
		return prior
