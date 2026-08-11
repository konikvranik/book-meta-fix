"""Deterministic metadata extraction from a book's page text (NOT from OPF).

The OPF metadata inside an EPUB may have been overwritten by calibre at import
time, so it cannot be trusted as an independent source. This module mines the
book's actual page text (the concatenated first-page text produced by
``extractors._epub_first_page_text``) for title / authors / ISBN / year /
publisher, using cheap offline heuristics. It is the first stage of the fix
pipeline (cheap, no network) and runs before online lookup and the LLM
fallback.

The heuristics were calibrated against a sample of real CZ/SK title pages from
this library. Key patterns (see scripts/llm_experiment.py and the title-page
inspection notes):

  - ``Neznámý`` is the library's placeholder-author string; it shows up as the
    literal first token on most title pages and must be dropped.
  - The title is very often ALL-CAPS on CZ/SK title pages
    (``JÁDRO GALAXIE``, ``ZASTAVENÝ PŘÍVAL``).
  - Some books carry explicit CZ field labels on a copyright page
    (``Název:``, ``Autor:``, ``Nakladatelství:``, ``Vydalo``, ``Přeložil``).
  - ``_strip_html`` leaks a ``Cover @page {...}`` CSS prefix that must be
    stripped before any heuristics run.

All extractors return ``None`` on no/low-confidence signal rather than
guessing — guesses are the LLM fallback's job.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .isbn import canonicalize

log = logging.getLogger(__name__)

# Lines/tokens that are pure noise on CZ/SK title pages and must never become a
# title or author. Compared after case-folding.
_PLACEHOLDER_TOKENS = {
	"neznámý", "nezanmy", "unknown", "anonym", "anonymní", "autor", "title",
	"subject", "name", "nc", "nc17",
	# CZ/SK structural sections that often appear ALL-CAPS on title pages and
	# must not be absorbed into the title run.
	"prolog", "epilog", "předmluva", "predmluva", "kapitola", "obsah",
	"další", "předchozí", "úvod", "uvod", "doslov",
}

# CSS / boilerplate that _strip_html leaks at the start of the first chunk.
_CSS_RE = re.compile(r"@\w+\s*\{[^}]*\}|body\s*\{[^}]*\}|\{[^}]{0,200}\}", re.IGNORECASE)
_COVER_PREFIX_RE = re.compile(r"^\s*cover\b", re.IGNORECASE)

# Web-navigation noise (some community EPUBs embed site nav in the title page).
_WEBNAV_RE = re.compile(r"(předchozí|další|obsah|hLavní\s+stránka|homepage|zpět|domů)", re.IGNORECASE)

# Filename-derived garbage that leaks into the text (e.g. calibre used the file
# name as the OPF title and it then shows on the title page).
_FILENAME_GARBAGE_RE = re.compile(
	r"microsoft\s+word\s*-\s*[^\s]*\.doc[x]?"
	r"|^[a-z0-9_]+\.(docx?|epub|pdf|pdb|mobi|txt|rtf)$",
	re.IGNORECASE,
)

# CZ/SK field labels on a copyright page. Capture group = the value. The value
# stops at the next known label or at a newline, so glued-together copyright
# lines like "Název: X Autor: Y Nakladatelství: Z" split correctly.
_LABEL_VALUE_END = r"(?=\s+(?:název|titul|kniha|autor|author|nakladatelství|vydavatelstvo|vydalo|vydavatel|přeložil|preložil|přeložila|preložila|rok\s+vydání|vydáno|rok|original|původní|edice)\s*[:=]|\s*$|\n)"
_LABEL_NAZEV = re.compile(r"(?:název|titul|kniha)\s*:\s*(.+?)" + _LABEL_VALUE_END, re.IGNORECASE)
_LABEL_AUTOR = re.compile(r"(?:autor|author)\s*:\s*(.+?)" + _LABEL_VALUE_END, re.IGNORECASE)
_LABEL_NAKLADATELSTVI = re.compile(
	r"(?:nakladatelství|vydavatelstvo|vydalo|vydavatel)\s*:\s*(.+?)" + _LABEL_VALUE_END,
	re.IGNORECASE,
)
_LABEL_PRELOZIL = re.compile(r"(?:přeložil[a]?|preložil[a]?|translated\s+by)\s+(.+?)" + _LABEL_VALUE_END, re.IGNORECASE)
_LABEL_ROK = re.compile(r"(?:rok\s+vydání|vydáno|rok)\s*:\s*((?:1[89]|20)\d{2})\b", re.IGNORECASE)

# CZ/SK diacritics letters — used to detect a "real" ALL-CAPS CZ/SK run (as
# opposed to random uppercase noise). A run with at least one of these (or a
# convincing length) is a strong title-page signal.
_CZ_LETTERS = set("áčďéěíňóřšťúůýžôäĺľŕšťž")

# 4-digit year near a publisher/copyright context.
_YEAR_RE = re.compile(r"\b((?:1[89]|20)\d{2})\b")


@dataclass
class TextMeta:
	"""Metadata mined from page text. Fields are None when no confident signal
	was found — callers should treat None as 'unknown', not as 'empty'."""

	title: str | None = None
	authors: list[str] = field(default_factory=list)
	isbn: str | None = None  # canonicalized
	publisher: str | None = None
	year: int | None = None
	source: str = "content"  # always 'content' for this module

	def has_any(self) -> bool:
		return bool(self.title or self.authors or self.isbn or self.publisher or self.year)


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _clean_page_text(text: str | None) -> str:
	"""Strip CSS leakage, KEEP line structure. Returns '' for None.

	Unlike a full whitespace collapse, this preserves newlines and the
	extractor's ' | ' chunk separators so label regexes (Název: / Autor: / ...)
	can anchor on line starts. Use _split_lines() to get the line list.
	"""
	if not text:
		return ""
	# Drop the `Cover @page {...} body {...}` CSS blob _strip_html leaks.
	cleaned = _CSS_RE.sub(" ", text)
	cleaned = _COVER_PREFIX_RE.sub(" ", cleaned)
	# Collapse spaced-out ALL-CAPS ("D A R K O Ň" -> "DARKOŇ") before line
	# splitting so the ALL-CAPS title heuristic can catch it.
	cleaned = _collapse_spaced_caps(cleaned)
	# Normalize the ' | ' chunk separator the extractor uses into newlines so
	# label regexes can anchor on ^ per logical line.
	cleaned = re.sub(r"\s*\|\s*", "\n", cleaned)
	# Collapse runs of spaces/tabs (but NOT newlines) within a line.
	cleaned = re.sub(r"[ \t]+", " ", cleaned)
	# Trim each line and drop leading/trailing blank lines.
	lines = [ln.strip() for ln in cleaned.split("\n")]
	cleaned = "\n".join(ln for ln in lines if ln)
	return cleaned


def _split_lines(text: str) -> list[str]:
	"""Split cleaned text into logical lines (newlines already normalized)."""
	if not text:
		return []
	return [ln for ln in text.split("\n") if ln.strip()]


def _is_noise(line: str) -> bool:
	"""True if a line is placeholder/garbage and must not become title/author."""
	folded = line.strip().lower().strip(".:;,-–—!?'\"()")
	if not folded:
		return True
	if folded in _PLACEHOLDER_TOKENS:
		return True
	# A line made entirely of placeholder tokens ("Neznámý Neznámý", "anonym unknown").
	tokens = [t.lower().strip(".:;,-–—!?'\"()") for t in line.split()]
	if tokens and all(t in _PLACEHOLDER_TOKENS for t in tokens):
		return True
	if _FILENAME_GARBAGE_RE.match(line):
		return True
	# Web-nav menus like "Předchozí | Další | Obsah | Hlavní stránka".
	if _WEBNAV_RE.fullmatch(folded):
		return True
	# Pure punctuation / symbols (ASCII-art borders handled separately).
	if not re.search(r"[A-Za-zÁ-ž]", line):
		return True
	return False


def _strip_leading_placeholder(s: str | None) -> str | None:
	"""Drop a leading placeholder token ('Neznámý', 'Unknown', ...) from *s*.

	These show up as the literal first token on most title pages and the
	ALL-CAPS / first-line heuristics sometimes absorb them into the candidate
	title (e.g. 'Neznámý 2002 Toyota Tundra ...'). Stripping the leading
	placeholder turns that into the real title.
	"""
	if not s:
		return s
	tokens = s.split()
	if not tokens:
		return s
	if tokens[0].lower().strip(".:;,-–—!?'\"()") in _PLACEHOLDER_TOKENS:
		rest = " ".join(tokens[1:])
		return rest or None
	return s


def _has_cz_diacritic(s: str) -> bool:
	return any(c in _CZ_LETTERS for c in s.lower())


def _looks_like_title(line: str) -> bool:
	"""Plausibility check for a candidate title."""
	if not line or len(line) < 3:
		return False
	if _is_noise(line):
		return False
	# Too long => probably a sentence, not a title.
	if len(line) > 120:
		return False
	return True


def _looks_like_author(line: str) -> bool:
	"""Plausibility check for a candidate author name.

	A real author name is 1-4 tokens, each Capitalized or ALL-CAPS, optionally
	with initials (J. R. R.), and not a sentence. We reject:

	  - sentences (>= 2 lowercase words of length >= 3: \"Obloha byla černá\")
	  - acronym fragments from prose (\"V Z\", \"S A K Z K\"): single-letter
	    tokens without trailing dots are not initials
	  - titles starting with a digit (\"451 stupňů Fahrenheita\")
	"""
	if not line or _is_noise(line):
		return False
	# Strip trailing role markers / parentheticals.
	cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", line).strip(" .,;:-–—")
	if not cleaned or len(cleaned) < 3 or len(cleaned) > 60:
		return False
	# Reject dialogue / sentence fragments that carry quotes or end in
	# sentence punctuation (a name never ends in "!" "?" "..." or quotes).
	if re.search(r'[!?“”"\']|(\.\.\.)', cleaned):
		return False
	# Reject if the raw line had closing dialogue punctuation we stripped.
	if re.search(r'[“”"\'][!\?]', line):
		return False
	# Reject leading digit (titles like "451 stupňů Fahrenheita").
	if cleaned[0].isdigit():
		return False
	tokens = cleaned.split()
	if len(tokens) > 4:
		return False
	# Reject sentences: >= 1 lowercase word of length >= 3 mixed with a
	# Capitalized word. A real author name has every word Capitalized or
	# an initial (A. S.); a sentence capitalizes only the first word and
	# keeps verbs/nouns lowercase ("Probudila se bolestí", "Darkoň je
	# bytost", "Obloha byla černá"). One lowercase word of length >= 3
	# next to a capitalized one is a reliable sentence signal in CZ/SK.
	alpha_tokens = [t for t in tokens if re.sub(r"[^A-Za-zÁ-ž]", "", t)]
	capitalized = [t for t in alpha_tokens if t[:1].isupper()]
	lower_words = [t for t in alpha_tokens if t.islower() and len(re.sub(r"[^A-Za-zÁ-ž]", "", t)) >= 3]
	if lower_words and capitalized:
		return False
	# Reject acronym fragments: lines of only single-letter tokens without
	# dots ("V Z", "S A K Z K"). A real initial has a trailing dot ("A. S.").
	non_punct = [t for t in tokens if re.sub(r"[^A-Za-zÁ-ž]", "", t)]
	if non_punct and all(
		len(re.sub(r"[^A-Za-zÁ-ž]", "", t)) <= 1 and not t.endswith(".")
		for t in non_punct
	):
		return False
	# Must contain at least one Capitalized token (a name part).
	if not any(t and t[0].isupper() for t in tokens):
		return False
	return True


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------


def extract_isbn_from_text(text: str | None) -> str | None:
	"""Return a canonical ISBN-13/10 found in *text*, or None.

	Reuses the project's isbn.extract_isbn (regex + checksum validation) so the
	result is identical to the existing first-page ISBN scan.
	"""
	if not text:
		return None
	from .isbn import extract_isbn

	raw = extract_isbn(text)
	if raw is None:
		return None
	canon = canonicalize(raw)
	return canon or None


def extract_title_from_text(text: str | None) -> str | None:
	"""Best-effort title from page text. Returns None if no confident signal.

	Any leading placeholder token ('Neznámý', 'Unknown', ...) accidentally
	absorbed into the candidate is stripped before returning.
	"""
	return _strip_leading_placeholder(_extract_title_from_text_raw(text))


def _extract_title_from_text_raw(text: str | None) -> str | None:
	"""Best-effort title from page text. Returns None if no confident signal."""
	cleaned = _clean_page_text(text)
	if not cleaned:
		return None
	lines = _split_lines(cleaned)

	# Priority 1: explicit CZ/SK field label "Název: ..." / "Titul: ...".
	m = _LABEL_NAZEV.search(cleaned)
	if m:
		val = m.group(1).strip().strip(".;,-–—")
		# A label value may itself be "Název: X (Y)" — take the part before "(".
		val = val.split("(")[0].strip()
		if _looks_like_title(val):
			return _normalize_title_case(val)

	# Priority 2: ALL-CAPS run (>= 2 words, at least one CZ letter or >= 4
	# letters) — the dominant CZ/SK title-page signal. Skip runs that are too
	# long (likely author+title glued together); those are left to the author
	# extractor and a weaker fallback.
	for ln in lines:
		if _is_noise(ln):
			continue
		title = _allcaps_title(ln)
		if title:
			return title

	# Priority 3: ASCII-art bordered title like "*** ANGLIČTINA ***".
	for ln in lines:
		# Strip border characters and check what remains.
		stripped = re.sub(r"^[*\-–—=_~#\s]+", "", ln)
		stripped = re.sub(r"[*\-–—=_~#\s]+$", "", stripped)
		if stripped and stripped != ln and _looks_like_title(stripped):
			return _normalize_title_case(stripped)

	# Priority 4: first non-noise, non-label line, if it looks title-ish and is
	# short. This is the weakest signal — only used when nothing better fired.
	for ln in lines[:4]:
		if _is_noise(ln):
			continue
		if _LABEL_AUTOR.match(ln) or _LABEL_NAKLADATELSTVI.match(ln):
			continue
		if _looks_like_title(ln) and len(ln) <= 80:
			return _normalize_title_case(ln)

	return None


# Spaced-out ALL-CAPS as used on some CZ/SK title pages: "D A R K O Ň N A
# C E S T Á CH". Match a run of single capital letters separated by single
# spaces, optionally ending in a 2-3 letter uppercase token (CZ digraphs
# like "CH"). Requires a whitespace/start boundary on each side so it does
# not eat into neighbouring words like the "ý" of "Neznámý". Requires at
# least 5 single-letter tokens.
_SPACED_CAPS_RE = re.compile(
	r"(?:(?<=\s)|(?<=^))(?:[A-ZÁ-Ž]\s){4,}[A-ZÁ-Ž](?:[A-ZÁ-Ž]{1,2})?(?=\s|$)"
)


def _collapse_spaced_caps(text: str) -> str:
	"""Collapse 'D A R K O Ň' -> 'DARKOŇ' so the ALL-CAPS heuristic can find it.

	Only collapses runs of single-letter caps separated by single spaces; other
	text is left untouched. Runs are rejoined into a single token.
	"""

	def _join(m: re.Match) -> str:
		# Strip the spaces between single letters: "D A R K O Ň" -> "DARKOŇ".
		return m.group(0).replace(" ", "")

	return _SPACED_CAPS_RE.sub(_join, text)


def _allcaps_title(line: str) -> str | None:
	"""If *line* is (mostly) an ALL-CAPS run of >= 2 words, return it as a
	title (restored to title case). Otherwise None.

	Skips lines with too many ALL-CAPS tokens (>= 7), which on CZ title pages
	usually mean an author name and a title glued onto one line — splitting
	those reliably is not possible without labels. Structural stop-words
	(PROLOG, KAPITOLA, PŘEDMLUVA, ...) are dropped from the run before it is
	considered, so 'ČAS PŘÍLIVU PROLOG' yields 'Čas Přílivu' rather than
	'Čas Přílivu Prolog'.
	"""
	tokens = line.split()
	if len(tokens) < 2:
		return None
	# Count uppercase-dominant tokens (allowing CZ diacritics + punctuation).
	upper_tokens = [
		t for t in tokens
		if t and (t.isupper() or t.rstrip(".,:;!?-–—\"'()").isupper())
	]
	# Drop structural stop-words (PROLOG, KAPITOLA, ...) that often appear
	# ALL-CAPS on title pages but are not part of the title.
	upper_tokens = [t for t in upper_tokens if t.lower().strip(".:;,-–—!?'\"()") not in _PLACEHOLDER_TOKENS]
	if len(upper_tokens) < 2 or len(upper_tokens) >= 7:
		return None
	# Require at least one "heavy" token: >= 4 letters, or any CZ diacritic.
	if not any(len(re.sub(r"[^A-Za-zÁ-ž]", "", t)) >= 4 or _has_cz_diacritic(t) for t in upper_tokens):
		return None
	# Drop trailing non-upper tokens (page numbers, "KAPITOLA PRVNÍ" continuations
	# are kept as part of the title).
	run = " ".join(upper_tokens)
	if not _looks_like_title(run):
		return None
	return _normalize_title_case(run)


def _normalize_title_case(s: str) -> str:
	"""Restore sensible title case from an ALL-CAPS source.

	Keeps the first letter of each word uppercase and lowercases the rest,
	preserving CZ/SK diacritics. Does NOT lowercase short all-caps acronyms
	that are likely intentional (e.g. "IBM", "USA") — heuristic: tokens of
	<= 3 uppercase letters with no diacritics stay uppercase.
	"""
	words: list[str] = []
	for w in s.split():
		core = re.sub(r"[^A-Za-zÁ-ž]", "", w)
		if core.isupper() and len(core) <= 3 and not _has_cz_diacritic(w):
			words.append(w)  # acronym
		else:
			words.append(w[:1].upper() + w[1:].lower() if w else w)
	return " ".join(words)


def extract_authors_from_text(text: str | None) -> list[str]:
	"""Best-effort author list from page text. Empty list if no signal."""
	cleaned = _clean_page_text(text)
	if not cleaned:
		return []
	lines = _split_lines(cleaned)
	authors: list[str] = []

	# Priority 1: explicit CZ label "Autor: ...".
	for m in _LABEL_AUTOR.finditer(cleaned):
		val = m.group(1).strip().strip(".;,-–—")
		# Labels may list "Jmeno Prijmeni (role)" — keep the name part.
		val = val.split("(")[0].strip()
		if _looks_like_author(val):
			authors.append(_normalize_title_case(val))
	if authors:
		return _dedupe(authors)

	# Priority 2: a short standalone line on the title page that looks like a
	# person name. We scan only the first ~8 lines (the title-page region
	# before the first paragraph) and accept any 1-4 token Capitalized /
	# ALL-CAPS / initials line that is NOT the chosen title. ALL-CAPS lines
	# are normalized to title case first so "KAREL ČAPEK" matches.
	title = extract_title_from_text(text)
	title_norm = title.lower() if title else None
	for ln in lines[:8]:
		if _is_noise(ln):
			continue
		tokens = ln.split()
		if len(tokens) < 1 or len(tokens) > 4:
			continue
		# Drop a leading placeholder ("Neznámý") before re-casing, so that
		# a title page line "Neznámý ZASTAVENÝ PŘÍVAL" (where "Neznámý" is the
		# library placeholder-author marker, not a real name) does not get
		# absorbed as an author candidate.
		ln_stripped = _strip_leading_placeholder(ln) or ""
		if not ln_stripped:
			continue
		# Validate on the RAW (pre-normalization) form first: a mixed-case
		# line with lowercase words of length >= 3 is a sentence fragment
		# ("Nějaký další text"), not a name. Only after the raw check passes
		# do we re-case ALL-CAPS to title case so "KAREL ČAPEK" reads as a name.
		if not _looks_like_author(ln_stripped):
			# ALL-CAPS lines pass _looks_like_author on raw form (no lowercase
			# words), so this branch only catches mixed-case rejects. Try the
			# normalized form as a last resort only if the line was ALL-CAPS
			# (where normalization is lossless and safe).
			if not ln_stripped.isupper():
				continue
			candidate = _normalize_title_case(ln_stripped)
			if not _looks_like_author(candidate):
				continue
		else:
			candidate = _normalize_title_case(ln_stripped)
		if title_norm and candidate.lower() == title_norm:
			continue  # this line is the title, not an author
		# Also reject if the candidate contains the title as a suffix/prefix
		# (e.g. placeholder + title collapsed onto one line).
		if title_norm and (
			candidate.lower().startswith(title_norm + " ")
			or candidate.lower().endswith(" " + title_norm)
		):
			continue
		authors.append(candidate)
		if len(authors) >= 3:
			break

	return _dedupe(authors)


def extract_publisher_from_text(text: str | None) -> str | None:
	cleaned = _clean_page_text(text)
	if not cleaned:
		return None
	m = _LABEL_NAKLADATELSTVI.search(cleaned)
	if m:
		val = m.group(1).strip()
		# Cut at a comma followed by a year or a city — "Academia, 2014" /
		# "Argo, Praha 1999" keep just the publisher name.
		val = re.sub(r",\s.*$", "", val)
		# Strip a trailing year.
		val = re.sub(r"\s+(?:1[89]|20)\d{2}\s*$", "", val).strip()
		# If an ALL-CAPS block leaked into the value, cut at the first run of
		# >= 3 consecutive ALL-CAPS tokens.
		val = re.sub(r"\s+[A-ZÁ-Ž]{2,}(?:\s+[A-ZÁ-Ž]{2,}){2,}.*$", "", val).strip()
		val = val.strip(".;,-–—")
		if val and len(val) <= 80:
			return val
	return None


def extract_year_from_text(text: str | None) -> int | None:
	cleaned = _clean_page_text(text)
	if not cleaned:
		return None
	# Priority 1: explicit "Rok vydání: YYYY" / "Vydáno: YYYY".
	m = _LABEL_ROK.search(cleaned)
	if m:
		try:
			return int(m.group(1))
		except ValueError:
			pass
	# Priority 2: a 4-digit year near a publisher/copyright marker.
	for ctx in (_LABEL_NAKLADATELSTVI,):
		lm = ctx.search(cleaned)
		if lm:
			# Look at the 40 chars around the label match for a year.
			span_start = max(0, lm.start() - 20)
			span_end = min(len(cleaned), lm.end() + 40)
			ym = _YEAR_RE.search(cleaned[span_start:span_end])
			if ym:
				try:
					return int(ym.group(1))
				except ValueError:
					pass
	return None


def extract_metadata_from_text(text: str | None) -> TextMeta:
	"""Mine all fields from page text. Returns a TextMeta (fields None when no
	confident signal). Always returns a TextMeta (possibly empty); never None."""
	return TextMeta(
		title=extract_title_from_text(text),
		authors=extract_authors_from_text(text),
		isbn=extract_isbn_from_text(text),
		publisher=extract_publisher_from_text(text),
		year=extract_year_from_text(text),
	)


# ---------------------------------------------------------------------------
# Internal: dedupe
# ---------------------------------------------------------------------------


def _dedupe(items: list[str]) -> list[str]:
	"""Case-insensitive dedupe, preserving order."""
	seen: set[str] = set()
	out: list[str] = []
	for it in items:
		key = it.lower().strip()
		if key and key not in seen:
			seen.add(key)
			out.append(it)
	return out
