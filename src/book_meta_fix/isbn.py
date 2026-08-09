"""ISBN extraction and validation.

ISBNs appear in the wild in many shapes:
  - plain 13 digits:  9788090131354
  - plain 10 digits:  8071741302
  - hyphenated:       80-85906-45-7  /  978-80-903850-5-4
  - with prefix:      ISBN:0072253606  /  ISBN-13: 978-...
  - with check digit X (ISBN-10): 080442957X

Canonicalize() strips to digits (preserving trailing X), then validates the
check digit and returns a 13-digit ISBN-13 (or None if invalid).
"""
from __future__ import annotations

import re

# Catch ISBN-10 or ISBN-13, optionally preceded by a label, possibly hyphenated/spaced.
# Must NOT be preceded/followed by another digit (avoids matching inside long numbers).
_ISBN_RE = re.compile(
	r"(?<![\dXx])"
	r"(?:ISBN(?:-1[03])?\s*[:\-]?\s*)?"  # optional "ISBN:" / "ISBN-13:" label
	r"(?P<isbn>"
	r"(?:97[89][- ]?\d{1,5}[- ]?\d{1,7}[- ]?\d{1,7}[- ]?\d)"  # ISBN-13
	r"|"
	r"(?:\d{1,5}[- ]?\d{1,7}[- ]?\d{1,7}[- ]?[\dXx])"  # ISBN-10
	r")"
	r"(?![\dXx])",
	re.IGNORECASE,
)


def extract_isbns(text: str) -> list[str]:
	"""Find all ISBN-like substrings in *text* and return them canonicalized.

	Returns only valid ISBN-13 strings (deduplicated, order preserved).
	"""
	seen: set[str] = set()
	out: list[str] = []
	for m in _ISBN_RE.finditer(text or ""):
		raw = m.group("isbn")
		canon = canonicalize(raw)
		if canon and canon not in seen:
			seen.add(canon)
			out.append(canon)
	return out


def extract_isbn(text: str) -> str | None:
	"""Return the first valid ISBN-13 found in *text*, or None."""
	isbns = extract_isbns(text)
	return isbns[0] if isbns else None


def canonicalize(raw: str | None) -> str | None:
	"""Normalize an ISBN string to a validated 13-digit ISBN-13, or None.

	Strips labels, hyphens, spaces; validates check digit; converts ISBN-10 to ISBN-13.
	"""
	if not raw:
		return None
	# Strip label and separators, keep digits and trailing X/x
	clean = re.sub(r"^[^0-9Xx]+", "", raw)  # leading label
	clean = clean.replace("-", "").replace(" ", "").strip()
	# A trailing 'X' is valid only for ISBN-10 check digit
	if not re.fullmatch(r"\d{9}[\dXx]|\d{13}", clean):
		return None
	upper = clean.upper()
	if len(upper) == 10:
		if not _check_isbn10(upper):
			return None
		return _isbn10_to_13(upper)
	if len(upper) == 13:
		return upper if _check_isbn13(upper) else None
	return None


def _check_isbn10(s: str) -> bool:
	"""Validate ISBN-10 check digit (last may be 'X' = 10)."""
	total = 0
	for i, ch in enumerate(s):
		val = 10 if ch == "X" else int(ch)
		total += val * (10 - i)
	return total % 11 == 0


def _check_isbn13(s: str) -> bool:
	"""Validate ISBN-13 check digit."""
	total = 0
	for i, ch in enumerate(s):
		d = int(ch)
		total += d if i % 2 == 0 else d * 3
	return total % 10 == 0


def _isbn10_to_13(s10: str) -> str:
	"""Convert a validated ISBN-10 to ISBN-13 (978 prefix)."""
	body = "978" + s10[:-1]
	# Recompute check digit
	total = 0
	for i, ch in enumerate(body):
		d = int(ch)
		total += d if i % 2 == 0 else d * 3
	check = (10 - (total % 10)) % 10
	return body + str(check)
