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

Auto-apply
---------
When an ``apply_threshold`` is configured, the writer applies high-confidence
proposals directly to metadata files (via writers.write_book_meta) and does
NOT append those books to review.yaml — exactly the split that auto_apply_results
implements, but interleaved with streaming instead of as a post-pass.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
from pathlib import Path
from typing import Any

from .models import Verdict
from .review import _build_current, _build_proposed, _header, _relative_path, _render_entry

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
		*,
		apply_threshold: str | None = None,
	) -> None:
		"""Set up the streaming writer.

		*output*: review.yaml path. Moved to ``output + ".bak"`` on construction
		(the original no longer exists at this path after construction).
		*library_root*: used to render relative paths in entries.
		*apply_threshold*: if given ('high'|'medium'|'low'), proposals at or
		above this confidence are applied to metadata files directly (and
		omitted from review.yaml). If None, every NEEDS_REVIEW book is appended.
		"""
		self.output = output
		self.library_root = library_root
		self.apply_threshold = apply_threshold
		self._confidence_rank = {"high": 3, "medium": 2, "low": 1}
		self._min_rank = self._confidence_rank.get(apply_threshold or "", 0)

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
		self._apply_summary = {
			"applied": 0,
			"skipped_low_conf": 0,
			"skipped_no_proposal": 0,
			"skipped_user_decided": 0,
		}
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
		"""Apply/auto-apply/append one book result."""
		meta, diag, verification = result[0], result[1], result[2]
		enriched = result[3] if len(result) > 3 else None
		extracted = verification.extracted if verification else None
		# Only NEEDS_REVIEW/UNFIXABLE books (or content-mismatch) go to review.
		include = diag.verdict.value in ("NEEDS_REVIEW", "UNFIXABLE")
		if verification and verification.result == "MISMATCH":
			include = True
		if not include:
			return

		bid = meta.calibre_id
		prior_entry = self._prior.get(bid)

		# Respect a prior user decision: if they already set action, keep the
		# prior entry verbatim (refresh current) and don't re-apply/append-new.
		if prior_entry is not None and prior_entry.get("action") is not None:
			self._apply_summary["skipped_user_decided"] += 1
			entry = dict(prior_entry)
			entry["current"] = _build_current(meta)
			self._append_entry(entry)
			self._processed.add(bid)
			return

		# Auto-apply path: high-confidence proposal -> write metadata, skip review.
		if self._min_rank > 0 and enriched is not None:
			conf = self._confidence(enriched)
			if self._confidence_rank.get(conf, 0) >= self._min_rank:
				if self._apply_to_metadata(meta, enriched):
					self._apply_summary["applied"] += 1
					self._processed.add(bid)
					return
				# write failed -> fall through to review so the human can fix
			elif enriched is not None:
				self._apply_summary["skipped_low_conf"] += 1
		elif self._min_rank > 0 and enriched is None:
			self._apply_summary["skipped_no_proposal"] += 1

		# Build the review entry, carrying prior user edits if any.
		entry = self._build_entry(meta, diag, extracted, enriched, prior_entry)
		self._append_entry(entry)
		self._processed.add(bid)

	def _confidence(self, enriched: Any) -> str:
		"""Confidence label from an EnrichedMeta.source ('llm:high'|'embedded'|...)."""
		src = getattr(enriched, "source", "") or ""
		if src.startswith("llm:"):
			return src.split(":", 1)[1] if ":" in src else "low"
		if src.startswith("embedded"):
			return "high"  # deterministic fixes are trustworthy
		return "low"

	def _apply_to_metadata(self, meta: Any, enriched: Any) -> bool:
		"""Apply *enriched* to metadata files. Returns True on success."""
		try:
			from .pipeline import _apply_enriched_to_meta
			from .writers import write_book_meta

			updated = _apply_enriched_to_meta(meta, enriched)
			write_book_meta(updated, dry_run=False, backup=True)
			return True
		except Exception:  # noqa: BLE001
			log.exception("auto-apply write failed for %s; falling back to review", getattr(meta, "path", "?"))
			return False

	def _build_entry(self, meta: Any, diag: Any, extracted: Any, enriched: Any, prior_entry: dict | None) -> dict:
		"""Build a review entry dict, carrying over prior user edits."""
		entry: dict[str, Any] = {
			"id": meta.calibre_id,
			"path": _relative_path(meta, self.library_root),
			"diagnosis": {
				"category": diag.category,
				"reason": diag.reason,
				"confidence": diag.confidence.value,
			},
			"current": _build_current(meta),
			"proposed": _build_proposed(meta, extracted, enriched),
			"action": None,
		}
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

	# ------------------------------------------------------------------
	# Lifecycle: finish
	# ------------------------------------------------------------------

	def finish(self, *, keep_backup: bool = False) -> dict:
		"""Drain the queue, carry over unprocessed prior entries, finalize.

		After finish() the writer thread has stopped and the file is closed.
		On success (no exception), the ``.bak`` is deleted unless *keep_backup*.

		Returns a summary dict: {written, applied, skipped_*, remaining_count,
		backup_path}.
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
			"applied": self._apply_summary["applied"],
			"skipped_low_conf": self._apply_summary["skipped_low_conf"],
			"skipped_no_proposal": self._apply_summary["skipped_no_proposal"],
			"skipped_user_decided": self._apply_summary["skipped_user_decided"],
			"remaining_count": self._written - self._apply_summary["applied"],
			"backup_path": backup_path,
			"threshold": self.apply_threshold,
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
