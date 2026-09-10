"""Library-wide normalization of author-name variants and genre tags.

The per-book detectors (detectors.py) judge one folder in isolation; the two
messes this module targets only become visible ACROSS the library:

* C15 — the same person spelled several ways: "Robert A. Heinlein" vs
  "Robert Anson Heinlein", "Neznamy" vs "Neznámý", "Ing. Václav Semerád",
  ALL-CAPS or mojibake copies, and swapped name order ("Příjmení, Jméno"
  Calibre convention or plain "Podroužek Přemysl").
* C16 — genre/tag strings that differ only by case, diacritics, word order
  or language ("humor"/"Humor"/"mumour"/"Comedy", "sci-fi"/"Sci-fi"/
  "Science Fiction").
* C18 — the same series spelled several ways: "Zaklínač"/"zaklínač"/
  "Zaklinac" (fold), "Perry Rodan"/"Perry Rhodan" (alias table), or a
  suffixed sibling "Mark Stone"/"Mark Stone (edice)" (suspect tier, weighed
  by volume-numbering complementarity and an optional online existence
  check). The volume INDEX is never proposed — only the name changes.

The engine is pure (no I/O): `analyze_library` takes the scanned BookMetas
and returns clusters + per-book proposals. The CLI turns those into review.yaml
entries (C15/C16 categories, emitted ONLY here — detect() never fires them),
and `bmf apply` writes them through the ordinary proposed.authors/genres/tags
path. Everything is human-gated: deterministic classes (case/diacritics fold,
initials vs full name, anonym spellings, title strip, the alias table) are
pre-filled accept; judgement calls (letter variants like Frederik/Frederick,
unevidenced comma reorders) stay pending.

Canonical-form policy (agreed with the user): Czech genre names, curated
synonym table + fold duplicates only (no semantic fuzzy merging), and for
authors the most frequent NON-degraded spelling (so "Neznamy" x63 loses to
"Neznámý" x24 — frequency alone would canonise the mangled form).
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from .detectors import _is_anonym_spelling, split_series_index
from .models import BookMeta, Confidence, series_entry_pair

# ---------------------------------------------------------------------------
# Shared string hygiene
# ---------------------------------------------------------------------------

# Czech + Slovak accented letters (both cases) → plain ASCII. Kept in ONE
# table here — the copies in verifier/detectors predate this module and are
# not touched (mass-refactoring them is noise, per commit etiquette).
_DIA_SRC = "áčďéěíňóřšťúůýžôäüąĺľŕÁČĎÉĚÍŇÓŘŠŤÚŮÝŽÔÄÜĄĹĽŔ"
_DIA_DST = "acdeeinorstuuyzoauallrACDEEINORSTUUYZOAUALLR"
_DIA_MAP = str.maketrans(_DIA_SRC, _DIA_DST)
_DIA_SET = set(_DIA_SRC)

# Latin-1/Czech letters (folded, i.e. lowercase ASCII + the legit CZ/SK
# accented set). Anything else >= U+0080 in a folded name is mojibake debris
# (Ă, Ĺ, ™, ˝ …) — the UTF-8-read-as-cp1250 look.
_LEGIT_LOWER = set("abcdefghijklmnopqrstuvwxyz0123456789 -" + _DIA_SRC.lower())

# Front-of-name academic titles (compared with dots stripped, lowercased).
# Conservative on purpose: a rare "Dr" surname would only lose its title, and
# every change is review-gated anyway.
_TITLE_TOKENS = {
	"ing", "mgr", "judr", "júdr", "phdr", "phd", "doc", "prof", "mvdr",
	"paeddr", "rndr", "csc", "drsc", "dsc", "mudr", "arch", "bc", "dr",
	"ingarch", "mga",
}

# Word tokens that join two author names into one string ("Wilhelm a Jacob
# Grimmové") — those are multi-author records, C7/C8 territory, not variants.
_CONNECTOR_TOKENS = {"a", "&", "and", "et", "y", "i"}

_ANONYM_CANONICAL = "Neznámý"


def _looks_like_neznamy(raw: str) -> bool:
	"""Mojibake/typo forms of "neznámý" ("NeznÁvmÁ" from the real library).

	The anonym spelling set in detectors covers exact spellings only; without
	this check the mangled copies form their own cluster and would canonise
	ANOTHER mangled spelling instead of joining the Neznámý family.
	"""
	folded = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", _nfc(raw)).casefold().translate(_DIA_MAP)).strip()
	if not 6 <= len(folded) <= 12:
		return False
	return Levenshtein.distance(folded, "neznamy") <= 2

# Fuzzy gates (rapidfuzz, 0-100). The house thresholds in enrichers are
# 70/80/90 (candidate floor / author agreement / near-exact); here 80 = "same
# name, different spelling" on a single token (Frederik vs Frederick ≈ 94,
# Miloslav vs Miroslav ≈ 87) and 40 is the surname-matched garbled floor
# (mojibake first names score all over the place, but a folded SURNAME match
# is strong evidence on its own). Genres get NO fuzzy tier at all: on the
# real library's 1621-name Czech vocabulary, distance-2 "typos" were ~50%
# false pairs (Afrika→Amerika, vlaky→války, etika→erotika) — a poor review-
# noise-to-value trade. Misspellings join GENRE_ALIASES as explicit rows
# instead ("mumour"), matching the curated-synonyms policy the user chose.
_GIVEN_FUZZY = 80
_GIVEN_GARBLED_FUZZY = 40


def _nfc(s: str) -> str:
	return unicodedata.normalize("NFC", s)


def _has_diacritics(s: str) -> bool:
	return any(c in _DIA_SET for c in s)


def _fold_word(s: str) -> str:
	"""Lowercase + strip Czech/Slovak diacritics from a single word."""
	return s.strip(".,;:!()'\"").translate(_DIA_MAP).lower()


def _cz_sort_key(s: str) -> str:
	"""Casefolded, diacritics-folded sort key (Ú under U, not after Z).

	The CLI cluster tables (C15 authors / C16 genres+tags / C18 series) are
	LOOKUP aids — the user scans them for a known name, so they sort
	alphabetically, not by cluster size."""
	return _nfc(s).casefold().translate(_DIA_MAP)


def _looks_garbled(folded_tokens: list[str]) -> bool:
	"""True when any folded token carries non-CZ/SK high bytes (mojibake)."""
	return any(any(ord(c) >= 0x80 and c not in _LEGIT_LOWER for c in t) for t in folded_tokens)


def _split_initials(token: str) -> list[str]:
	"""Split dotted/hyphenated initials: "G." → ["g"], "G.-J."/"G.J." →
	["g","j"], "R.R." → ["r","r"].

	Multi-letter parts mean the token is a hyphenated NAME ("Smith-Jones"),
	not an initial cluster — keep it whole so the surname survives.
	"""
	parts = [p for p in re.split(r"[.\-–]", token) if p]
	if parts and all(len(p) == 1 for p in parts) and (len(parts) > 1 or token.endswith(".")):
		return [p.lower() for p in parts]
	return [_fold_word(token)]


# ---------------------------------------------------------------------------
# Author clustering
# ---------------------------------------------------------------------------


@dataclass
class _AuthorForm:
	"""One distinct author string, parsed for clustering."""

	raw: str
	count: int = 0
	uuids: list[str] = field(default_factory=list)
	key: str = ""  # fold key, given-first order ("peter james")
	tokens: list[str] = field(default_factory=list)  # folded tokens in key order
	comma: bool = False  # raw used the Calibre "Surname, Given" convention
	had_title: bool = False
	upper: bool = False  # raw is ALL-CAPS
	garbled: bool = False
	# True when this form joined its cluster only through the FUZZY tier
	# (letter variant / mojibake) — downgrades the cluster to MEDIUM (pending
	# proposal) because same-letter name pairs can still be two people.
	fuzzy: bool = False

	@property
	def surname(self) -> str:
		return self.tokens[-1] if self.tokens else ""

	@property
	def given(self) -> list[str]:
		return self.tokens[:-1]


def _parse_author(raw: str) -> _AuthorForm | None:
	"""Parse one author string; None = not a single-person name (skip).

	Multi-author strings ("Wilhelm a Jacob Grimmové", "byKathy…", digit
	bearers) are left to C7/C8 — mis-clustering them would merge two people.
	"""
	s = _nfc(raw).strip()
	if not s or len(s) > 80:
		return None
	if s.count(",") > 1:
		return None
	comma = "," in s
	if comma:
		# Calibre "Surname, Given" → given-first key order.
		last, given = s.split(",", 1)
		if not last.strip() or not given.strip():
			return None
		s = f"{given.strip()} {last.strip()}"
	words = s.split()
	# Multi-author gate: a connector WORD between names ("Wilhelm a Jacob
	# Grimmové"). Dotted single letters are INITIALS, not the Czech "a" —
	# "Robert A. Heinlein" must survive this check.
	for w in words[1:-1]:
		bare = w.strip(".,;:!")
		if w == "&" or bare in ("and", "et"):
			return None
		if bare in _CONNECTOR_TOKENS and not w.endswith("."):
			return None
	# Strip leading academic titles ("Ing.", "Dr.") — they are not names.
	i = 0
	while i < len(words) - 1 and re.sub(r"[.\-–]", "", words[i]).lower() in _TITLE_TOKENS:
		i += 1
	had_title = i > 0
	words = words[i:]
	tokens: list[str] = []
	for w in words:
		tokens.extend(_split_initials(w))
	if not tokens or len(tokens) > 6:
		return None
	if any(re.search(r"\d", t) for t in tokens):
		return None
	# A bare single token with no letters at all (e.g. "6-Harry Potter") is
	# already rejected above; also drop strings whose folding produced noise.
	flat = " ".join(tokens)
	if not any(c.isalpha() for c in flat):
		return None
	return _AuthorForm(
		raw=raw,
		key=flat,
		tokens=tokens,
		comma=comma,
		had_title=had_title,
		upper=s == s.upper() and any(c.isalpha() for c in s),
		garbled=_looks_garbled(tokens),
	)


def _given_compat(a: list[str], b: list[str]) -> bool:
	"""Deterministic given-name compatibility: pairwise equal tokens or
	initial↔full expansion ("j" ~ "jan"), tolerating up to 2 TRAILING
	leftovers (omitted middle names at the end: "robert" ~ "robert anson").

	In-position skipping is deliberately FORBIDDEN — it let "kevin j" match
	"poul" by stepping over "kevin", false-merging Poul Anderson with Kevin
	J. Anderson in the real library. A genuinely omitted MIDDLE name still
	passes because the shared prefix aligns and the omission lands in the
	trailing leftovers.
	"""
	i = j = 0
	while i < len(a) and j < len(b):
		x, y = a[i], b[j]
		if x == y or (len(x) == 1 and y.startswith(x)) or (len(y) == 1 and x.startswith(y)):
			i += 1
			j += 1
		else:
			return False
	return (len(a) - i) + (len(b) - j) <= 2


@dataclass
class AuthorCluster:
	"""One person: all library spellings + the chosen canonical form."""

	canonical: str
	kind: str  # "variant" | "swapped" | "comma" | "anonym" | "title"
	confidence: Confidence
	reason: str
	variants: list[tuple[str, int]] = field(default_factory=list)  # (raw, book count)


def _swap_key(tokens: list[str]) -> str:
	"""Fold key of the same name written surname-first ("Podroužek Přemysl")."""
	return " ".join([tokens[-1], *tokens[:-1]]) if len(tokens) > 1 else " ".join(tokens)


def _synthesize_expansion(raw: str) -> str:
	"""Display form of a Calibre "Surname, Given" string: "James, Peter" →
	"Peter James" (title-cased exactly as the parts were written)."""
	last, given = _nfc(raw).split(",", 1)
	return f"{given.strip()} {last.strip()}"


class _Union:
	"""Index-based union-find (members are unhashable dataclasses)."""

	def __init__(self, n: int) -> None:
		self.parent = list(range(n))

	def find(self, i: int) -> int:
		while self.parent[i] != i:
			self.parent[i] = self.parent[self.parent[i]]  # path halving
			i = self.parent[i]
		return i

	def union(self, a: int, b: int) -> None:
		ra, rb = self.find(a), self.find(b)
		if ra != rb:
			self.parent[ra] = rb


def build_author_clusters(
	counter: Counter[str], books_by_author: dict[str, list[str]]
) -> tuple[dict[str, AuthorCluster], list[tuple[str, int]]]:
	"""Cluster distinct author strings → {raw: cluster} + skipped multi-author.

	Only clusters that actually CHANGE something are returned (a lone
	canonical spelling maps to nothing).
	"""
	multi: list[tuple[str, int]] = []
	forms: list[_AuthorForm] = []
	anonym_raws: list[str] = []
	for raw, n in counter.items():
		if _is_anonym_spelling(_nfc(raw).strip()) or _looks_like_neznamy(raw):
			anonym_raws.append(raw)
			continue
		f = _parse_author(raw)
		if f is None:
			multi.append((raw, n))
			continue
		f.count = n
		f.uuids = books_by_author.get(raw, [])
		forms.append(f)

	clusters: dict[str, AuthorCluster] = {}

	if len(anonym_raws) > 1 or (anonym_raws and _ANONYM_CANONICAL not in anonym_raws):
		variants = sorted(((r, counter[r]) for r in anonym_raws), key=lambda v: -v[1])
		reason = f"anonymous-author spelling unified to '{_ANONYM_CANONICAL}'"
		cluster = AuthorCluster(_ANONYM_CANONICAL, "anonym", Confidence.HIGH, reason, variants)
		for raw, _ in variants:
			if raw != _ANONYM_CANONICAL:
				clusters[raw] = cluster

	if forms:
		uf = _Union(len(forms))
		by_key: dict[str, int] = {}
		for idx, f in enumerate(forms):
			by_key.setdefault(f.key, idx)
		# (a) exact fold equality
		key_groups: dict[str, list[int]] = defaultdict(list)
		for idx, f in enumerate(forms):
			key_groups[f.key].append(idx)
		for group in key_groups.values():
			for idx in group[1:]:
				uf.union(group[0], idx)
		# (b) swap pairs: one string's key is another's surname-first key
		for idx, f in enumerate(forms):
			partner = by_key.get(_swap_key(f.tokens))
			if partner is not None and partner != idx and f.key != forms[partner].key:
				uf.union(idx, partner)
		# (c) same surname + compatible/fuzzy given names
		by_surname: dict[str, list[int]] = defaultdict(list)
		for idx, f in enumerate(forms):
			by_surname[f.surname].append(idx)
		for group in by_surname.values():
			for i, i1 in enumerate(group):
				for i2 in group[i + 1 :]:
					f1, f2 = forms[i1], forms[i2]
					compat = _given_compat(f1.given, f2.given)
					fuzzy = False
					if not compat:
						score = fuzz.token_sort_ratio(" ".join(f1.given), " ".join(f2.given))
						floor = _GIVEN_GARBLED_FUZZY if (f1.garbled or f2.garbled) else _GIVEN_FUZZY
						compat = score >= floor
						fuzzy = True
					if compat:
						if fuzzy:
							f1.fuzzy = f2.fuzzy = True
						uf.union(i1, i2)

		sets: dict[int, list[_AuthorForm]] = defaultdict(list)
		for idx, f in enumerate(forms):
			sets[uf.find(idx)].append(f)

		for members in sets.values():
			cluster = _finalize_author_cluster(members, counter)
			if cluster is not None:
				for raw, _ in cluster.variants:
					if raw != cluster.canonical:
						clusters[raw] = cluster
	return clusters, multi


def _pick_canonical(members: list[_AuthorForm]) -> tuple[str, list[_AuthorForm]]:
	"""Choose the canonical display string: most frequent NON-degraded member.

	Degraded = a better spelling of the same person exists: ALL-CAPS, ASCII-
	folded while a diacritic spelling is present, mojibake, leading title, or
	the comma convention. "Neznamy" x63 therefore loses to "Neznámý" x24 —
	frequency alone would canonise the mangled form.
	"""
	has_dia = any(_has_diacritics(m.raw) for m in members)
	clean = [
		m for m in members
		if not m.upper and not m.garbled and not m.had_title and not m.comma
		and not (has_dia and not _has_diacritics(m.raw))
	]
	pool = clean or members
	best = max(pool, key=lambda m: (m.count, m.raw))
	return best.raw, [m for m in members if m is not best]


def _finalize_author_cluster(
	members: list[_AuthorForm], counter: Counter[str]
) -> AuthorCluster | None:
	"""Turn a union set into an AuthorCluster (None when nothing changes)."""
	# Order majority: count-weighted token order of the KEY forms. Members in
	# the minority order are the swapped spellings.
	order_counts: Counter[str] = Counter()
	for m in members:
		order_counts[m.key] += m.count
	majority_key = max(order_counts, key=lambda k: (order_counts[k], k))
	majority_members = [m for m in members if m.key == majority_key]
	comma_only = all(m.comma for m in members)

	if comma_only and len(members) == 1:
		# Isolated "James, Peter" with no plain-order twin in the library:
		# propose the mechanical expansion but keep it pending — "Weis,
		# Hickman" may equally be two authors joined by a comma.
		m = members[0]
		canonical = _synthesize_expansion(m.raw)
		return AuthorCluster(
			canonical, "comma", Confidence.MEDIUM,
			f"comma name order '{m.raw}' → '{canonical}' (no library evidence it is one person)",
			[(m.raw, m.count)],
		)

	canonical, others = _pick_canonical(members)
	if not others:
		return None

	# Confidence: HIGH when the merge is deterministic AND the cluster's
	# order/spelling is attested; MEDIUM whenever a fuzzy-tier member (letter
	# variant, mojibake) or an unevidenced reorder is involved — those stay
	# pending for the human, because same-letter name pairs can be two people.
	majority_total = sum(m.count for m in majority_members)
	evidenced = bool(majority_members) and (majority_total >= 3 or len(majority_members) >= 2)
	if any(m.fuzzy for m in others) or not evidenced:
		confidence = Confidence.MEDIUM
	else:
		confidence = Confidence.HIGH

	# Dominant failure mode of the cluster: what the non-canonical members
	# are doing wrong, for the reason line.
	kinds = set()
	for m in others:
		if m.key != majority_key and _swap_key(m.tokens) == majority_key:
			kinds.add("swapped")
		elif m.comma:
			kinds.add("comma")
		elif m.had_title:
			kinds.add("title")
		else:
			kinds.add("variant")
	kind = kinds.pop() if len(kinds) == 1 else "variant"
	variants = [(m.raw, m.count) for m in sorted(members, key=lambda m: -m.count)]
	if kind == "swapped":
		reason = f"author name order swapped, unified to '{canonical}'"
	elif kind == "comma":
		reason = f"comma name order unified to '{canonical}'"
	else:
		n_books = sum(c for _, c in variants)
		reason = f"author variant unified to '{canonical}' ({len(variants)} spellings, {n_books} books)"
	return AuthorCluster(canonical, kind, confidence, reason, variants)


# ---------------------------------------------------------------------------
# Known-author pool (C1 swap detection)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KnownAuthorPool:
	"""Library-wide author index: "is this string a known library author?"

	Built once per analyze run over the whole scan and handed to the C1
	detector/swap-repair tier. A book whose TITLE field parses as a single
	person name that the library knows as an author is near-certainly a
	swapped/lost-title record — no per-book heuristic can see that, only the
	library-wide pool can ("Anatolij Dněprov" as a title is invisible to C1's
	patterns, but the pool knows Dněprov is an author with N books).

	Keys are the same fold keys the clustering uses (diacritics-stripped,
	lowercased, given-first; the surname-first swap key is indexed too), so a
	title matches the author regardless of spelling variant. The anonym
	family is excluded — a title "Neznámý" must never match an author.
	"""

	# fold key (given-first or surname-first) → canonical display string
	_canonical: dict[str, str]
	# canonical display string → book count across the library
	_counts: dict[str, int]

	def lookup(self, name: str) -> tuple[str, int] | None:
		"""Resolve a name/title to ``(canonical, book_count)``; None = unknown.

		Returns None for strings that are not a single person name (the
		``_parse_author`` gate rejects multi-author chains, digits, long
		sentences) — a title that is not name-shaped cannot be a swapped
		author even when it coincides with some string in the library.
		"""
		f = _parse_author(name)
		if f is None:
			return None
		canon = self._canonical.get(f.key)
		if canon is None and len(f.tokens) > 1:
			canon = self._canonical.get(_swap_key(f.tokens))
		if canon is None:
			return None
		return canon, self._counts.get(canon, 0)

	def same_person(self, a: str, b: str) -> bool:
		"""True when both strings resolve to the same pool person (variants incl.)."""
		la = self.lookup(a)
		lb = self.lookup(b)
		return la is not None and lb is not None and la[0] == lb[0]


def build_known_author_pool(books: list[BookMeta]) -> KnownAuthorPool:
	"""Index every author in the scanned library into a KnownAuthorPool.

	Reuse of the C15 machinery: variant spellings are unioned by
	``build_author_clusters`` so a lookup under ANY spelling lands on the
	cluster canonical, and lone spellings (no cluster — nothing to change)
	are indexed under their own fold key. Anonym spellings never enter the
	pool. Cluster canonicals carry the cluster's total book count, so the
	detector can say "known library author (N books)".
	"""
	counter: Counter[str] = Counter()
	for b in books:
		for a in b.authors or []:
			counter[a] += 1
	clusters, _multi = build_author_clusters(counter, defaultdict(list))

	canonical: dict[str, str] = {}
	counts: dict[str, int] = {}
	clustered_raws: set[str] = set()
	for cluster in {id(c): c for c in clusters.values()}.values():
		if cluster.kind == "anonym":
			continue  # "Neznámý" & spellings are not persons — a title must never match them
		counts[cluster.canonical] = sum(c for _, c in cluster.variants)
		for raw, _n in cluster.variants:
			clustered_raws.add(raw)
			f = _parse_author(raw)
			if f is None:
				continue
			canonical.setdefault(f.key, cluster.canonical)
			if len(f.tokens) > 1:
				canonical.setdefault(_swap_key(f.tokens), cluster.canonical)
	for raw, n in counter.items():
		if raw in clustered_raws:
			continue
		if _is_anonym_spelling(_nfc(raw).strip()) or _looks_like_neznamy(raw):
			continue
		f = _parse_author(raw)
		if f is None:
			continue  # multi-author/garbage strings are not pool persons
		canonical.setdefault(f.key, raw)
		if len(f.tokens) > 1:
			canonical.setdefault(_swap_key(f.tokens), raw)
		counts.setdefault(raw, n)
	return KnownAuthorPool(canonical, counts)


# ---------------------------------------------------------------------------
# Genre / tag canonicalization
# ---------------------------------------------------------------------------

# Curated alias table: human-readable alias → canonical STRING. Lookup runs
# through fold_genre, and targets resolve through the library's own fold
# groups (see build_genre_clusters), so "Comedy" and "humor" both land on
# whichever spelling of Humor the library itself uses most. Seeded from the
# measured distinct genres of the real library; extend freely — every row is
# an explicit, reviewable merge (this table IS the agreed synonym policy).
GENRE_ALIASES: dict[str, str] = {
	# English → Czech
	"science fiction": "Sci-fi",
	"comedy": "Humor",
	"humour": "Humor",
	"horror": "Horory",
	"thriller": "Thrillery",
	"thrillers": "Thrillery",
	"adventure": "Dobrodružné",
	"parodies": "parodie",
	"mystery": "Detektivky, krimi",
	"history": "Historie",
	"france": "Francie",
	"young adult": "pro dospívající mládež (young adult)",
	# Czech morphological variants
	"román": "Romány",
	"horor": "Horory",
	"detektivka": "Detektivky, krimi",
	"krimi": "Detektivky, krimi",
	"dobrodružný": "Dobrodružné",
	"dobrodružství": "Dobrodružné",
	"historický román": "Historické romány",
	"dramata": "drama",
	"novela": "Novely",
	"pohádka": "pohádky, báchorky",
	"westerny": "western",
	"pro děti": "Pro děti a mládež",
	"pro mládež": "Pro děti a mládež",
	"pohádky a bajky": "pohádky, báchorky",
	# Misspellings observed in the real library (the former fuzzy tier —
	# kept as explicit rows: distance-2 auto-matching was ~50% wrong on the
	# Czech vocabulary)
	"mumour": "Humor",
	"humorná": "Humor",
}


def fold_genre(s: str) -> str:
	"""Fold key: NFC + casefold + diacritics out + punctuation to spaces +
	token-sorted (word order does not distinguish genres: "česká literatura"
	≡ "Literatura česká")."""
	s = _nfc(s).casefold().translate(_DIA_MAP)
	tokens = sorted(re.sub(r"[^\w\s]", " ", s).split())
	return " ".join(tokens)


@dataclass
class GenreCluster:
	"""One canonical genre name and the spellings that map to it."""

	canonical: str
	kind: str  # "fold" (case/diacritics/word-order duplicates) | "alias" (table)
	confidence: Confidence
	reason: str
	variants: list[tuple[str, int]] = field(default_factory=list)


def build_genre_clusters(
	counter: Counter[str], aliases: dict[str, str] | None = None
) -> dict[str, GenreCluster]:
	"""Map every distinct genre string → its canonical cluster (changed only).

	Two deterministic mechanisms only — fold duplicates and the curated
	alias table. There is deliberately NO fuzzy tier (see the _GIVEN_FUZZY
	comment): misspellings become alias rows.
	"""
	aliases = GENRE_ALIASES if aliases is None else aliases
	groups: dict[str, list[tuple[str, int]]] = defaultdict(list)
	for raw, n in counter.items():
		groups[fold_genre(raw)].append((raw, n))
	# Representative spelling per fold group = most frequent original form —
	# the library's own dominant casing ("Sci-fi", "humor") wins.
	reps: dict[str, str] = {}
	for key, items in groups.items():
		reps[key] = max(items, key=lambda it: (it[1], it[0]))[0]

	def _resolve(target: str) -> str:
		"""Alias target → the fold-group representative spelling, so table
		rows and fold duplicates converge on ONE string."""
		return reps.get(fold_genre(target), target)

	clusters: dict[str, GenreCluster] = {}

	def _register(key: str, cluster: GenreCluster) -> None:
		for raw, _ in groups.get(key, []):
			if raw != cluster.canonical:
				clusters[raw] = cluster

	# 1) fold groups with >1 spelling
	for key, items in groups.items():
		if len(items) > 1:
			canonical = reps[key]
			n_books = sum(c for _, c in items)
			cluster = GenreCluster(
				canonical, "fold", Confidence.HIGH,
				f"genre spelling unified to '{canonical}' ({len(items)} spellings, {n_books} books)",
				sorted(items, key=lambda it: -it[1]),
			)
			_register(key, cluster)

	# 2) alias table (each matched row merges its whole fold group). Alias
	# keys are looked up THROUGH fold_genre — the table is human-readable
	# ("science fiction", "dobrodružný"), but group keys are token-sorted and
	# diacritics-stripped ("fiction science", "dobrodruzny").
	for alias, target in aliases.items():
		alias_key = fold_genre(alias)
		canonical = _resolve(target)
		members = list(groups.get(alias_key, []))
		if not members:
			continue
		# Merge into the target's cluster if one exists so variants/reasons
		# accumulate instead of overwriting.
		tkey = fold_genre(target)
		existing = next((c for c in clusters.values() if c.canonical == canonical), None)
		# Union with existing.variants too: several alias rows can target the
		# same canonical one after another, and each rewrite must keep what
		# the previous one merged in.
		variants = sorted(
			set(members) | set(groups.get(tkey, [])) | set(existing.variants if existing else []),
			key=lambda it: -it[1],
		)
		if existing is not None:
			existing.variants = variants
			if len(variants) > 1:
				existing.reason = f"genre unified to '{canonical}' (alias + spelling variants, {len(variants)} forms)"
		else:
			existing = GenreCluster(
				canonical, "alias", Confidence.HIGH,
				f"genre unified to '{canonical}' (alias merge)",
				variants,
			)
		for raw, _ in variants:
			if raw != canonical:
				clusters[raw] = existing
	return clusters


# ---------------------------------------------------------------------------
# Series-name canonicalization (C18)
# ---------------------------------------------------------------------------

# Curated rename table: human-readable alias → canonical STRING, lookup runs
# through fold_series and targets resolve through the library's own fold
# groups (same contract as GENRE_ALIASES). The fold tier already catches
# case/diacritics/punctuation variants ("Zaklínač"/"zaklínač"/"Zaklinac");
# this table is for REAL-WORD renames the fold cannot see — different letters
# ("Perry Rodan" → "Perry Rhodan") or edition suffixes ("Mark Stone (edice)"
# → "Mark Stone"). Unlike genre aliases (HIGH), every row here lands as
# PENDING: a fresh row retitles a whole group at once and the one-time GUI
# confirm is the safety net against a typo in the row itself.
SERIES_ALIASES: dict[str, str] = {
	# The Zeměplocha family (measured 2026-09-10): the Czech name is the
	# dominant spelling (×16); the bare form, the English name and the
	# CZ/SK dual name with the slash are four DIFFERENT fold groups no
	# automatic tier may bridge — real-word renames, alias rows by design.
	"Zeměplocha": "Úžasná Zeměplocha",
	"Discworld": "Úžasná Zeměplocha",
	"Diskworld": "Úžasná Zeměplocha",  # typo of Discworld (k≠c), fold-blind
	"Úžasná Zeměplocha / Úžasná Plochozem": "Úžasná Zeměplocha",
	# The Nomes trilogy: EN name vs the Czech one (same works, CZ wins).
	"Bromeliad Trilogy": "Vyprávění o nomech",
}


def fold_series(s: str) -> str:
	"""Fold key: NFC + casefold + diacritics out + punctuation to spaces,
	whitespace collapsed — word ORDER preserved (unlike genres, close
	sub-series names must not auto-merge: "Legenda o Drizztovi" ≠
	"Drizztova legenda"). A trailing "#N" glued into the name is stripped
	first so a glued spelling lands in the same group as the bare name.
	"""
	bare = s
	split = split_series_index(s)
	if split is not None:
		bare = split[0]
	folded = _nfc(bare).casefold().translate(_DIA_MAP)
	return " ".join(re.sub(r"[^\w\s]", " ", folded).split())


@dataclass
class SeriesSequence:
	"""Volume-numbering overview of one series group (display + evidence).

	Missing volumes are INFORMATION, not a defect — a personal library does
	not own every title; duplicates (two books claiming the same slot) and
	anomalies (index 0, non-numeric) are warnings.
	"""

	present: list[float] = field(default_factory=list)
	missing: list[tuple[int, int]] = field(default_factory=list)  # inclusive int ranges
	duplicates: list[tuple[float, int]] = field(default_factory=list)
	unnumbered: int = 0
	anomalies: list[tuple[str, int]] = field(default_factory=list)


def analyze_sequence(indexes: list[str]) -> SeriesSequence:
	"""Summarize one group's raw series_index strings ("" = unnumbered)."""
	counts: Counter[float] = Counter()
	anomalies: Counter[str] = Counter()
	unnumbered = 0
	for raw in indexes:
		s = (raw or "").strip().replace(",", ".")
		if not s:
			unnumbered += 1
			continue
		try:
			n = float(s)
		except ValueError:
			anomalies[raw] += 1
			continue
		if n <= 0:
			anomalies[raw] += 1
			continue
		counts[n] += 1
	present = sorted(counts)
	# Gaps are counted on whole numbers only ("3.5" is a sub-volume, not a hole).
	ints = sorted({int(n) for n in present if n == int(n)})
	missing = [
		(a + 1, b - 1)
		for a, b in zip(ints, ints[1:], strict=False)
		if b - a > 1
	]
	return SeriesSequence(
		present=present,
		missing=missing,
		duplicates=sorted((n, c) for n, c in counts.items() if c > 1),
		unnumbered=unnumbered,
		anomalies=sorted(anomalies.items()),
	)


def _numeric_volumes(indexes: list[str]) -> set[int]:
	"""Positive integer volume numbers claimed by one name's books."""
	out: set[int] = set()
	for raw in indexes:
		s = (raw or "").strip().replace(",", ".")
		try:
			n = float(s)
		except ValueError:
			continue
		if n > 0 and n == int(n):
			out.add(int(n))
	return out


@dataclass
class SeriesCluster:
	"""One canonical series name and the spellings that map to it."""

	canonical: str
	kind: str  # "fold" | "alias" (table) | "prefix" | "fuzzy" (suspect, evidenced)
	confidence: Confidence
	reason: str
	variants: list[tuple[str, int]] = field(default_factory=list)
	sequence: SeriesSequence | None = None


@dataclass
class SuspectPair:
	"""Two series names suspected of being the same series (advisory).

	Suspects are found by structural proximity (one name is a token prefix
	of the other) or by fuzzy closeness to a VERIFIED series name; the
	verdict weighs the offline evidence (volume numbering) and, when an
	online check is supplied, the names' existence in the bibliographic DB.
	Only "merge" suspects become clusters/proposals — the rest stay here
	for the overview tables.
	"""

	base: str
	suspect: str
	kind: str  # "prefix" | "fuzzy"
	complementary: bool | None = None  # volume sets interleave into one row
	collision: bool | None = None  # both claim the same volume number
	online_base: bool | None = None
	online_suspect: bool | None = None
	verdict: str = "unknown"  # "merge" | "distinct" | "unknown"

	@property
	def evidence(self) -> str:
		parts = []
		if self.complementary:
			parts.append("numbering complementary")
		if self.collision:
			parts.append("numbering collision")
		if self.online_base is not None:
			parts.append(f"online base={self.online_base}")
		if self.online_suspect is not None:
			parts.append(f"online suspect={self.online_suspect}")
		return "; ".join(parts) or "no evidence"


def _prefix_pairs(keys: list[str]) -> list[tuple[str, str]]:
	"""Fold keys that are a whole-token prefix of another fold key."""
	by_tokens = {k: tuple(k.split()) for k in keys}
	pairs: list[tuple[str, str]] = []
	for key, tokens in by_tokens.items():
		for i in range(1, len(tokens)):
			base = " ".join(tokens[:i])
			if base in by_tokens and by_tokens[base] != key:
				pairs.append((base, key))
	return pairs


def build_series_clusters(
	counter: Counter[str],
	*,
	aliases: dict[str, str] | None = None,
	indexes_by_name: dict[str, list[str]] | None = None,
	verified_names: set[str] | None = None,
	online_check=None,
) -> tuple[dict[str, SeriesCluster], list[SuspectPair]]:
	"""Map every distinct series-name string → its canonical cluster (changed only).

	Mirrors build_genre_clusters' deterministic core (fold groups + curated
	table) with two series-specific tiers on top:

	* confidence policy — fold merges are HIGH (pre-filled accept, same
	  letters after folding); alias-table rows and evidenced suspects are
	  MEDIUM (pending — a fresh alias row or a prefix/fuzzy merge retitles
	  a whole group at once and gets one GUI confirm);
	* suspects — token-prefix pairs ("Mark Stone" / "Mark Stone (edice)")
	  and fuzzy closeness to a verified series name, weighed by volume
	  numbering (complementary sets ⇒ one series; a collision ⇒ two series
	  or duplicates) and optionally by ``online_check(name) -> bool``
	  (injected; the engine stays I/O-free). Only positively-evidenced
	  pairs become clusters; every pair is returned for display.

	There is deliberately NO free fuzzy tier (same trade as genres, see
	_GIVEN_FUZZY): close sub-series names would auto-merge; misses become
	alias rows or suspects instead.
	"""
	aliases = SERIES_ALIASES if aliases is None else aliases
	indexes_by_name = indexes_by_name or {}
	verified_names = verified_names or set()
	groups: dict[str, list[tuple[str, int]]] = defaultdict(list)
	for raw, n in counter.items():
		groups[fold_series(raw)].append((raw, n))
	reps: dict[str, str] = {}
	for key, items in groups.items():
		reps[key] = max(items, key=lambda it: (it[1], it[0]))[0]

	def _group_indexes(key: str) -> list[str]:
		out: list[str] = []
		for raw, _n in groups.get(key, []):
			out.extend(indexes_by_name.get(raw, []))
		return out

	clusters: dict[str, SeriesCluster] = {}
	by_canonical: dict[str, SeriesCluster] = {}
	# Fold keys each cluster covers — sequence/variants are recomputed over
	# the WHOLE membership whenever a merge extends an existing cluster.
	cluster_keys: dict[int, set[str]] = defaultdict(set)

	def _register(cluster: SeriesCluster, member_keys: list[str]) -> None:
		all_keys = cluster_keys[id(cluster)] | set(member_keys)
		cluster_keys[id(cluster)] = all_keys
		for key in all_keys:
			for raw, _n in groups.get(key, []):
				if raw != cluster.canonical:
					clusters[raw] = cluster
		cluster.sequence = analyze_sequence(
			[idx for key in all_keys for idx in _group_indexes(key)]
		)

	merged_keys: set[str] = set()  # fold groups consumed by a table/suspect merge

	# 1) fold groups with >1 spelling (deterministic, HIGH)
	for key, items in groups.items():
		if len(items) > 1:
			canonical = reps[key]
			n_books = sum(c for _, c in items)
			cluster = SeriesCluster(
				canonical, "fold", Confidence.HIGH,
				f"series spelling unified to '{canonical}' ({len(items)} spellings, {n_books} books)",
				sorted(items, key=lambda it: -it[1]),
			)
			by_canonical[canonical] = cluster
			_register(cluster, [key])

	# 2) alias table (each matched row merges its whole fold group, MEDIUM)
	def _resolve(target: str) -> str:
		return reps.get(fold_series(target), target)

	for alias, target in aliases.items():
		alias_key = fold_series(alias)
		if alias_key not in groups:
			continue
		canonical = _resolve(target)
		tkey = fold_series(target)
		member_keys = [alias_key] if tkey not in groups else [alias_key, tkey]
		variants = sorted(
			{i for key in member_keys for i in groups.get(key, [])},
			key=lambda it: -it[1],
		)
		existing = by_canonical.get(canonical)
		if existing is not None:
			existing.variants = sorted(set(existing.variants) | set(variants), key=lambda it: -it[1])
			if existing.confidence is Confidence.HIGH:
				existing.confidence = Confidence.MEDIUM
			if existing.kind == "fold":
				existing.kind = "alias"
			existing.reason = f"series unified to '{canonical}' (alias + spelling variants)"
			_register(existing, member_keys)
		else:
			cluster = SeriesCluster(
				canonical, "alias", Confidence.MEDIUM,
				f"series unified to '{canonical}' (alias merge)",
				variants,
			)
			by_canonical[canonical] = cluster
			_register(cluster, member_keys)
		merged_keys.add(alias_key)
		if tkey in groups:
			merged_keys.add(tkey)

	# 3) suspects — token-prefix pairs + fuzzy closeness to a verified name.
	pair_index: dict[tuple[str, str], str] = {}
	for base_key, sus_key in _prefix_pairs(list(groups)):
		if base_key in merged_keys or sus_key in merged_keys:
			continue
		pair_index[(base_key, sus_key)] = "prefix"
	if verified_names:
		verified_folded = {fold_series(v) for v in verified_names}
		for key in groups:
			if key in merged_keys or key in verified_folded:
				continue
			for vkey in verified_folded:
				if vkey == key or (key, vkey) in pair_index or (vkey, key) in pair_index:
					continue
				if fuzz.token_set_ratio(key, vkey) >= 80:
					# The verified name is the base — it is the trusted anchor.
					pair_index.setdefault((vkey, key), "fuzzy")

	suspects: list[SuspectPair] = []
	for (base_key, sus_key), kind in sorted(pair_index.items()):
		base, sus = reps[base_key], reps[sus_key]
		na = _numeric_volumes(_group_indexes(base_key))
		nb = _numeric_volumes(_group_indexes(sus_key))
		complementary: bool | None = None
		collision: bool | None = None
		if na and nb:
			collision = bool(na & nb)
			union = na | nb
			complementary = not collision and union == set(range(min(union), max(union) + 1))
		online_base = online_suspect = None
		if online_check is not None:
			online_base = bool(online_check(base))
			online_suspect = bool(online_check(sus))
		verdict = "unknown"
		if collision:
			verdict = "distinct"  # two series (or duplicate books) — never merge
		elif complementary:
			verdict = "merge"
		elif online_base is not None:
			if online_base and not online_suspect:
				verdict = "merge"  # the base exists online, the suspect does not
			elif online_base and online_suspect:
				verdict = "distinct"
		suspects.append(SuspectPair(base, sus, kind, complementary, collision, online_base, online_suspect, verdict))
		if verdict != "merge":
			continue
		# A "merge" suspect joins the base group's cluster (extending fold
		# variants if one exists) as MEDIUM — pending, one GUI confirm.
		existing = by_canonical.get(base)
		if existing is not None:
			existing.variants = sorted(
				set(existing.variants) | {i for i in groups[sus_key]},
				key=lambda it: -it[1],
			)
			if existing.confidence is Confidence.HIGH:
				existing.confidence = Confidence.MEDIUM
			existing.reason = f"series merged with '{base}' (suspected same series, numbering/online evidence)"
			_register(existing, [sus_key])
		else:
			cluster = SeriesCluster(
				base, kind, Confidence.MEDIUM,
				f"series merged with '{base}' (suspected same series, numbering/online evidence)",
				sorted({*groups[base_key], *groups[sus_key]}, key=lambda it: -it[1]),
			)
			by_canonical[base] = cluster
			_register(cluster, [sus_key])
	return clusters, suspects


@dataclass
class SeriesGroup:
	"""One fold group as it exists ON DISK today — the honest overview unit
	for `bmf series` (proposals annotate this view, they do not shape it)."""

	canonical: str  # most frequent original spelling in the group
	books: int
	variants: list[tuple[str, int]] = field(default_factory=list)
	sequence: SeriesSequence = field(default_factory=SeriesSequence)


# ---------------------------------------------------------------------------
# Library pass
# ---------------------------------------------------------------------------


@dataclass
class BookProposal:
	"""Proposed list replacements for one book (uuid-keyed, review-gated)."""

	uuid: str | None
	path: str
	calibre_id: int | None = None
	authors: list[str] | None = None
	genres: list[str] | None = None
	tags: list[str] | None = None
	# C18: canonical series NAME only — the book's series index is never
	# proposed (_apply_fields keeps the current half of the pair).
	series: str | None = None
	# Per-diagnosis reason pools (C15 authors / C16 genres+tags / C18 series);
	# `reasons` is their union for flat CLI display.
	author_reasons: list[str] = field(default_factory=list)
	genre_reasons: list[str] = field(default_factory=list)
	series_reasons: list[str] = field(default_factory=list)
	high_confidence: bool = True

	@property
	def reasons(self) -> list[str]:
		return [*self.author_reasons, *self.genre_reasons, *self.series_reasons]

	@property
	def categories(self) -> list[str]:
		cats = []
		if self.authors is not None:
			cats.append("C15")
		if self.genres is not None or self.tags is not None:
			cats.append("C16")
		if self.series is not None:
			cats.append("C18")
		return cats


@dataclass
class NormalizeResult:
	author_clusters: list[AuthorCluster] = field(default_factory=list)
	genre_clusters: list[GenreCluster] = field(default_factory=list)
	tag_clusters: list[GenreCluster] = field(default_factory=list)
	series_clusters: list[SeriesCluster] = field(default_factory=list)
	series_suspects: list[SuspectPair] = field(default_factory=list)
	series_overview: list[SeriesGroup] = field(default_factory=list)
	multi_author: list[tuple[str, int]] = field(default_factory=list)
	# C18 skips: books whose series order is glued into the NAME (C14's
	# split already pre-fills accept for them) and books listing more than
	# one series (applying a single-name proposal would drop the rest).
	glued_series: list[tuple[str, int]] = field(default_factory=list)
	multi_series: list[tuple[str, int]] = field(default_factory=list)
	proposals: list[BookProposal] = field(default_factory=list)


def _dedupe(items: list[str]) -> list[str]:
	return list(dict.fromkeys(items))


def analyze_library(
	books: list[BookMeta],
	*,
	fields: tuple[str, ...] = ("authors", "genres", "tags", "series"),
	series_online_check=None,
) -> NormalizeResult:
	"""Cluster authors/genres/series across the library and build per-book proposals."""
	result = NormalizeResult()
	author_map: dict[str, AuthorCluster] = {}
	gmap: dict[str, GenreCluster] = {}
	tmap: dict[str, GenreCluster] = {}
	smap: dict[str, SeriesCluster] = {}

	if "authors" in fields:
		counter: Counter[str] = Counter()
		books_by_author: dict[str, list[str]] = defaultdict(list)
		for b in books:
			for a in b.authors or []:
				counter[a] += 1
				books_by_author[a].append(b.uuid or b.path)
		author_map, multi = build_author_clusters(counter, books_by_author)
		result.multi_author = multi
		seen: dict[int, AuthorCluster] = {}
		for cluster in author_map.values():
			seen.setdefault(id(cluster), cluster)
		result.author_clusters = sorted(seen.values(), key=lambda c: _cz_sort_key(c.canonical))

	if "genres" in fields or "tags" in fields:
		gcounter: Counter[str] = Counter()
		tcounter: Counter[str] = Counter()
		for b in books:
			for g in b.genres or []:
				gcounter[g] += 1
			for t in b.tags or []:
				tcounter[t] += 1
		# Genres and tags share the dc:subject space (writers merge both into
		# one OPF), so they are canonicalized against ONE combined vocabulary —
		# otherwise "Science Fiction" in tags could land on a different casing
		# of Sci-fi than the same word in genres.
		combined: Counter[str] = Counter()
		if "genres" in fields:
			combined.update(gcounter)
		if "tags" in fields:
			combined.update(tcounter)
		gmap = build_genre_clusters(combined)
		tmap = gmap
		gseen: dict[int, GenreCluster] = {}
		for c in gmap.values():
			gseen.setdefault(id(c), c)
		result.genre_clusters = sorted(gseen.values(), key=lambda c: _cz_sort_key(c.canonical))
		result.tag_clusters = result.genre_clusters

	if "series" in fields:
		scounter: Counter[str] = Counter()
		indexes_by_name: dict[str, list[str]] = defaultdict(list)
		verified_names: set[str] = set()
		glued: Counter[str] = Counter()
		multi: Counter[str] = Counter()
		for b in books:
			entries = b.series or []
			if len(entries) > 1:
				# The apply path writes ONE {name, index} pair per proposal —
				# renaming here would silently drop the other series.
				for s in {series_entry_pair(s_)[0] for s_ in entries if series_entry_pair(s_)[0]}:
					multi[s] += 1
				continue
			name, idx = b.series_pair()
			if not name:
				continue
			if split_series_index(name) is not None:
				# Order glued into the name — C14's split owns these books.
				glued[name] += 1
				continue
			scounter[name] += 1
			indexes_by_name[name].append(idx)
			if b.verified:
				verified_names.add(name)
		smap, suspects = build_series_clusters(
			scounter,
			indexes_by_name=indexes_by_name,
			verified_names=verified_names,
			online_check=series_online_check,
		)
		result.series_suspects = suspects
		result.glued_series = sorted(glued.items(), key=lambda it: -it[1])
		result.multi_series = sorted(multi.items(), key=lambda it: -it[1])
		sseen: dict[int, SeriesCluster] = {}
		for c in smap.values():
			sseen.setdefault(id(c), c)
		result.series_clusters = sorted(sseen.values(), key=lambda c: _cz_sort_key(c.canonical))
		# The overview mirrors the DISK state (plain fold groups, alias/suspect
		# merges are only proposals) — `bmf series` annotates, never presumes.
		overview: dict[str, list[tuple[str, int]]] = defaultdict(list)
		for raw, n in scounter.items():
			overview[fold_series(raw)].append((raw, n))
		result.series_overview = sorted(
			(
				SeriesGroup(
					canonical=max(items, key=lambda it: (it[1], it[0]))[0],
					books=sum(n for _, n in items),
					variants=sorted(items, key=lambda it: -it[1]),
					sequence=analyze_sequence(
						[idx for raw, _n in items for idx in indexes_by_name.get(raw, [])]
					),
				)
				for items in overview.values()
			),
			key=lambda g: (-g.books, g.canonical.lower()),
		)

	for b in books:
		proposal = BookProposal(uuid=b.uuid, path=b.path, calibre_id=b.calibre_id)
		high = True
		if "authors" in fields and (b.authors or []):
			new_authors = _dedupe([author_map[a].canonical if a in author_map else a for a in b.authors])
			if new_authors != b.authors:
				proposal.authors = new_authors
				for a in b.authors:
					c = author_map.get(a)
					if c is not None:
						proposal.author_reasons.append(f"{a} → {c.canonical} ({c.reason})")
						if c.confidence is not Confidence.HIGH:
							high = False
		for field_name, current, mapping in (
			("genres", b.genres, gmap),
			("tags", b.tags, tmap),
		):
			if field_name not in fields or not current:
				continue
			new_list = _dedupe([mapping[v].canonical if v in mapping else v for v in current])
			if new_list != current:
				setattr(proposal, field_name, new_list)
				for v in current:
					c = mapping.get(v)
					if c is not None:
						proposal.genre_reasons.append(f"{field_name[:-1]} '{v}' → '{c.canonical}'")
						if c.confidence is not Confidence.HIGH:
							high = False
		if "series" in fields and len(b.series or []) == 1:
			name, _idx = b.series_pair()
			if name and split_series_index(name) is None:
				c = smap.get(name)
				if c is not None:
					proposal.series = c.canonical
					proposal.series_reasons.append(f"series '{name}' → '{c.canonical}' ({c.reason})")
					if c.confidence is not Confidence.HIGH:
						high = False
		proposal.author_reasons = list(dict.fromkeys(proposal.author_reasons))
		proposal.genre_reasons = list(dict.fromkeys(proposal.genre_reasons))
		proposal.series_reasons = list(dict.fromkeys(proposal.series_reasons))
		proposal.high_confidence = high
		if proposal.categories:
			result.proposals.append(proposal)
	return result
