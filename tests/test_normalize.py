"""Tests for library-wide normalization (normalize.py) and its review merge.

The clustering rules encode judgement calls agreed with the library's owner:
deterministic merges (fold-equal, initials vs full name, anonym spellings,
titles, ALL-CAPS, swapped order with cluster evidence) pre-fill accept;
letter variants, mojibake and fuzzy typos stay pending. The homonym guard
(different first-name initials never merge) is the safety net that keeps
Karel/Josef Čapek or Dan/Eric Brown apart.
"""
from __future__ import annotations

from collections import Counter

from book_meta_fix.models import BookMeta
from book_meta_fix.normalize import (
	analyze_library,
	analyze_sequence,
	build_author_clusters,
	build_genre_clusters,
	build_known_author_pool,
	build_series_clusters,
	canonical_language,
	fold_genre,
	fold_series,
)
from book_meta_fix.pipeline import _apply_fields


def _author_map(counter: dict[str, int]):
	clusters, _multi = build_author_clusters(Counter(counter), {a: ["u"] * n for a, n in counter.items()})
	return clusters


def _one(counter: dict[str, int], raw: str):
	"""The (single) cluster a raw string maps to, or None."""
	return _author_map(counter).get(raw)


class TestAuthorClusters:
	def test_initials_vs_full_name_merges_high(self):
		c = _one({"Robert A. Heinlein": 42, "Robert Anson Heinlein": 3}, "Robert Anson Heinlein")
		assert c is not None and c.canonical == "Robert A. Heinlein"
		assert c.confidence.value == "HIGH" and c.kind == "variant"

	def test_initials_spacing_and_hyphens_fold(self):
		c = _one({"G. J. Arnaud": 5, "G.-J. Arnaud": 1, "G.J. Arnaud": 1, "Georges Jean Arnaud": 2}, "G.J. Arnaud")
		assert c is not None and c.canonical == "G. J. Arnaud"
		assert c.confidence.value == "HIGH"

	def test_dotted_middle_initials_fold(self):
		# "R." must fold to "r" (with the dot stripped), not "r." — otherwise
		# "George R. R. Martin" and "George R.R. Martin" would not meet.
		c = _one({"George R. R. Martin": 8, "George R.R. Martin": 1}, "George R.R. Martin")
		assert c is not None and c.canonical == "George R. R. Martin"

	def test_diacritics_variant_prefers_diacritic_spelling(self):
		# "Jiří" x8 must beat "Jiri" x2 even though both are fold-equal.
		c = _one({"Jiří Kulhánek": 8, "Jiri Kulhanek": 2}, "Jiri Kulhanek")
		assert c is not None and c.canonical == "Jiří Kulhánek"

	def test_nfc_decomposed_duplicate_merges(self):
		decomposed = "Karel Čapek".replace("Č", "C\u030c").replace("á", "a\u0301")
		assert decomposed != "Karel Čapek"
		c = _one({"Karel Čapek": 23, decomposed: 1}, decomposed)
		assert c is not None and c.canonical == "Karel Čapek"

	def test_letter_variant_is_pending_not_prefilled(self):
		# Frederik/Frederick differ beyond initials — same person probably,
		# but same-letter homonyms exist, so the cluster is MEDIUM (pending).
		c = _one({"Frederik Pohl": 3, "Frederick Pohl": 1}, "Frederick Pohl")
		assert c is not None and c.confidence.value == "MEDIUM"

	def test_different_first_initials_never_merge(self):
		# Homonym guard: Karel vs Josef Čapek (and Dan vs Eric Brown).
		assert _one({"Karel Čapek": 23, "Josef Čapek": 4}, "Josef Čapek") is None
		assert _one({"Dan Brown": 5, "Eric Brown": 3}, "Eric Brown") is None

	def test_two_token_given_cannot_step_over_first_token(self):
		# Regression from the real library: the broken in-position skip let
		# "Kevin J." match "Poul" by stepping over "kevin", false-merging
		# Poul Anderson with Kevin J. Anderson as HIGH.
		assert _one({"Poul Anderson": 10, "Kevin J. Anderson": 7}, "Kevin J. Anderson") is None
		assert _one({"Wilbur Smith": 20, "Ali Smith": 2, "R. Scott Smith": 1}, "Ali Smith") is None

	def test_omitted_middle_name_still_merges(self):
		# "Robert Heinlein" vs "Robert A. Heinlein": shared prefix aligns,
		# the omission lands in the trailing leftovers.
		c = _one({"Robert A. Heinlein": 42, "Robert Heinlein": 3}, "Robert Heinlein")
		assert c is not None and c.canonical == "Robert A. Heinlein"

	def test_allcaps_variant_loses_canonical(self):
		c = _one({"Jiří W. Procházka": 13, "JIŘÍ WALKER PROCHÁZKA": 1}, "JIŘÍ WALKER PROCHÁZKA")
		assert c is not None and c.canonical == "Jiří W. Procházka"

	def test_title_stripped_from_cluster(self):
		c = _one({"Václav Semerád": 12, "Ing. Václav Semerád": 1}, "Ing. Václav Semerád")
		assert c is not None and c.canonical == "Václav Semerád" and c.kind == "title"

	def test_single_dotted_initial_is_not_a_connector(self):
		# "A." in "Robert A. Heinlein" is an initial, not the Czech "a".
		assert _one({"Robert A. Heinlein": 42}, "Robert A. Heinlein") is None  # alone: nothing to unify

	def test_anonym_spellings_unify_to_neznamy(self):
		c = _one({"Neznamy": 63, "Neznámý": 24, "neznámý": 1, "Unknown": 17}, "Neznamy")
		assert c is not None and c.canonical == "Neznámý"
		assert c.kind == "anonym" and c.confidence.value == "HIGH"

	def test_single_anonym_spelling_also_maps(self):
		c = _one({"Neznamy": 1}, "Neznamy")
		assert c is not None and c.canonical == "Neznámý"

	def test_mojibake_anonym_joins_neznamy_family(self):
		# "NeznÁvmÁ" is an encoding-mangled "Neznámý" from the real library —
		# without the distance check it canonised itself instead of joining
		# the anonym family.
		c = _one({"Neznámý": 24, "NeznÁvmÁ": 4}, "NeznÁvmÁ")
		assert c is not None and c.canonical == "Neznámý"

	def test_mojibake_variant_is_pending(self):
		c = _one({"Jiří Kosek": 8, "JiĹ™Ă Kosek": 1}, "JiĹ™Ă Kosek")
		assert c is not None and c.canonical == "Jiří Kosek"
		assert c.confidence.value == "MEDIUM"

	def test_comma_form_with_evidence_is_high(self):
		c = _one({"Peter James": 17, "James, Peter": 2}, "James, Peter")
		assert c is not None and c.canonical == "Peter James" and c.kind == "comma"
		assert c.confidence.value == "HIGH"

	def test_isolated_comma_form_is_pending(self):
		# "Weis, Hickman" may be two authors joined by a comma, not a swap.
		c = _one({"Weis, Hickman": 1}, "Weis, Hickman")
		assert c is not None and c.confidence.value == "MEDIUM"
		assert c.canonical == "Hickman Weis"

	def test_swapped_order_pair_merges_to_majority(self):
		c = _one({"Přemysl Podroužek": 10, "Podroužek Přemysl": 1}, "Podroužek Přemysl")
		assert c is not None and c.canonical == "Přemysl Podroužek" and c.kind == "swapped"
		assert c.confidence.value == "HIGH"

	def test_multi_author_strings_skipped(self):
		_, multi = build_author_clusters(
			Counter({"Wilhelm a Jacob Grimmové": 2, "Dan Brown": 5}), {"Wilhelm a Jacob Grimmové": ["u1"], "Dan Brown": ["u2"]}
		)
		assert [raw for raw, _ in multi] == ["Wilhelm a Jacob Grimmové"]

	def test_frequencies_drive_canonical_not_raw_count_of_mangled(self):
		# "Neznamy" x63 loses to "Neznámý" x24: clean-then-frequent, not
		# frequent-alone (frequency alone would canonise the mangled form).
		c = _one({"Neznamy": 63, "Neznámý": 24}, "Neznamy")
		assert c.canonical == "Neznámý"


class TestKnownAuthorPool:
	"""The library-wide author index behind C1's pool pattern ("is the TITLE
	string a known library author?") — built from the same clustering as
	`bmf normalize`, so variants resolve to the cluster canonical."""

	def _books(self, **counts):
		# One BookMeta per (author, index) so book counts are real.
		books: list[BookMeta] = []
		i = 0
		for author, n in counts.items():
			for _ in range(n):
				books.append(BookMeta(calibre_id=str(i), uuid=f"u{i}", title=f"Kniha {i}", authors=[author], path=f"/lib/{i}"))
				i += 1
		return books

	def test_variants_resolve_to_cluster_canonical_with_total_count(self):
		pool = build_known_author_pool(self._books(**{"Anatolij Dněprov": 3, "A. Dněprov": 2}))
		hit = pool.lookup("A. Dněprov")
		assert hit == ("Anatolij Dněprov", 5)

	def test_lone_spelling_is_its_own_pool_entry(self):
		# A single-spelling author never forms a C15 cluster (nothing to
		# change) — the pool indexes it under its own fold key anyway.
		pool = build_known_author_pool(self._books(**{"Jan Novák": 1}))
		assert pool.lookup("Jan Novák") == ("Jan Novák", 1)

	def test_anonym_family_excluded(self):
		# A title "Neznámý" must never match an author — the anonym spellings
		# (and their mojibake forms) are not persons.
		pool = build_known_author_pool(self._books(**{"Neznámý": 10, "Neznamy": 4, "Jan Novák": 1}))
		assert pool.lookup("Neznámý") is None
		assert pool.lookup("Neznamy") is None
		assert pool.lookup("NeznAVmA") is None

	def test_same_person_across_variants(self):
		pool = build_known_author_pool(self._books(**{"Arthur C. Clarke": 4, "Arthur Charles Clarke": 1, "Isaac Asimov": 2}))
		assert pool.same_person("Arthur C. Clarke", "Arthur Charles Clarke") is True
		assert pool.same_person("Arthur C. Clarke", "Isaac Asimov") is False
		assert pool.same_person("Arthur C. Clarke", "Nikdo Známý") is False

	def test_non_name_titles_do_not_match(self):
		# lookup() gates on _parse_author: multi-author chains, digit bearers
		# and sentences are not person names, whatever strings the library has.
		pool = build_known_author_pool(self._books(**{"Jan Novák": 2}))
		assert pool.lookup("Novák a Nový") is None
		assert pool.lookup("5 dílů série") is None
		assert pool.lookup("") is None

	def test_surname_first_order_lookup(self):
		# The swap key is indexed too: a title written surname-first still
		# resolves (the same convention C15's swapped-order tier handles).
		pool = build_known_author_pool(self._books(**{"Jan Novák": 2}))
		assert pool.lookup("Novák Jan") == ("Jan Novák", 2)


class TestGenreClusters:
	def test_case_fold_unifies_to_most_frequent_spelling(self):
		m = build_genre_clusters(Counter({"Sci-fi": 464, "sci-fi": 449}))
		assert m["sci-fi"].canonical == "Sci-fi"
		assert m["sci-fi"].confidence.value == "HIGH" and m["sci-fi"].kind == "fold"

	def test_word_order_folds(self):
		m = build_genre_clusters(Counter({"Literatura česká": 163, "česká literatura": 30}))
		assert m["česká literatura"].canonical == "Literatura česká"

	def test_english_alias_to_czech(self):
		m = build_genre_clusters(Counter({"Sci-fi": 10, "Science Fiction": 5}))
		assert m["Science Fiction"].canonical == "Sci-fi"
		assert m["Science Fiction"].kind == "alias"

	def test_alias_targets_resolve_through_fold_groups(self):
		# Comedy and Humour both land on the library's own dominant spelling
		# of Humor — one canonical, not two.
		m = build_genre_clusters(Counter({"humor": 82, "Humor": 78, "Comedy": 4, "Humour": 13}))
		assert m["Comedy"].canonical == "humor"
		assert m["Humour"].canonical == "humor"
		assert m["Humor"].canonical == "humor"

	def test_misspelling_row_catches_mumour(self):
		# The user's example word — an explicit GENRE_ALIASES row (the former
		# fuzzy typo tier was removed: ~50% of its distance-2 matches on the
		# real library were false pairs like Afrika→Amerika).
		m = build_genre_clusters(Counter({"humor": 20, "mumour": 1}))
		assert m["mumour"].canonical == "humor"
		assert m["mumour"].kind == "alias"

	def test_no_fuzzy_merging_at_all(self):
		# Real-library false positives that killed the fuzzy tier: these are
		# distinct words at edit distance ~2 and must stay separate.
		m = build_genre_clusters(Counter({
			"čarodějové": 65, "čarodějnice": 20, "Historické romány": 25,
			"humoristické romány": 10, "Afrika": 9, "Amerika": 5,
			"etika": 1, "erotika": 4, "vlaky": 1, "války": 13,
		}))
		assert "čarodějové" not in m
		assert "čarodějnice" not in m
		assert "humoristické romány" not in m
		assert "Amerika" not in m
		assert "etika" not in m
		assert "vlaky" not in m

	def test_plural_alias_rows(self):
		m = build_genre_clusters(Counter({"western": 15, "westerny": 25, "drama": 10, "dramata": 12}))
		assert m["westerny"].canonical == "western"
		assert m["dramata"].canonical == "drama"

	def test_shared_suffix_families_do_not_merge(self):
		# "česká/ruská/anglická literatura" all share the 10-char suffix;
		# whole-string fuzzy would merge them (~90 ratio). The token-pair
		# rule must keep them apart.
		m = build_genre_clusters(Counter({
			"česká literatura": 30, "ruská literatura": 10, "anglická literatura": 24,
		}))
		assert not any(v.canonical != k for k, v in m.items())

	def test_singleton_left_alone(self):
		m = build_genre_clusters(Counter({"Hugo (literární cena)": 14, "Sci-fi": 100, "sci-fi": 90}))
		assert "Hugo (literární cena)" not in m

	def test_fold_genre_key(self):
		assert fold_genre("Literatura česká") == fold_genre("česká literatura")
		# tokens are sorted, so word order never distinguishes genres
		assert fold_genre("Sci-fi") == "fi sci"


class TestSeriesSequence:
	def test_missing_gaps_and_duplicates(self):
		seq = analyze_sequence(["1", "2", "4", "7", "8", "4"])
		assert seq.present == [1.0, 2.0, 4.0, 7.0, 8.0]
		assert seq.missing == [(3, 3), (5, 6)]
		assert seq.duplicates == [(4.0, 2)]
		assert seq.unnumbered == 0 and seq.anomalies == []

	def test_unnumbered_and_anomalies(self):
		# "0" and "III" are anomalies, "3,5" a legit sub-volume, "" unnumbered.
		seq = analyze_sequence(["", "", "0", "III", "3,5"])
		assert seq.unnumbered == 2
		assert seq.anomalies == [("0", 1), ("III", 1)]
		assert seq.present == [3.5]
		assert seq.missing == [] and seq.duplicates == []


class TestSeriesClusters:
	def _build(self, counter: dict[str, int], **kw):  # noqa: ANN003
		return build_series_clusters(Counter(counter), **kw)

	def test_glued_order_folds_with_bare_name(self):
		assert fold_series("Mark Stone #73") == fold_series("Mark Stone")
		assert fold_series("Zaklínač") == fold_series("Zaklinac") == fold_series("zaklínač")

	def test_word_order_not_merged(self):
		# Close sub-series are genuinely different series — the series fold is
		# order-sensitive on purpose (unlike the genre fold).
		clusters, suspects = self._build({"Legenda o Drizztovi": 2, "Drizztova legenda": 3})
		assert not clusters and not suspects

	def test_fold_unifies_to_most_frequent_high(self):
		clusters, suspects = self._build({"Zaklínač": 4, "Zaklinac": 1, "zaklínač": 1})
		assert not suspects
		c = clusters["Zaklinac"]
		assert c.canonical == "Zaklínač" and c.confidence.value == "HIGH" and c.kind == "fold"
		assert clusters["zaklínač"] is c

	def test_alias_row_merges_pending(self):
		# A human-written row is an explicit decision, but it retitles a whole
		# group at once — MEDIUM/pending is the one-confirm safety net.
		clusters, _ = self._build(
			{"Perry Rhodan": 8, "Perry Rodan": 2},
			aliases={"Perry Rodan": "Perry Rhodan"},
		)
		c = clusters["Perry Rodan"]
		assert c.canonical == "Perry Rhodan" and c.confidence.value == "MEDIUM" and c.kind == "alias"

	def test_prefix_pair_complementary_numbering_merges(self):
		idx = {"Mark Stone": ["1", "2", "4", "5"], "Mark Stone (edice)": ["3"]}
		clusters, suspects = self._build({"Mark Stone": 4, "Mark Stone (edice)": 1}, indexes_by_name=idx)
		c = clusters["Mark Stone (edice)"]
		assert c.canonical == "Mark Stone" and c.confidence.value == "MEDIUM"
		s = suspects[0]
		assert s.verdict == "merge" and s.complementary and not s.collision

	def test_prefix_pair_numbering_collision_never_merges(self):
		# Both claim volumes 1-2: two real series (or duplicates) — a merge
		# would be wrong, the pair stays advisory.
		idx = {"Duna": ["1", "2"], "Duna: Chronologie": ["1", "2"]}
		clusters, suspects = self._build({"Duna": 2, "Duna: Chronologie": 2}, indexes_by_name=idx)
		assert "Duna: Chronologie" not in clusters
		assert suspects[0].verdict == "distinct" and suspects[0].collision

	def test_fuzzy_against_verified_name_suspected(self):
		# "Perry Rodan" is not a prefix twin and folds differently; only its
		# closeness to the VERIFIED "Perry Rhodan" makes it a suspect, and the
		# complementary numbering (1,2 + 3) upgrades it to a merge proposal.
		idx = {"Perry Rhodan": ["1", "2"], "Perry Rodan": ["3"]}
		clusters, suspects = self._build(
			{"Perry Rhodan": 2, "Perry Rodan": 1},
			indexes_by_name=idx,
			verified_names={"Perry Rhodan"},
		)
		assert clusters["Perry Rodan"].canonical == "Perry Rhodan"
		assert any(s.kind == "fuzzy" and s.verdict == "merge" for s in suspects)

	def test_online_existence_decides_without_numbering(self):
		# Only a DECORATIVE tail stays online-decidable — a content extension
		# is a named sub-series (see test_named_subseries... below).
		clusters, suspects = self._build(
			{"Duna": 2, "Duna (edice)": 1},
			online_check=lambda n: n == "Duna",
		)
		assert clusters["Duna (edice)"].canonical == "Duna"
		s = suspects[0]
		assert s.verdict == "merge" and s.online_base and not s.online_suspect

	def test_named_subseries_never_merges_into_umbrella(self):
		# Real library shape (measured 2026-09-10): the umbrella "Star Wars"
		# holds vols {3,4}, the one-book lines "Star Wars - Akademie Jedi"
		# {2} and "Star Wars - Legendy" {5} — both unions are CONTIGUOUS
		# ({2,3,4}, {3,4,5}), which the complementary rule alone misread as
		# "one series" and retitled both lines to "Star Wars" on every run.
		idx = {"Star Wars": ["4", "3", ""], "Star Wars - Akademie Jedi": ["2"], "Star Wars - Legendy": ["5"]}
		counter = {"Star Wars": 3, "Star Wars - Akademie Jedi": 1, "Star Wars - Legendy": 1}
		clusters, suspects = self._build(counter, indexes_by_name=idx)
		assert not clusters
		verdicts = {(s.base, s.suspect): s.verdict for s in suspects}
		assert verdicts[("Star Wars", "Star Wars - Akademie Jedi")] == "distinct"
		assert verdicts[("Star Wars", "Star Wars - Legendy")] == "distinct"
		assert all(s.subseries for s in suspects)
		# The online tier must not resurrect the merge either: the FRANCHISE
		# umbrella exists in the bibliographic DB, the local line name does not.
		clusters, suspects = self._build(counter, indexes_by_name=idx, online_check=lambda n: n == "Star Wars")
		assert not clusters
		assert all(s.verdict == "distinct" for s in suspects)

	def test_both_online_stay_distinct(self):
		clusters, suspects = self._build(
			{"Duna": 2, "Duna Chronicles": 1},
			online_check=lambda n: True,
		)
		assert not clusters
		assert suspects[0].verdict == "distinct"

	def test_singleton_untouched(self):
		clusters, suspects = self._build({"Only Series": 3})
		assert not clusters and not suspects


class TestAnalyzeSeries:
	def _book(self, uuid, name=None, idx=None, series_list=None, verified=False):  # noqa: ANN001
		series = series_list if series_list is not None else ([{"name": name, "index": idx}] if name else [])
		return BookMeta(
			uuid=uuid, path=f"/lib/{uuid}", title="T", authors=["A"], series=series, verified=verified
		)

	def test_proposes_canonical_name_only(self):
		books = [self._book("u1", "Zaklínač", "1"), self._book("u2", "Zaklinac", "2")]
		res = analyze_library(books, fields=("series",))
		p = next(p for p in res.proposals if p.uuid == "u2")
		assert p.series == "Zaklínač" and p.series_reasons
		assert p.categories == ["C18"]
		# Only the NAME is proposed — apply keeps the book's own index half.
		meta = self._book("u2", "Zaklinac", "2")
		_apply_fields(meta, {"series": p.series})
		assert meta.series == [{"name": "Zaklínač", "index": "2"}]

	def test_fold_high_suspect_merge_pending(self):
		books = [
			self._book("u1", "Mark Stone", "1"),
			self._book("u2", "Mark Stone", "2"),
			self._book("u3", "Mark Stone", "4"),
			self._book("u4", "Mark Stone (edice)", "3"),
			self._book("u5", "Zaklínač", "1"),
			self._book("u6", "Zaklinac", "2"),
		]
		res = analyze_library(books, fields=("series",))
		sus = next(p for p in res.proposals if p.uuid == "u4")
		fold = next(p for p in res.proposals if p.uuid == "u6")
		assert sus.high_confidence is False  # suspect tier → pending
		assert fold.high_confidence is True  # deterministic fold → accept

	def test_glued_and_multi_series_skipped_and_reported(self):
		books = [
			self._book("u1", "Mark Stone #73", "73"),  # dict-glued: C14's split owns it
			self._book("u2", series_list=[{"name": "A", "index": "1"}, {"name": "B", "index": "2"}]),
			self._book("u3", "Fine Series", "1"),
		]
		res = analyze_library(books, fields=("series",))
		assert not res.proposals
		assert ("Mark Stone #73", 1) in res.glued_series
		assert ("A", 1) in res.multi_series and ("B", 1) in res.multi_series

	def test_named_subseries_books_get_no_series_proposal(self):
		# The library-level twin of the cluster regression: umbrella books
		# (vols 3, 4 + one unnumbered) must not pull the one-book lines
		# "Star Wars - Akademie Jedi"/"Star Wars - Legendy" into a rename.
		books = [
			self._book("u1", "Star Wars", "4"),
			self._book("u2", "Star Wars", "3"),
			self._book("u3", "Star Wars", ""),
			self._book("u4", "Star Wars - Akademie Jedi", "2"),
			self._book("u5", "Star Wars - Legendy", "5"),
		]
		res = analyze_library(books, fields=("series",))
		assert not [p for p in res.proposals if p.series is not None]

	def test_overview_mirrors_disk_state(self):
		books = [self._book("u1", "Zaklínač", "1"), self._book("u2", "Zaklinac", "2")]
		res = analyze_library(books, fields=("series",))
		assert len(res.series_overview) == 1
		g = res.series_overview[0]
		assert g.canonical == "Zaklínač" and g.books == 2
		assert g.sequence.present == [1.0, 2.0]
		assert [(v, n) for v, n in g.variants if v != g.canonical] == [("Zaklinac", 1)]

	def test_series_only_field_selection_leaves_authors_alone(self):
		books = [
			BookMeta(uuid="u1", path="/lib/u1", title="T", authors=["Neznamy"], series=[{"name": "Zaklinac", "index": "1"}]),
			BookMeta(uuid="u2", path="/lib/u2", title="T", authors=["Neznamy"], series=[{"name": "Zaklínač", "index": "2"}]),
		]
		res = analyze_library(books, fields=("series",))
		p = next(p for p in res.proposals if p.uuid == "u1")
		assert p.series == "Zaklínač" and p.authors is None and p.genres is None and p.tags is None

	def test_clusters_sorted_alphabetically_diacritics_folded(self):
		books = [
			self._book("u1", "Žízeň", "1"), self._book("u2", "Zizen", "2"),
			self._book("u3", "Zaklinac", "1"), self._book("u4", "Zaklínač", "2"),
			self._book("u5", "Úžasná Zeměplocha", "1"), self._book("u6", "Úžasná Zemeplocha", "2"),
		]
		res = analyze_library(books, fields=("series",))
		names = [c.canonical for c in res.series_clusters]
		# "Ú" sorts under U (diacritics folded), "Ž" last — the table exists
		# for looking up a known name, so it is alphabetical, not count-ranked.
		assert names == ["Úžasná Zeměplocha", "Zaklínač", "Žízeň"]


class TestAnalyzeLibrary:
	def _books(self):
		return [
			BookMeta(uuid="u1", calibre_id=1, path="/lib/Neznamy/X (1)", title="X",
				authors=["Neznamy"], genres=["sci-fi", "Humor"], tags=["Science Fiction"]),
			BookMeta(uuid="u2", calibre_id=2, path="/lib/B/Y (2)", title="Y",
				authors=["James, Peter"], genres=["česká literatura", "Literatura česká"]),
			BookMeta(uuid="u3", calibre_id=3, path="/lib/C/Z (3)", title="Z",
				authors=["Dan Brown"], genres=["fantasy"]),
		] + [
			BookMeta(uuid=f"p{i}", path=f"/lib/P/{i}", title="P", authors=["Dan Brown"], genres=["Sci-fi"])
			for i in range(12)
		]

	def test_proposal_dedupes_and_replaces_whole_lists(self):
		res = analyze_library(self._books())
		p1 = next(p for p in res.proposals if p.uuid == "u1")
		assert p1.authors == ["Neznámý"]
		# "sci-fi" folds onto the dominant "Sci-fi" spelling; "Humor" has no
		# twin in this vocabulary → stays, but the whole list is re-proposed.
		assert p1.genres == ["Sci-fi", "Humor"]
		assert p1.tags == ["Sci-fi"]
		assert p1.categories == ["C15", "C16"]

	def test_pending_when_any_change_is_judgement(self):
		res = analyze_library(self._books())
		p2 = next(p for p in res.proposals if p.uuid == "u2")
		# The comma reorder has no evidence (no "Peter James" elsewhere).
		assert p2.high_confidence is False
		assert p2.authors == ["Peter James"]

	def test_clean_book_gets_no_proposal(self):
		res = analyze_library(self._books())
		assert all(p.uuid != "u3" for p in res.proposals)

	def test_authors_only_field_selection(self):
		res = analyze_library(self._books(), fields=("authors",))
		assert all(p.genres is None and p.tags is None for p in res.proposals)

	def test_cluster_tables_sorted_alphabetically(self):
		"""The C15/C16 cluster tables share the C18 alphabetical order with
		the diacritics-folded key (Ú under U, Ž last) — lookup aids, not size
		rankings. The counts here make count-order differ from alphabetical."""
		books = [
			*[BookMeta(uuid=f"z{i}", path=f"/z{i}", title="T", authors=["Zilka Jan"]) for i in range(4)],
			BookMeta(uuid="z9", path="/z9", title="T", authors=["Žilka Jan"]),
			BookMeta(uuid="u1", path="/u1", title="T", authors=["Úr Jan"]),
			BookMeta(uuid="u2", path="/u2", title="T", authors=["Ur Jan"]),
			BookMeta(uuid="g1", path="/g1", title="T", authors=["G H"], genres=["žába"]),
			BookMeta(uuid="g2", path="/g2", title="T", authors=["G H"], genres=["Žába"]),
			BookMeta(uuid="g3", path="/g3", title="T", authors=["G H"], genres=["žába"]),
			BookMeta(uuid="g4", path="/g4", title="T", authors=["G H"], genres=["Ábie"]),
			BookMeta(uuid="g5", path="/g5", title="T", authors=["G H"], genres=["Abie"]),
		]
		res = analyze_library(books)
		# Žilka has MORE books (5 vs 2) — alphabetical still puts Úr first;
		# the diacritic spelling stays the canonical despite ×4 ASCII copies.
		assert [c.canonical for c in res.author_clusters] == ["Úr Jan", "Žilka Jan"]
		# žába ×3 beats Ábie ×2 — alphabetical still puts Ábie first.
		assert [c.canonical for c in res.genre_clusters] == ["Ábie", "žába"]


class TestLanguageCanonicalization:
	def test_three_letter_families_map_to_two_letter(self):
		# ISO 639-2/B (cze, slk, fre), 639-2/T ≈ 639-3 (ces, slo, fra) and
		# the junk spellings (cz = country code, csy = legacy Windows locale)
		# all land on the BCP 47 two-letter form (the EPUB dc:language norm).
		assert canonical_language("ces") == "cs"
		assert canonical_language("cze") == "cs"
		assert canonical_language("cz") == "cs"
		assert canonical_language("slk") == "sk"
		assert canonical_language("slo") == "sk"
		assert canonical_language("fre") == "fr"
		assert canonical_language("fra") == "fr"
		assert canonical_language("eng") == "en"

	def test_case_and_region_suffix_reduce_to_primary(self):
		assert canonical_language("CS") == "cs"
		assert canonical_language("cs-CZ") == "cs"
		assert canonical_language("cs_CZ") == "cs"
		assert canonical_language(" ces ") == "cs"

	def test_already_canonical_unchanged(self):
		assert canonical_language("cs") == "cs"
		assert canonical_language("sk") == "sk"
		assert canonical_language("en") == "en"
		assert canonical_language("en-GB") == "en"

	def test_unknown_returned_untouched(self):
		# The curated table is the only proof — unrecognized values are
		# reported (unknown_languages), never rewritten.
		assert canonical_language("čeština") == "čeština"
		assert canonical_language("klingon") == "klingon"

	def test_none_and_empty_pass_through(self):
		assert canonical_language(None) is None
		assert canonical_language("") == ""


class TestAnalyzeLanguage:
	def _books(self):
		return [
			BookMeta(uuid="l1", path="/l1", title="A", authors=["X"], language="ces"),
			BookMeta(uuid="l2", path="/l2", title="B", authors=["X"], language="cs"),
			BookMeta(uuid="l3", path="/l3", title="C", authors=["X"], language="čeština"),
			BookMeta(uuid="l4", path="/l4", title="D", authors=["X"], language=None),
		]

	def test_proposes_two_letter_form(self):
		res = analyze_library(self._books(), fields=("language",))
		p = next(p for p in res.proposals if p.uuid == "l1")
		assert p.language == "cs"
		assert p.categories == ["C20"] and p.high_confidence is True
		assert p.language_reasons == ["language 'ces' → 'cs' (ISO 639-1 / BCP 47)"]

	def test_canonical_unknown_and_missing_get_no_proposal(self):
		res = analyze_library(self._books(), fields=("language",))
		assert all(p.uuid not in {"l2", "l3", "l4"} for p in res.proposals)
		assert res.unknown_languages == [("čeština", 1)]

	def test_cluster_table_and_apply_roundtrip(self):
		res = analyze_library(self._books(), fields=("language",))
		assert [(c.canonical, c.variants) for c in res.language_clusters] == [("cs", [("ces", 1)])]
		meta = BookMeta(title="A", authors=["X"], language="ces")
		_apply_fields(meta, {"language": "cs"})
		assert meta.language == "cs"

	def test_language_in_default_fields(self):
		# A plain analyze_library() run (no fields kwarg) includes the tier.
		res = analyze_library(self._books())
		assert res.language_clusters and any(p.uuid == "l1" for p in res.proposals)


class TestApplyTags:
	def test_tags_list_replaces(self):
		meta = BookMeta(authors=["A"], title="T", tags=["sci-fi", "Science Fiction"])
		_apply_fields(meta, {"tags": ["Sci-fi"]})
		assert meta.tags == ["Sci-fi"]

	def test_tags_scalar_wraps_to_list(self):
		meta = BookMeta(authors=["A"], title="T", tags=["old"])
		_apply_fields(meta, {"tags": "Sci-fi"})
		assert meta.tags == ["Sci-fi"]

	def test_tags_null_clears(self):
		meta = BookMeta(authors=["A"], title="T", tags=["old"])
		_apply_fields(meta, {"tags": None})
		assert meta.tags == []


class TestMergeNormalizations:
	def _review_file(self, tmp_path, entries_yaml: str) -> object:
		p = tmp_path / "review.yaml"
		p.write_text(entries_yaml, encoding="utf-8")
		return p

	def test_adds_new_entry_with_prefilled_accept(self, tmp_path):
		from book_meta_fix.normalize import BookProposal
		from book_meta_fix.review import merge_normalizations, parse_review

		p = self._review_file(tmp_path, "# empty\n")
		prop = BookProposal(
			uuid="u1", path="/lib/A", calibre_id=7, authors=["Neznámý"],
			author_reasons=["Neznamy → Neznámý (anonymous-author spelling unified to 'Neznámý')"],
			high_confidence=True,
		)
		books = [BookMeta(uuid="u1", calibre_id=7, path="/lib/A", authors=["Neznamy"], title="X", genres=["humor"])]
		summary = merge_normalizations(p, [prop], books, library_root=None)
		assert summary == {"added": 1, "updated": 0, "skipped_decided": 0, "verified_prefilled": 0}
		items = parse_review(p)
		assert len(items) == 1
		assert items[0].action == "accept" and items[0].proposed["authors"] == ["Neznámý"]
		assert items[0].diagnosis["category"] == "C15"
		# The book still misses ISBN/year/cover (fake path, no files) — the
		# projection is not detector-clean, so no verified pre-fill.
		assert not items[0].verified

	def test_fresh_deterministic_entry_on_clean_book_gets_verified(self, tmp_path):
		"""A fresh HIGH-confidence normalize proposal on an otherwise-clean
		book is born verified: apply fixes AND closes it in one pass (the same
		close-the-loop contract as the analyze path's _projected_clean)."""
		from book_meta_fix.normalize import BookProposal
		from book_meta_fix.review import merge_normalizations, parse_review

		# A real folder shape — cover.jpg + an ebook file — else MISSING_COVER /
		# EMPTY_BOOK fire and the projection is never clean.
		book_dir = tmp_path / "A" / "Stin hmly"
		book_dir.mkdir(parents=True)
		(book_dir / "cover.jpg").write_bytes(b"x")
		(book_dir / "book.epub").write_bytes(b"x")

		p = self._review_file(tmp_path, "# empty\n")
		prop = BookProposal(uuid="u1", path=str(book_dir), genres=["humor"],
			genre_reasons=["genre 'Humor' → 'humor' (fold group canonical)"], high_confidence=True)
		books = [BookMeta(uuid="u1", path=str(book_dir), title="Stín hmly",
			authors=["Graham Masterton"], isbn="8020312345", year=2005, genres=["Humor"])]
		summary = merge_normalizations(p, [prop], books)
		assert summary["verified_prefilled"] == 1
		items = parse_review(p)
		assert items[0].action == "accept"
		assert items[0].verified is True

	def test_fresh_entry_with_missing_leftover_stays_unverified(self, tmp_path):
		"""Same deterministic genre fix, but the book still misses its ISBN:
		closing it would cancel the enricher retries that field still needs —
		the accept pre-fill stays, the verified mark does not."""
		from book_meta_fix.normalize import BookProposal
		from book_meta_fix.review import merge_normalizations, parse_review

		book_dir = tmp_path / "A" / "Stin hmly"
		book_dir.mkdir(parents=True)
		(book_dir / "cover.jpg").write_bytes(b"x")
		(book_dir / "book.epub").write_bytes(b"x")

		p = self._review_file(tmp_path, "# empty\n")
		prop = BookProposal(uuid="u1", path=str(book_dir), genres=["humor"],
			genre_reasons=["genre 'Humor' → 'humor' (fold group canonical)"], high_confidence=True)
		books = [BookMeta(uuid="u1", path=str(book_dir), title="Stín hmly",
			authors=["Graham Masterton"], year=2005, genres=["Humor"])]  # no ISBN
		summary = merge_normalizations(p, [prop], books)
		assert summary["verified_prefilled"] == 0
		items = parse_review(p)
		assert items[0].action == "accept"
		assert not items[0].verified

	def test_pending_proposal_gets_null_action(self, tmp_path):
		from book_meta_fix.normalize import BookProposal
		from book_meta_fix.review import merge_normalizations, parse_review

		p = self._review_file(tmp_path, "")
		prop = BookProposal(uuid="u2", path="/lib/B", genres=["humor"], genre_reasons=["genre 'Humor' → 'humor'"], high_confidence=False)
		books = [BookMeta(uuid="u2", path="/lib/B", authors=["A"], title="Y", genres=["Humor"])]
		merge_normalizations(p, [prop], books)
		items = parse_review(p)
		assert items[0].action is None
		assert items[0].diagnosis["category"] == "C16"
		assert items[0].current["genres"] == ["Humor"]
		assert not items[0].verified  # pending entries are never pre-verified

	def test_overlays_pending_entry_and_keeps_other_fields(self, tmp_path):
		from book_meta_fix.normalize import BookProposal
		from book_meta_fix.review import merge_normalizations, parse_review

		p = self._review_file(tmp_path, """---
id: 1
uuid: u1
path: A/X (1)
diagnosis:
  category: C2
  reason: filename as title
  confidence: HIGH
current:
  author: Neznamy
  title: X
proposed:
  title: Fixed Title
  source: embedded
action: null
""")
		prop = BookProposal(uuid="u1", path="/lib/A", authors=["Neznámý"],
			author_reasons=["Neznamy → Neznámý"], high_confidence=True)
		books = [BookMeta(uuid="u1", path="/lib/A", authors=["Neznamy"], title="X")]
		summary = merge_normalizations(p, [prop], books)
		assert summary["updated"] == 1
		items = parse_review(p)
		assert len(items) == 1
		assert items[0].proposed["title"] == "Fixed Title"  # untouched
		assert items[0].proposed["authors"] == ["Neznámý"]
		assert "normalize" in items[0].proposed["source"]
		cats = [d["category"] for d in items[0].diagnoses]
		assert cats == ["C2", "C15"]
		assert not items[0].verified  # an overlaid pending entry stays open for review

	def test_decided_entry_is_skipped(self, tmp_path):
		from book_meta_fix.normalize import BookProposal
		from book_meta_fix.review import merge_normalizations, parse_review

		p = self._review_file(tmp_path, """---
id: 1
uuid: u1
path: A/X (1)
diagnosis:
  category: C2
  reason: filename as title
  confidence: HIGH
current:
  title: X
proposed:
  title: Fixed
action: keep
""")
		prop = BookProposal(uuid="u1", path="/lib/A", authors=["Neznámý"], author_reasons=["r"], high_confidence=True)
		books = [BookMeta(uuid="u1", path="/lib/A", authors=["Neznamy"], title="X")]
		summary = merge_normalizations(p, [prop], books)
		assert summary == {"added": 0, "updated": 0, "skipped_decided": 1, "verified_prefilled": 0}
		items = parse_review(p)
		assert "authors" not in (items[0].proposed or {})

	def test_rerun_does_not_duplicate_diagnoses(self, tmp_path):
		from book_meta_fix.normalize import BookProposal
		from book_meta_fix.review import merge_normalizations, parse_review

		p = self._review_file(tmp_path, "")
		prop = BookProposal(uuid="u1", path="/lib/A", genres=["humor"], genre_reasons=["genre 'Humor' → 'humor'"], high_confidence=True)
		books = [BookMeta(uuid="u1", path="/lib/A", authors=["A"], title="X", genres=["Humor"])]
		merge_normalizations(p, [prop], books)
		merge_normalizations(p, [prop], books)
		items = parse_review(p)
		assert len(items) == 1
		cats = [d["category"] for d in items[0].diagnoses]
		assert cats.count("C16") == 1

	def test_missing_review_file_is_created(self, tmp_path):
		# First-ever run: no review.yaml yet — merge must create it, not crash.
		from book_meta_fix.normalize import BookProposal
		from book_meta_fix.review import merge_normalizations, parse_review

		p = tmp_path / "review.yaml"
		assert not p.exists()
		prop = BookProposal(uuid="u9", path="/lib/Z", authors=["Neznámý"],
			author_reasons=["Neznamy → Neznámý"], high_confidence=True)
		books = [BookMeta(uuid="u9", path="/lib/Z", authors=["Neznamy"], title="Q")]
		summary = merge_normalizations(p, [prop], books)
		assert summary["added"] == 1
		assert parse_review(p)[0].uuid == "u9"

	def test_series_c18_accept_and_pending(self, tmp_path):
		"""C18 entries: a deterministic fold proposal pre-fills accept, a
		suspect-tier proposal stays pending; both carry the canonical NAME
		only — the book's own series_index is never part of the proposal."""
		from book_meta_fix.normalize import BookProposal
		from book_meta_fix.review import merge_normalizations, parse_review

		p = self._review_file(tmp_path, "# empty\n")
		high = BookProposal(uuid="u1", path="/lib/A", series="Zaklínač",
			series_reasons=["series 'Zaklinac' → 'Zaklínač' (fold)"], high_confidence=True)
		med = BookProposal(uuid="u2", path="/lib/B", series="Mark Stone",
			series_reasons=["series 'Mark Stone (edice)' → 'Mark Stone' (suspect)"], high_confidence=False)
		books = [
			BookMeta(uuid="u1", path="/lib/A", authors=["A"], title="X", series=[{"name": "Zaklinac", "index": "2"}]),
			BookMeta(uuid="u2", path="/lib/B", authors=["A"], title="Y", series=[{"name": "Mark Stone (edice)", "index": "3"}]),
		]
		summary = merge_normalizations(p, [high, med], books)
		assert summary["added"] == 2
		items = {i.uuid: i for i in parse_review(p)}
		assert items["u1"].action == "accept" and items["u1"].proposed["series"] == "Zaklínač"
		assert items["u1"].diagnosis["category"] == "C18"
		assert items["u1"].current["series"] == "Zaklinac"
		assert "series_index" not in items["u1"].proposed
		assert items["u2"].action is None

	def test_series_overlaid_onto_pending_entry(self, tmp_path):
		from book_meta_fix.normalize import BookProposal
		from book_meta_fix.review import merge_normalizations, parse_review

		p = self._review_file(tmp_path, """---
id: 1
uuid: u1
path: A/X (1)
diagnosis:
  category: MISSING_ISBN
  reason: isbn missing
  confidence: HIGH
current:
  author: A
  title: X
proposed:
  isbn: "80-01-00000-0"
action: null
""")
		prop = BookProposal(uuid="u1", path="/lib/A", series="Zaklínač",
			series_reasons=["series 'Zaklinac' → 'Zaklínač' (fold)"], high_confidence=True)
		books = [BookMeta(uuid="u1", path="/lib/A", authors=["A"], title="X", series=[{"name": "Zaklinac", "index": "2"}])]
		summary = merge_normalizations(p, [prop], books)
		assert summary["updated"] == 1
		items = parse_review(p)
		assert items[0].proposed["isbn"] == "80-01-00000-0"  # other proposal keys kept
		assert items[0].proposed["series"] == "Zaklínač"  # series overlaid
		assert "C18" in [d["category"] for d in items[0].diagnoses]

	def test_language_c20_entry(self, tmp_path):
		"""C20: a curated-table mapping is deterministic HIGH — the fresh entry
		pre-fills accept and carries the raw spelling in `current`."""
		from book_meta_fix.normalize import BookProposal
		from book_meta_fix.review import merge_normalizations, parse_review

		p = self._review_file(tmp_path, "# empty\n")
		prop = BookProposal(uuid="u1", path="/lib/A", calibre_id=7, language="cs",
			language_reasons=["language 'ces' → 'cs' (ISO 639-1 / BCP 47)"],
			high_confidence=True)
		books = [BookMeta(uuid="u1", calibre_id=7, path="/lib/A", authors=["A"], title="X", language="ces")]
		summary = merge_normalizations(p, [prop], books, library_root=None)
		assert summary["added"] == 1
		items = parse_review(p)
		assert items[0].action == "accept" and items[0].proposed["language"] == "cs"
		assert items[0].diagnosis["category"] == "C20"
		assert items[0].current["language"] == "ces"

	def test_language_overlaid_onto_pending_entry(self, tmp_path):
		from book_meta_fix.normalize import BookProposal
		from book_meta_fix.review import merge_normalizations, parse_review

		p = self._review_file(tmp_path, """---
id: 1
uuid: u1
path: A/X (1)
diagnosis:
  category: MISSING_ISBN
  reason: isbn missing
  confidence: HIGH
current:
  author: A
  title: X
proposed:
  isbn: "80-01-00000-0"
action: null
""")
		prop = BookProposal(uuid="u1", path="/lib/A", calibre_id=1, language="sk",
			language_reasons=["language 'slk' → 'sk' (ISO 639-1 / BCP 47)"],
			high_confidence=True)
		books = [BookMeta(uuid="u1", calibre_id=1, path="/lib/A", authors=["A"], title="X", language="slk")]
		summary = merge_normalizations(p, [prop], books)
		assert summary["updated"] == 1
		items = parse_review(p)
		assert items[0].proposed["isbn"] == "80-01-00000-0"  # other proposal keys kept
		assert items[0].proposed["language"] == "sk"  # language overlaid
		assert "C20" in [d["category"] for d in items[0].diagnoses]
