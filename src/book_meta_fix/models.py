"""Core data models for book-meta-fix.

BookMeta: parsed metadata of a single book (from metadata.json / opf / path).
Diagnosis: result of a detector rule applied to a BookMeta.
Verdict: bucket the book falls into (OK / AUTO_FIXABLE / NEEDS_REVIEW / UNFIXABLE).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Verdict(str, Enum):
	"""Bucket a book falls into after detection + verification."""

	OK = "OK"  # passes all rules (may still be verified against content)
	VERIFIED = "VERIFIED"  # OK and confirmed by book content
	AUTO_FIXABLE = "AUTO_FIXABLE"  # high-confidence fix, can apply automatically
	NEEDS_REVIEW = "NEEDS_REVIEW"  # uncertain, goes to YAML review
	UNFIXABLE = "UNFIXABLE"  # cannot be resolved without manual input


class Confidence(str, Enum):
	"""Confidence level of a diagnosis."""

	HIGH = "HIGH"
	MEDIUM = "MEDIUM"
	LOW = "LOW"


@dataclass
class BookMeta:
	"""Metadata of one book, normalized from metadata.json + opf + path.

	Fields use normalized forms: authors is a list, year is int|None, isbn is
	the canonical digits-only form (validated) or None.
	"""

	# Identity
	calibre_id: int | None = None
	uuid: str | None = None

	# Core metadata (the "current" values from metadata.json, fall back to opf)
	authors: list[str] = field(default_factory=list)
	title: str = ""
	subtitle: str | None = None
	isbn: str | None = None  # validated digits-only form
	publisher: str | None = None
	year: int | None = None
	language: str | None = None
	description: str | None = None
	series: list[dict[str, Any]] = field(default_factory=list)
	tags: list[str] = field(default_factory=list)
	genres: list[str] = field(default_factory=list)

	# Filesystem location
	path: str = ""  # absolute path to the book folder
	author_folder: str = ""  # the <Author> part of the path
	title_folder: str = ""  # the <Title> (<id>) part of the path
	formats: list[str] = field(default_factory=list)  # ['.epub', '.pdb', ...]
	primary_file: str | None = None  # best file for content extraction

	# Provenance: where each field's value came from
	source: str = "json"  # 'json' | 'opf' | 'path'

	# Encoding repair bookkeeping (filled by readers.py)
	# True if any text field was repaired from mojibake.
	encoding_repaired: bool = False
	# Fields where mojibake was detected but could NOT be repaired (need LLM/content lookup).
	encoding_unrepairable: list[str] = field(default_factory=list)

	def series_pair(self) -> tuple[str, str]:
		"""First series as ``(name, index)``, normalising the wild shapes.

		metadata.json in this library carries series either as plain strings
		(``"Zaklínač #8"``) or as ``{"name", "index"}`` dicts; newer
		Audiobookshelf builds write ``sequence`` for the index key. This is the
		ONE accessor for display / organize / OPF, so every consumer sees the
		same (name, index) regardless of the stored shape.
		"""
		if not self.series:
			return "", ""
		s0 = self.series[0]
		if not isinstance(s0, dict):
			return str(s0), ""
		idx = s0.get("index")
		if idx is None:
			idx = s0.get("sequence")
		return str(s0.get("name") or ""), str(idx) if idx is not None else ""

	def to_dict(self) -> dict[str, Any]:
		d = asdict(self)
		# Normalize for YAML/report readability
		return d


@dataclass
class Diagnosis:
	"""Result of applying detector rules to a BookMeta."""

	category: str  # 'C1'..'C10' or 'OK' / 'VERIFY_FAIL' / custom
	reason: str
	confidence: Confidence = Confidence.LOW
	verdict: Verdict = Verdict.OK
	# When verdict in (AUTO_FIXABLE, NEEDS_REVIEW), proposed may hold a fix
	proposed: dict[str, Any] | None = None
	proposed_source: str | None = None  # 'embedded' / 'obalkyknih' / 'llm' / ...
	# Other problems found on the same book (the rules below the primary in
	# priority order that also matched). detect() returns the first match as
	# the primary and stashes the rest here, so one book can carry several
	# diagnoses — e.g. C2 (filename-as-title) + C11 (generated cover). Consumers
	# that care about "any problem of kind X" use all_diagnoses() rather than
	# the single category. See detectors.detect_all.
	additional: list[Diagnosis] = field(default_factory=list)

	def to_dict(self) -> dict[str, Any]:
		return {
			"category": self.category,
			"reason": self.reason,
			"confidence": self.confidence.value,
			"verdict": self.verdict.value,
			"proposed": self.proposed,
			"proposed_source": self.proposed_source,
		}


@dataclass
class Book:
	"""BookMeta + its diagnosis + (later) verification result."""

	meta: BookMeta
	diagnosis: Diagnosis = field(default_factory=lambda: Diagnosis(category="OK", reason="no rule triggered"))

	# Verification (filled by verifier.py)
	verified: bool = False
	verify_reason: str | None = None
