"""Library-wide duplicate-folder detection (C19) + merge proposals into
review.yaml.

The same work often lives in TWO folders (a duplicate import under a second
calibre_id). Under the default ``{author}/{title} ({id})`` pattern the ids
keep the target paths apart, so apply's collision-merge can never reach them
— the duplicates persist forever. This module is the explicit counterpart: a
library-wide pass that clusters same-work folders and proposes the merge as a
review action the human decides on, exactly like C15–C18 propose
normalizations and C17 proposes file deletions.

Detection is deliberately EXACT-ONLY: two books are candidates when their
folded first author + folded title are identical, or when both carry the same
valid (canonicalized) ISBN. There is no fuzzy tier on purpose — a per-book
0.85 band is fine for a path collision that a human sees in placement, but a
library-wide sweep multiplies false pairs (~5,000 books), and a wrong merge
proposal is noise the user pays for. Misspellings stay invisible to C19; fix
them via C15/normalize first and C19 picks the pair up on the next run.

The final gate is :func:`book_meta_fix.mover.same_book` (the year
tie-breaker): folders whose years BOTH exist and differ are different
editions and never merge — unless the ISBNs already match (a matching ISBN
identifies the same edition even when one record carries a wrong year).
The survivor is chosen by
:func:`book_meta_fix.mover._pick_base` — a valid ISBN wins, then the lowest
calibre_id — so the merged folder's path/uuid are deterministic across runs.
"""
from __future__ import annotations

import os
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .isbn import canonicalize
from .models import BookMeta
from .mover import _pick_base, same_book
from .normalize import _DIA_MAP


def _fold_text(s: str) -> str:
	"""Fold key for author/title: NFC + casefold + diacritics out +
	punctuation to spaces, whitespace collapsed (the fold_series recipe).
	Word order is preserved — a reordered title is a DIFFERENT key on
	purpose, C19 has no fuzzy tier."""
	folded = unicodedata.normalize("NFC", s).casefold().translate(_DIA_MAP)
	return " ".join(re.sub(r"[^\w\s]", " ", folded).split())


@dataclass
class DuplicateFinding:
	"""One duplicate folder proposed to merge into its survivor.

	``path``/``uuid`` identify the LOSER (the folder absorbed — its review
	entry carries ``action: merge``); ``merge_into`` is the survivor's path,
	library-relative when possible (the review-file convention).
	"""

	path: Path
	uuid: str | None
	merge_into: str
	survivor_uuid: str | None
	reason: str = ""
	# Both sides carry the same valid ISBN — the only tier whose fresh
	# entries get ``action: merge`` pre-filled (an exact author+title match
	# without ISBN proof stays pending; the human confirms).
	isbn_confirmed: bool = False


def _relative(folder: Path, library_root: Path | None) -> str:
	try:
		return str(folder.relative_to(library_root))
	except ValueError:
		return str(folder)


def scan_duplicates(books, library_root: Path | None = None) -> list[DuplicateFinding]:
	"""Cluster same-work folders over the whole library.

	Returns one finding per LOSER folder (a 3-book cluster yields 2 findings,
	all merging into the same survivor). Books without format files (dead
	EMPTY_BOOK records) are excluded — they belong to the needfix/empty flow,
	and a dead record must not become a survivor. A loser is proposed once
	even when it matches the survivor via both the title and the ISBN key.
	"""
	groups: dict[str, list[BookMeta]] = defaultdict(list)
	for meta in books:
		if not meta.formats:
			continue  # dead record — EMPTY_BOOK owns it
		title = _fold_text(meta.title or "")
		author = _fold_text(meta.authors[0]) if meta.authors else ""
		if author and title:
			groups[f"t\x00{author}\x00{title}"].append(meta)
		if meta.isbn:
			isbn = canonicalize(meta.isbn)
			if isbn:
				groups[f"i\x00{isbn}"].append(meta)

	findings: list[DuplicateFinding] = []
	seen_losers: set[str] = set()
	for group in groups.values():
		if len(group) < 2:
			continue
		base = _pick_base(group)
		# same_book gate against the SURVIVOR: the year tie-breaker splits
		# mixed groups (different editions) — only true same-work members
		# follow the base, a filtered-out member just stays unmerged.
		members = [m for m in group if Path(m.path) != Path(base.path) and same_book(base, m)]
		if not members:
			continue
		base_rel = _relative(Path(base.path), library_root)
		base_isbn = canonicalize(base.isbn) if base.isbn else None
		for meta in members:
			key = str(Path(meta.path))
			if key in seen_losers:
				continue
			seen_losers.add(key)
			isbn_confirmed = bool(base_isbn and meta.isbn and canonicalize(meta.isbn) == base_isbn)
			if isbn_confirmed:
				reason = f"duplicate of {base_rel}: identical ISBN {base_isbn}"
			else:
				reason = f"duplicate of {base_rel}: identical author + title"
			findings.append(
				DuplicateFinding(
					path=Path(meta.path),
					uuid=meta.uuid,
					merge_into=base_rel,
					survivor_uuid=base.uuid,
					reason=reason,
					isbn_confirmed=isbn_confirmed,
				)
			)
	return findings


def merge_duplicate_proposals(
	path: str | Path,
	findings: list[DuplicateFinding],
	books,
	library_root: Path | None = None,
) -> dict[str, int]:
	"""Merge C19 merge proposals into review.yaml (in place, atomic).

	Same contract as :func:`book_meta_fix.review.merge_normalizations` and
	:func:`book_meta_fix.filecheck.merge_file_deletions`: a PENDING entry
	gets ``proposed.merge_into`` overlaid and the C19 diagnosis appended; a
	DECIDED entry is never touched; a book without an entry gets a fresh one
	with ``action: merge`` pre-filled ONLY for the ISBN-confirmed tier (the
	exact-match-without-proof tier stays pending — the human confirms).
	Entries are never born ``verified`` — merging folders is not identity
	evidence. Returns ``{added, updated, skipped_decided}``.
	"""
	from .review import _build_current, _header, _load_raw_entries, _relative_path, _render_entry

	p = Path(path)
	entries = _load_raw_entries(p) if p.is_file() else []
	by_uuid = {e.get("uuid"): e for e in entries if e.get("uuid")}
	metas = {b.uuid: b for b in books if b.uuid}
	added = updated = skipped = 0
	for finding in findings:
		meta = metas.get(finding.uuid)
		if meta is None:
			continue
		diag = {
			"category": "C19",
			"reason": finding.reason,
			"confidence": "HIGH" if finding.isbn_confirmed else "MEDIUM",
		}
		existing = by_uuid.get(finding.uuid)
		if existing is not None:
			if existing.get("action") is not None:
				skipped += 1
				continue
			proposed = dict(existing.get("proposed") or {})
			proposed["merge_into"] = finding.merge_into
			existing["proposed"] = proposed
			diags = existing.get("diagnoses") or ([existing["diagnosis"]] if existing.get("diagnosis") else [])
			if not any(d.get("category") == "C19" for d in diags):
				diags.append(diag)
			if diags:
				existing["diagnoses"] = diags
			updated += 1
		else:
			entry = {
				"id": meta.calibre_id,
				"uuid": meta.uuid,
				"path": _relative_path(meta, library_root),
				"diagnosis": diag,
				"current": _build_current(meta),
				"proposed": {"merge_into": finding.merge_into, "source": "duplicates"},
				"action": "merge" if finding.isbn_confirmed else None,
			}
			entries.append(entry)
			by_uuid[meta.uuid] = entry
			added += 1
	if not (added or updated):
		return {"added": 0, "updated": 0, "skipped_decided": skipped}
	header = _header(len(entries))
	body = "\n".join(_render_entry(e) for e in entries)
	if body:
		body += "\n"
	tmp = p.with_suffix(p.suffix + ".tmp")
	tmp.write_text(header + body, encoding="utf-8")
	os.replace(tmp, p)
	return {"added": added, "updated": updated, "skipped_decided": skipped}
