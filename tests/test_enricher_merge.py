"""Tests for the parallel same-book merge in Enricher.lookup.

All applicable sources run concurrently; the first hit in priority order is
the ANCHOR, other results may only FILL fields the anchor lacks — and only
after passing the same-book gate. A result of a DIFFERENT book must never
contribute a single field.
"""
from __future__ import annotations

import pytest

from book_meta_fix import enrichers
from book_meta_fix.enrichers import EnrichedMeta, Enricher


def _enricher(**kw) -> Enricher:
	"""A fan-out Enricher with no cache and every source opt-in explicit."""
	defaults = dict(
		databazeknih_enabled=False, legie_enabled=False,
		openlibrary_enabled=False, google_books_enabled=False,
	)
	defaults.update(kw)
	return Enricher(**defaults)


class TestSameBookMerge:
	def test_secondary_fills_missing_fields(self, monkeypatch):
		"""The provider (priority 2) anchors; the databazeknih title scrape
		(priority 3) fills the ISBN and genres the provider row lacks, and the
		provider's year/publisher survive — each field keeps its first owner."""
		abs_hit = EnrichedMeta(
			title="Horká půda", authors=["Václava Molcarová"], source="abs_czech",
			publisher="Triton", year=2016, cover_url="https://abs/c.jpg",
		)
		dbk_hit = EnrichedMeta(
			title="Horká půda", authors=["Václava Molcarová"], source="databazeknih",
			isbn="9788073879570", genres=["Sci-fi"], series="Agent JFK",
		)
		monkeypatch.setattr(enrichers, "lookup_abs_czech", lambda **kw: abs_hit)
		monkeypatch.setattr(enrichers, "lookup_databazeknih", lambda **kw: dbk_hit)
		em = _enricher(databazeknih_enabled=True, abs_czech_url="http://p:8000").lookup(
			title="Horká půda", author="Václava Molcarová",
		)
		assert em is not None
		# Anchor identity + its own fields stay.
		assert em.source == "abs_czech"
		assert em.title == "Horká půda"
		assert em.publisher == "Triton"
		assert em.year == 2016
		# Missing fields filled from the same-book secondary.
		assert em.isbn == "9788073879570"
		assert em.series == "Agent JFK"
		assert em.genres == ["Sci-fi"]

	def test_anchor_values_are_never_overwritten(self, monkeypatch):
		"""Editions of the same work disagree on the year/ISBN — the anchor's
		value wins, the secondary's is not written over it."""
		abs_hit = EnrichedMeta(title="Ponorka", authors=["Tom Clancy"], source="abs_czech", year=2005, isbn="1111111111111")
		dbk_hit = EnrichedMeta(title="Ponorka", authors=["Tom Clancy"], source="databazeknih", year=1988, isbn="9788020612656")
		monkeypatch.setattr(enrichers, "lookup_abs_czech", lambda **kw: abs_hit)
		monkeypatch.setattr(enrichers, "lookup_databazeknih", lambda **kw: dbk_hit)
		em = _enricher(databazeknih_enabled=True, abs_czech_url="http://p:8000").lookup(
			title="Ponorka", author="Tom Clancy",
		)
		assert em is not None
		assert em.year == 2005
		assert em.isbn == "1111111111111"

	def test_genres_union_and_authors_fill_only_when_empty(self, monkeypatch):
		abs_hit = EnrichedMeta(title="1984", source="abs_czech", genres=["antiutopie"])
		dbk_hit = EnrichedMeta(
			title="1984", authors=["George Orwell"], source="databazeknih", genres=["Romány", "antiutopie"],
		)
		monkeypatch.setattr(enrichers, "lookup_abs_czech", lambda **kw: abs_hit)
		monkeypatch.setattr(enrichers, "lookup_databazeknih", lambda **kw: dbk_hit)
		em = _enricher(databazeknih_enabled=True, abs_czech_url="http://p:8000").lookup(
			title="1984", author="George Orwell",
		)
		assert em is not None
		assert em.authors == ["George Orwell"]  # anchor had none → filled
		assert em.genres == ["antiutopie", "Romány"]  # union, order-stable, deduped

	def test_merged_result_is_cached(self, tmp_path, monkeypatch):
		"""The merged EnrichedMeta round-trips the SQLite cache — a second
		Enricher is served the merge without re-querying anything."""
		abs_hit = EnrichedMeta(title="1984", authors=["George Orwell"], source="abs_czech", year=2003)
		dbk_hit = EnrichedMeta(title="1984", authors=["George Orwell"], source="databazeknih", isbn="9788073099993")
		monkeypatch.setattr(enrichers, "lookup_abs_czech", lambda **kw: abs_hit)
		monkeypatch.setattr(enrichers, "lookup_databazeknih", lambda **kw: dbk_hit)
		cache = tmp_path / "cache.db"
		kw = dict(databazeknih_enabled=True, abs_czech_url="http://p:8000",
				  openlibrary_enabled=False, google_books_enabled=False)
		em1 = Enricher(cache_db=cache, **kw).lookup(title="1984", author="George Orwell")
		assert em1 is not None and em1.isbn == "9788073099993" and em1.year == 2003

		monkeypatch.setattr(enrichers, "lookup_abs_czech", lambda **kw: (_ for _ in ()).throw(AssertionError("re-queried")))
		monkeypatch.setattr(enrichers, "lookup_databazeknih", lambda **kw: (_ for _ in ()).throw(AssertionError("re-queried")))
		em2 = Enricher(cache_db=cache, **kw).lookup(title="1984", author="George Orwell")
		assert em2 is not None
		assert em2.isbn == "9788073099993"
		assert em2.year == 2003


class TestDifferentBookIsNeverMerged:
	def test_conflicting_author_secondary_dropped(self, monkeypatch):
		"""Storefront keyword search shape: same title, DIFFERENT author. Not
		one field may be taken from it (here: the cover)."""
		abs_hit = EnrichedMeta(title="Horká půda", authors=["Václava Molcarová"], source="abs_czech", year=2016)
		other_book = EnrichedMeta(
			title="Horká půda", authors=["Thomas Enger"], source="databazeknih",
			publisher="MOBA", cover_url="https://dk/other.jpg", isbn="2222222222222",
		)
		monkeypatch.setattr(enrichers, "lookup_abs_czech", lambda **kw: abs_hit)
		monkeypatch.setattr(enrichers, "lookup_databazeknih", lambda **kw: other_book)
		em = _enricher(databazeknih_enabled=True, abs_czech_url="http://p:8000").lookup(
			title="Horká půda", author="Václava Molcarová",
		)
		assert em is not None
		assert em.source == "abs_czech"
		assert em.isbn is None
		assert em.publisher is None
		assert em.cover_url is None

	def test_title_similar_other_work_dropped(self, monkeypatch):
		"""A merely title-similar row of a different work ('Miami Vice Horká
		půda Trpká příchuť pomsty') never reaches the merge."""
		abs_hit = EnrichedMeta(title="Horká půda", authors=["Václava Molcarová"], source="abs_czech")
		other = EnrichedMeta(title="Miami Vice Horká půda Trpká příchuť pomsty", source="databazeknih", year=1996)
		monkeypatch.setattr(enrichers, "lookup_abs_czech", lambda **kw: abs_hit)
		monkeypatch.setattr(enrichers, "lookup_databazeknih", lambda **kw: other)
		em = _enricher(databazeknih_enabled=True, abs_czech_url="http://p:8000").lookup(
			title="Horká půda", author="Václava Molcarová",
		)
		assert em is not None
		assert em.year is None

	def test_authorless_secondary_needs_near_exact_title(self, monkeypatch):
		"""Rozhlas-style author-less rows: a merely title-similar one (~< 90)
		is junk risk and dropped even when the anchor has no author either."""
		abs_hit = EnrichedMeta(title="Válka s mloky", source="abs_czech")
		junk = EnrichedMeta(title="Nová válka s mloky", source="databazeknih", cover_url="https://dk/huge.jpg")
		monkeypatch.setattr(enrichers, "lookup_abs_czech", lambda **kw: abs_hit)
		monkeypatch.setattr(enrichers, "lookup_databazeknih", lambda **kw: junk)
		em = _enricher(databazeknih_enabled=True, abs_czech_url="http://p:8000").lookup(title="Válka s mloky")
		assert em is not None
		assert em.cover_url is None


class TestIsbnFanOut:
	def test_pure_isbn_merge_skips_title_gate(self, monkeypatch):
		"""Every source in an ISBN-anchored fan-out answered the SAME exact
		key — legitimately different title spellings ('1984' vs 'Nineteen
		Eighty-Four') must not block the fill-only merge."""
		dk_hit = EnrichedMeta(title="1984", authors=["George Orwell"], source="databazeknih", isbn="9788073099993")
		ol_hit = EnrichedMeta(title="Nineteen Eighty-Four", source="openlibrary", publisher="Penguin")
		monkeypatch.setattr(enrichers, "lookup_databazeknih_isbn", lambda isbn: dk_hit)
		monkeypatch.setattr(enrichers, "lookup_openlibrary_isbn", lambda isbn: ol_hit)
		em = _enricher(databazeknih_enabled=True, openlibrary_enabled=True).lookup(isbn="9788073099993")
		assert em is not None
		assert em.source == "databazeknih"
		assert em.title == "1984"  # anchor's title is never overwritten
		assert em.publisher == "Penguin"  # filled from the same-edition secondary

	def test_title_riding_on_isbn_query_still_gated(self, monkeypatch):
		"""A query carrying BOTH isbn and title runs title sources too — those
		must pass the same-book gate like any title-path secondary."""
		dk_hit = EnrichedMeta(title="1984", authors=["George Orwell"], source="databazeknih", isbn="9788073099993")
		other = EnrichedMeta(title="Encyklopedie hub prakticky", authors=["Jan Borovička"], source="databazeknih")
		monkeypatch.setattr(enrichers, "lookup_databazeknih_isbn", lambda isbn: dk_hit)
		monkeypatch.setattr(enrichers, "lookup_databazeknih", lambda **kw: other)
		em = _enricher(databazeknih_enabled=True).lookup(title="1984", author="George Orwell", isbn="9788073099993")
		assert em is not None
		assert em.source == "databazeknih"
		assert em.title == "1984"


class TestFanOutRobustness:
	def test_raising_source_is_a_none(self, monkeypatch):
		"""A dead source must not fail the lookup — its task yields None and
		the remaining sources still anchor/merge."""
		monkeypatch.setattr(enrichers, "lookup_abs_czech", lambda **kw: (_ for _ in ()).throw(RuntimeError("provider down")))
		dbk_hit = EnrichedMeta(title="1984", authors=["George Orwell"], source="databazeknih", year=2003)
		monkeypatch.setattr(enrichers, "lookup_databazeknih", lambda **kw: dbk_hit)
		em = _enricher(databazeknih_enabled=True, abs_czech_url="http://p:8000").lookup(
			title="1984", author="George Orwell",
		)
		assert em is not None
		assert em.source == "databazeknih"

	def test_no_applicable_sources_returns_none(self):
		assert _enricher().lookup(title="1984", author="George Orwell") is None


@pytest.mark.parametrize("tab", ["\t", "  "])
def test_dbk_series_parse_from_detail_html(tab):
	"""The DBK series box contributes the series NAME (no index — the page
	carries none)."""
	html = (
		'<html><head><script type="application/ld+json">'
		'{"name": "Horká půda", "isbn": "9788073879570", "author": {"name": "Václava Molcarová"}}'
		"</script></head><body>"
		'<div class="bookRightDiv"><div class="orangeBoxLight">'
		f'<div class="lora book_detail_serie_info"><p class="inline">{tab}'
		f"<a class='odright_pet' href='/serie/agent-jfk-11342?lang=cz' title='Agent JFK'>{tab}Agent JFK{tab}</a> série</p>"
		"</div></div></div>"
		"</body></html>"
	)
	em = enrichers._parse_databazeknih_detail(html)
	assert em is not None
	assert em.series == "Agent JFK"
	assert em.series_index is None  # name only — the detail page carries no index


def test_dbk_series_absent_leaves_series_none():
	html = (
		'<html><head><script type="application/ld+json">'
		'{"name": "1984", "isbn": "9788073099993", "author": {"name": "George Orwell"}}'
		"</script></head><body><p>no series box</p></body></html>"
	)
	em = enrichers._parse_databazeknih_detail(html)
	assert em is not None
	assert em.series is None
