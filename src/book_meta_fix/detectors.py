"""Detector rules C1–C10 — classify each book's metadata corruption.

Each rule is a function (meta: BookMeta) -> Diagnosis | None.
`detect()` applies rules in priority order and returns the first match; if no
rule fires, the book is OK (and will be passed to the verifier for content
validation).

Corruption categories (from empirical study of the library):

	C1  author/title swapped               NEEDS_REVIEW (LLM/swap)
	C2  filename-as-title (diacritics lost) AUTO_FIXABLE (when combined
	    with other signals; alone too noisy — 47% of titles have '_')
	C3  series/library/publisher as author  NEEDS_REVIEW
	C4  encoding corruption (unrepairable)  NEEDS_REVIEW (LLM)
	C5  placeholder record                  NEEDS_REVIEW (metadata corrupted; recover from page text)
	C6  Word lock-file duplicate            AUTO_FIXABLE (delete)
	C7  glued authors ("byXandY")           NEEDS_REVIEW
	C8  translator mislabeled as author     NEEDS_REVIEW
	C9  anonym (mostly fake — real anonym is whitelisted) NEEDS_REVIEW
	C10 long comma-separated author list    NEEDS_REVIEW (mostly real multi-author)
	C11 generated cover (calibre placeholder) NEEDS_REVIEW (download replacement)
	EXTRA: missing ISBN / year              AUTO_FIXABLE (enrichable)
	EXTRA: missing cover / generated cover  AUTO_FIXABLE / NEEDS_REVIEW (download)
"""
from __future__ import annotations

import re
from typing import Callable

from .models import BookMeta, Confidence, Diagnosis, Verdict

# A detector rule: returns Diagnosis if it matches, else None.
Rule = Callable[[BookMeta], "Diagnosis | None"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Czech/Slovak "anonymous" spellings — almost always signal a corrupted record,
# NOT a genuine anonymous work. Genuine anonym (Bible etc.) is whitelisted below.
# Compared case-insensitively (callers .lower() before checking membership).
_ANONYM_SPELLINGS = {
	"", "anonym", "anonymní", "anonymni", "anonymous", "neznamy", "neznámý", "unknown",
}

# Titles that indicate a GENUINE anonymous work (religion/folklore).
_REAL_ANONYM_RE = re.compile(r"\b(bible|bibl[ei]|kralick|[mn]ový?\s+z[áa]kon|knihy\s+moj|koran|quran|edda)\b", re.IGNORECASE)

# Filename-like patterns in the title
_EXTENSION_RE = re.compile(r"\.(docx?|epub|pdf|pdb|mobi|azw|lit|rtf|txt|djvu|fm|prz)\b", re.IGNORECASE)
_WORD_TMP_RE = re.compile(r"^microsoft\s+word\s*-\s*", re.IGNORECASE)
# Truncated-title markers (Calibre's slugify leaves these behind)
_TRUNCATED_RE = re.compile(r"[_]n[_]?_$|_txt$|_n$")

# Placeholder record: literally "title" / "author" / "subject"
_PLACEHOLDER_RE = re.compile(r"^(title|author|subject|name|unknown)$", re.IGNORECASE)

# Word lock-file prefix
_WORD_LOCK_RE = re.compile(r"^~\$")

# Glued-author patterns: "byX...andY", "XandY" without spaces around connectives
_GLUED_RE = re.compile(r"\b(by[A-Z]|[a-z]and[A-Z]|[a-z]and\s|[A-Z][a-z]+[a-z]and[A-Z])")

# Czech diacritics — used to detect CZ vs foreign author names
_CZ_DIACRITICS = set("áčďéěíňóřšťúůýžôÁČĎÉĚÍŇÓŘŠŤÚŮÝŽÔ")

# Common Czech/Slovak name fragments (rough heuristic — diacritics OR known names)
_CZ_NAME_HINTS = {
	"jan", "jiří", "karel", "pavel", "petr", "michal", "tomáš", "marek", "martin",
	"josef", "františek", "vlastimil", "zdeněk", "milan", "vladimír", "jarmila",
	"jana", "lenka", "eva", "hana", "marie", "anna", "alena", "lidmila",
	"kotrle", "kotrla", "soukup", "smékal", "blažek", "macák", "kantůrek",
	"emmerová", "malčíková", "petrů", "kosatíková", "dufek", "maxon", "moravcová",
	"netopil", "beránek", "batrla", "koubová", "ryšková", "kacetlová", "hrubá",
}

# Foreign-author hints (very rough — presence of these tokens signals non-CZ)
_FOREIGN_NAME_HINTS = {
	"king", "christie", "asimov", "clark", "clarke", "tolkien", "pratchett",
	"sheldon", "burroughs", "le", "guin", "herbert", "bradbury", "orwell",
	"brown", "rowling", "martin", "shakespeare", "dickens", "twain", "poe",
	"verne", "doyle", "lovecraft", "heinlein", "niven", "sagan", "strugack",
}

# Common Czech "series as author" patterns
_SERIES_AS_AUTHOR_RE = re.compile(
	r"\b(část|cast|díl|dil|kapitol|antologie|série|serie|kniha)\b\s*[0-9ivx]+",
	re.IGNORECASE,
)


def _strip_accents(s: str) -> str:
	"""Remove Czech diacritics for fuzzy comparison."""
	repl = str.maketrans("áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ", "acdeeinorstuuyzACDEEINORSTUUYZ")
	return s.translate(repl)


def _looks_cz_name(name: str) -> bool:
	"""Heuristic: does *name* look like a Czech/Slovak person name?"""
	if not name:
		return False
	if any(c in _CZ_DIACRITICS for c in name):
		return True
	low = _strip_accents(name).lower()
	tokens = re.split(r"[\s.,]+", low)
	return any(t in _CZ_NAME_HINTS for t in tokens)


def _looks_foreign_name(name: str) -> bool:
	"""Heuristic: does *name* look like a non-Czech author name?

	A name is "foreign" if it has NO Czech diacritics AND no CZ name-hint tokens.
	Anything that looks like a plausible person name but isn't Czech is treated
	as foreign — this is deliberately broad so the translator detector catches
	most cases. False positives (e.g. a Czech author whose name happens to be
	ASCII-only) are acceptable: the verdict is NEEDS_REVIEW, not auto-fix.
	"""
	if not name:
		return False
	# Must look like a person name (at least one capitalized token, ≥3 chars total)
	if len(name) < 3:
		return False
	tokens = [t for t in re.split(r"[\s.,]+", name) if t]
	if not tokens or not any(t[0].isupper() for t in tokens):
		return False
	# Reject if it has CZ diacritics OR is in the CZ name-hint list
	if _looks_cz_name(name):
		return False
	# Reject obvious non-name strings (digit-led, sentence-like, ≥5 tokens)
	if tokens[0][0].isdigit():
		return False
	if len(tokens) >= 5:
		return False
	return True


# ---------------------------------------------------------------------------
# Rules (in priority order — first match wins)
# ---------------------------------------------------------------------------


def rule_c6_word_lockfile(meta: BookMeta) -> Diagnosis | None:
	"""C6: MS-Word lock-file duplicate (~$ prefix)."""
	if _WORD_LOCK_RE.match(meta.author_folder):
		return Diagnosis(
			category="C6",
			reason=f"author_folder začíná '~$' (Word lock-file dup): {meta.author_folder!r}",
			confidence=Confidence.HIGH,
			verdict=Verdict.AUTO_FIXABLE,
			proposed={"action": "delete", "reason": "duplicate of a Word lock file"},
		)
	return None


def rule_c5_placeholder(meta: BookMeta) -> Diagnosis | None:
	"""C5: literal placeholder record ('author'/'title'/'subject').

	The metadata are corrupted (Calibre overwrote them with a placeholder), but
	the book itself may still hold the real title/author. Reliable recovery
	requires extracting metadata from the page text (a planned enhancement);
	until then this needs a human in the loop.
	"""
	if _PLACEHOLDER_RE.match(meta.title) or any(_PLACEHOLDER_RE.match(a) for a in meta.authors):
		return Diagnosis(
			category="C5",
			reason=f"placeholder record: author={meta.authors!r} title={meta.title!r}",
			confidence=Confidence.HIGH,
			verdict=Verdict.NEEDS_REVIEW,
		)
	return None


def rule_c4_encoding(meta: BookMeta) -> Diagnosis | None:
	"""C4: metadata has unrepairable mojibake — needs LLM/content lookup."""
	if meta.encoding_unrepairable:
		fields = ", ".join(meta.encoding_unrepairable)
		return Diagnosis(
			category="C4",
			reason=f"unrepairable mojibake in field(s): {fields}",
			confidence=Confidence.HIGH,
			verdict=Verdict.NEEDS_REVIEW,
		)
	return None


def rule_c7_glued_authors(meta: BookMeta) -> Diagnosis | None:
	"""C7: authors glued together with 'by'/'and' without spaces."""
	for a in meta.authors:
		if _GLUED_RE.search(a):
			return Diagnosis(
				category="C7",
				reason=f"glued author tokens: {a!r}",
				confidence=Confidence.MEDIUM,
				verdict=Verdict.NEEDS_REVIEW,
			)
	return None


def rule_c1_swap(meta: BookMeta) -> Diagnosis | None:
	"""C1: author/title swapped. Detects two patterns:

	1. Author name (token ≥5 chars, looks like a real surname) literally appears
	   in the title — but ONLY when the author itself is clean (no filename/mojibake
	   characters), otherwise this is really a C2 filename-pollution case.
	2. The author_folder looks like a book title (sentence, not a person name)
	   AND the title looks like a person name.
	"""
	# Guard: if the author itself is polluted (filename chars, mojibake), this is
	# not a swap — it's a C2 filename case. Skip C1.
	def _is_clean_name(name: str) -> bool:
		if not name:
			return False
		# Reject names with filename/encoding artifacts
		if "_" in name or _EXTENSION_RE.search(name):
			return False
		if any(ord(c) > 0x2000 for c in name):  # fancy punctuation, mojibake
			return False
		# Reject names that look like sentences (4+ lowercase words)
		tokens = [t for t in re.split(r"[\s]+", name) if t]
		if len(tokens) >= 4:
			return False
		return True

	title_low = _strip_accents(meta.title).lower()
	# Common words that are NOT surname signals — never treat as swap evidence
	COMMON_WORDS = {
		"the", "and", "king", "lord", "dark", "book", "magicky", "rohan", "livu",
		"first", "second", "third", "new", "old", "good", "bad", "man", "woman",
		"world", "time", "life", "death", "love", "war", "house", "city", "road",
	}

	# Pattern 1: a substantial author token (≥5 chars) appears in the title
	for a in meta.authors:
		if not _is_clean_name(a):
			continue
		a_low = _strip_accents(a).lower()
		tokens = [t for t in re.split(r"[^a-z]+", a_low) if len(t) >= 5]
		for tok in tokens:
			if tok in COMMON_WORDS:
				continue
			if tok in title_low and len(title_low) >= len(tok) + 2:
				return Diagnosis(
					category="C1",
					reason=f"author surname {tok!r} appears in title — possible swap or title pollution",
					confidence=Confidence.MEDIUM,
					verdict=Verdict.NEEDS_REVIEW,
				)
	# Pattern 2: author_folder looks like a title (4+ words) AND title looks like a name
	af = meta.author_folder
	if _is_clean_name(af):
		af_tokens = [t for t in re.split(r"[\s_-]+", af) if t]
		if len(af_tokens) >= 4 and not any(t.lower() in _CZ_NAME_HINTS for t in af_tokens):
			title_tokens = [t for t in re.split(r"\s+", meta.title) if t]
			if 1 <= len(title_tokens) <= 3 and all(t[0].isupper() or not t[0].isalpha() for t in title_tokens):
				return Diagnosis(
					category="C1",
					reason=f"author_folder {af!r} looks like a title; title {meta.title!r} looks like a name",
					confidence=Confidence.MEDIUM,
					verdict=Verdict.NEEDS_REVIEW,
				)
	return None


def rule_c2_filename_title(meta: BookMeta) -> Diagnosis | None:
	"""C2: title is actually a filename (diacritics stripped, extension present).

	Alone, '_' in title is too noisy (47% of library). We require a STRONGER
	signal: either a file extension in the title, or a Word-temp prefix, or a
	truncated marker, or a near-exact match with the primary file's stem.

	The filename-stem match compares the title to the primary file's stem
	(minus extension and ' - Author' suffix) **directly** (case-insensitive,
	not accent-stripped). Calibre strips diacritics from filenames but not
	from the title field, so a healthy book whose title is "Čas přílivu" will
	have a filename "Cas prilivu - Author.epub" — the stem and title do NOT
	match without accent-stripping, so the rule does not fire. Only when the
	title field itself IS the filename (no diacritics) does the match succeed.
	"""
	reasons: list[str] = []
	if _EXTENSION_RE.search(meta.title):
		reasons.append("file extension in title")
	if _WORD_TMP_RE.match(meta.title):
		reasons.append("MS-Word temp filename prefix")
	if _TRUNCATED_RE.search(meta.title):
		reasons.append("truncated slug marker (_n_ / _txt)")
	# Filename-stem match: title IS the primary file's stem (minus ' - Author'
	# suffix). Compare directly (case-insensitive), NOT accent-stripped — a
	# healthy book's title has diacritics that the filename lacks, so they
	# won't match. This only fires when the title field is literally the
	# filename (a genuine filename-as-title corruption).
	if meta.primary_file:
		import os

		stem = os.path.basename(meta.primary_file)
		# Strip extension
		for ext in (".epub", ".pdb", ".pdf", ".mobi", ".doc", ".rtf", ".txt", ".lit", ".djvu"):
			if stem.lower().endswith(ext):
				stem = stem[: -len(ext)]
				break
		# Strip trailing ' - <something>' (author)
		stem = re.sub(r"\s*-\s*[^-]+$", "", stem).strip()
		if stem and stem.lower() == meta.title.lower():
			reasons.append("title == primary file stem")
	if reasons:
		return Diagnosis(
			category="C2",
			reason="; ".join(reasons),
			confidence=Confidence.HIGH,
			verdict=Verdict.NEEDS_REVIEW,  # need content/online to know the *right* title
		)
	# Weaker signal: heavy underscores (3+) without other corruption — still flag
	underscore_count = meta.title.count("_")
	if underscore_count >= 3:
		return Diagnosis(
			category="C2",
			reason=f"heavy diacritics loss ({underscore_count} underscores in title)",
			confidence=Confidence.MEDIUM,
			verdict=Verdict.NEEDS_REVIEW,
		)
	return None


def rule_c3_series_as_author(meta: BookMeta) -> Diagnosis | None:
	"""C3: a series/part/library name is in the author field."""
	# Combine author + author_folder for the check
	for candidate in [meta.author_folder, *meta.authors]:
		if candidate and _SERIES_AS_AUTHOR_RE.search(candidate):
			return Diagnosis(
				category="C3",
				reason=f"series/part marker in author field: {candidate!r}",
				confidence=Confidence.MEDIUM,
				verdict=Verdict.NEEDS_REVIEW,
			)
	return None


def rule_c8_translator(meta: BookMeta) -> Diagnosis | None:
	"""C8: a translator is mislabeled as a second author.

	Signal: 2+ authors, mix of CZ-looking and foreign-looking names. With
	2-3 authors and exactly one foreign name, the CZ names are likely
	translators. With 2+ foreign names, it's more likely a real anthology.
	"""
	if len(meta.authors) < 2:
		return None
	cz_names = [a for a in meta.authors if _looks_cz_name(a)]
	foreign_names = [a for a in meta.authors if _looks_foreign_name(a)]
	# Strong: exactly one foreign author + 1-2 CZ names = translator case
	if len(foreign_names) == 1 and 1 <= len(cz_names) <= 2:
		return Diagnosis(
			category="C8",
			reason=f"likely translator as author: foreign={foreign_names}, cz={cz_names}",
			confidence=Confidence.MEDIUM,
			verdict=Verdict.NEEDS_REVIEW,
			proposed={"translators": cz_names, "authors": foreign_names},
		)
	# Weaker: 4+ authors — could be anthology OR translator team
	if len(meta.authors) >= 4:
		return Diagnosis(
			category="C10",
			reason=f"{len(meta.authors)} authors — verify anthology vs translator list",
			confidence=Confidence.LOW,
			verdict=Verdict.NEEDS_REVIEW,
		)
	return None


def rule_c9_anonym(meta: BookMeta) -> Diagnosis | None:
	"""C9: anonymous author. Genuinely anonymous works (Bible etc.) are
	whitelisted; everything else with an anonym spelling is flagged because
	99.7% of such records in this library are corrupted (lost author), not
	truly anonymous.

	The anonym signal must come from the actual metadata (authors list), not
	merely from the author_folder. ~15% of the library lives in a 'Neznamy/'
	folder but already carries a real author in metadata.json (calibre was
	fixed at some point, the folder was never moved). Flagging those as C9
	is a false positive — the metadata is already correct.
	"""
	# A real (non-anonym) author in the metadata means the record is fine,
	# regardless of what folder it happens to live in.
	has_real_author = any(
		a.strip().lower() not in _ANONYM_SPELLINGS
		for a in meta.authors
		if a
	)
	if has_real_author:
		return None
	# No real author in metadata — check whether the folder signals anonym.
	# (author_folder alone, without an anonym authors[], is still C9 because
	# the metadata has no author at all and the folder confirms it.)
	is_anonym = any(
		a.strip().lower() in _ANONYM_SPELLINGS
		for a in (meta.author_folder, *meta.authors)
		if a
	)
	if not is_anonym:
		return None
	# Whitelist: title looks like a genuine religious/folkloric anonymous work
	if _REAL_ANONYM_RE.search(meta.title):
		return Diagnosis(
			category="C9",
			reason="genuine anonymous work (religious/folkloric title)",
			confidence=Confidence.HIGH,
			verdict=Verdict.OK,
		)
	# Everything else with anonym spelling is suspect
	return Diagnosis(
		category="C9",
		reason=f"author marked anonymous but title {meta.title!r} doesn't look anonymous — likely lost author",
		confidence=Confidence.MEDIUM,
		verdict=Verdict.NEEDS_REVIEW,
	)


def rule_missing_isbn(meta: BookMeta) -> Diagnosis | None:
	"""Missing ISBN — auto-fixable via online lookup (obalkyknih / Google Books)."""
	if not meta.isbn:
		return Diagnosis(
			category="MISSING_ISBN",
			reason="no valid ISBN in metadata",
			confidence=Confidence.LOW,
			verdict=Verdict.AUTO_FIXABLE,
		)
	return None


def rule_missing_year(meta: BookMeta) -> Diagnosis | None:
	"""Missing publication year — auto-fixable via online lookup."""
	if meta.year is None:
		return Diagnosis(
			category="MISSING_YEAR",
			reason="no publication year in metadata",
			confidence=Confidence.LOW,
			verdict=Verdict.AUTO_FIXABLE,
		)
	return None


# ---------------------------------------------------------------------------
# Cover rules (C11 generated, MISSING_COVER absent)
# ---------------------------------------------------------------------------
#
# These are enrichment-tier rules: a generated/missing cover is not metadata
# corruption, but a fixable quality issue. They run after the structural and
# ISBN/year enrichment rules. When cover_url is available from an enricher
# (preferably databazeknih), the ReviewWriter pre-fills action:accept and
# `bmf apply` downloads the replacement.


def rule_generated_cover(meta: BookMeta) -> Diagnosis | None:
	"""C11: auto-generated (Calibre placeholder) cover detected.

	Fires when cover.jpg exists AND pixel analysis classifies it as generated
	(1200x1600 default template + low colour count + dominant background).
	Returns NEEDS_REVIEW so a human sees the proposal before the replacement
	cover is downloaded — though the ReviewWriter pre-fills action:accept when
	a cover_url is available.
	"""
	from pathlib import Path

	cover_path = Path(meta.path) / "cover.jpg"
	if not cover_path.is_file():
		return None
	from .covers import analyze_cover

	info = analyze_cover(cover_path)
	if not info.is_generated:
		return None
	signals = ", ".join(info.signals) if info.signals else f"{info.width}x{info.height}"
	return Diagnosis(
		category="C11",
		reason=f"generated cover ({signals})",
		confidence=Confidence.HIGH,
		verdict=Verdict.NEEDS_REVIEW,
	)


def rule_missing_cover(meta: BookMeta) -> Diagnosis | None:
	"""MISSING_COVER: no cover.jpg sidecar at all.

	Auto-fixable: if an enricher has a cover_url, `bmf apply` will download it.
	Fires only when cover.jpg is entirely absent (PDFs/PDBs may have an
	embedded cover but no sidecar — those are left for a future extractor).
	"""
	from pathlib import Path

	cover_path = Path(meta.path) / "cover.jpg"
	if cover_path.is_file():
		return None
	return Diagnosis(
		category="MISSING_COVER",
		reason="no cover.jpg sidecar",
		confidence=Confidence.LOW,
		verdict=Verdict.AUTO_FIXABLE,
	)


# Priority-ordered list of structural rules (NOT the missing-ISBN/year ones —
# those are enrichment opportunities applied only to books that pass the
# structural checks as OK).
# Order matters: C2 (filename pollution) is checked BEFORE C1 (swap) because
# polluted titles often produce false swap signals (the filename contains both
# author and title). Once C2 fires, C1 is skipped.
RULES: list[Rule] = [
	rule_c6_word_lockfile,
	rule_c5_placeholder,
	rule_c4_encoding,
	rule_c2_filename_title,  # before C1 — filename pollution masks swap signals
	rule_c7_glued_authors,
	rule_c1_swap,
	rule_c3_series_as_author,
	rule_c8_translator,
	rule_c9_anonym,
]

# Enrichment rules — applied to books that passed all structural rules as OK
# (these are not corruption, just missing data we can fetch).
# Cover rules are last: C11/MISSING_COVER are lower priority than metadata
# enrichment, and their cover_url pre-fill in the ReviewWriter is independent
# of the metadata proposal.
ENRICHMENT_RULES: list[Rule] = [
	rule_missing_isbn,
	rule_missing_year,
	rule_generated_cover,
	rule_missing_cover,
]


def detect(meta: BookMeta) -> Diagnosis:
	"""Apply rules in priority order; return the first matching Diagnosis.

	If no structural rule fires, the book is OK. Then enrichment rules are
	checked — if any fire, the book is OK-with-enrichment (still auto-fixable,
	but for a different reason: missing data, not corruption).
	"""
	for rule in RULES:
		d = rule(meta)
		if d is not None:
			return d
	# No corruption found — check for enrichment opportunities
	for rule in ENRICHMENT_RULES:
		d = rule(meta)
		if d is not None:
			return d
	# Truly clean
	return Diagnosis(
		category="OK",
		reason="no structural or enrichment rule triggered",
		confidence=Confidence.HIGH,
		verdict=Verdict.OK,
	)
