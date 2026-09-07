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

from .detectors import _is_anonym_spelling
from .models import BookMeta, Confidence

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
	# Per-diagnosis reason pools (C15 authors / C16 genres+tags); `reasons` is
	# their union for flat CLI display.
	author_reasons: list[str] = field(default_factory=list)
	genre_reasons: list[str] = field(default_factory=list)
	high_confidence: bool = True

	@property
	def reasons(self) -> list[str]:
		return [*self.author_reasons, *self.genre_reasons]

	@property
	def categories(self) -> list[str]:
		cats = []
		if self.authors is not None:
			cats.append("C15")
		if self.genres is not None or self.tags is not None:
			cats.append("C16")
		return cats


@dataclass
class NormalizeResult:
	author_clusters: list[AuthorCluster] = field(default_factory=list)
	genre_clusters: list[GenreCluster] = field(default_factory=list)
	tag_clusters: list[GenreCluster] = field(default_factory=list)
	multi_author: list[tuple[str, int]] = field(default_factory=list)
	proposals: list[BookProposal] = field(default_factory=list)


def _dedupe(items: list[str]) -> list[str]:
	return list(dict.fromkeys(items))


def analyze_library(
	books: list[BookMeta], *, fields: tuple[str, ...] = ("authors", "genres", "tags")
) -> NormalizeResult:
	"""Cluster authors/genres across the library and build per-book proposals."""
	result = NormalizeResult()
	author_map: dict[str, AuthorCluster] = {}
	gmap: dict[str, GenreCluster] = {}
	tmap: dict[str, GenreCluster] = {}

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
		result.author_clusters = sorted(seen.values(), key=lambda c: -sum(n for _, n in c.variants))

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
		result.genre_clusters = sorted(gseen.values(), key=lambda c: -sum(n for _, n in c.variants))
		result.tag_clusters = sorted(gseen.values(), key=lambda c: -sum(n for _, n in c.variants))

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
		proposal.author_reasons = list(dict.fromkeys(proposal.author_reasons))
		proposal.genre_reasons = list(dict.fromkeys(proposal.genre_reasons))
		proposal.high_confidence = high
		if proposal.categories:
			result.proposals.append(proposal)
	return result
