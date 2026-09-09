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
	build_author_clusters,
	build_genre_clusters,
	build_known_author_pool,
	fold_genre,
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
		assert summary == {"added": 1, "updated": 0, "skipped_decided": 0}
		items = parse_review(p)
		assert len(items) == 1
		assert items[0].action == "accept" and items[0].proposed["authors"] == ["Neznámý"]
		assert items[0].diagnosis["category"] == "C15"

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
		assert summary == {"added": 0, "updated": 0, "skipped_decided": 1}
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
