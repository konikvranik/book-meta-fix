"""Unit tests for the databazeknih.cz scraper enricher.

Uses fixtures captured from the real site (search results + book detail page)
and monkeypatches the HTTP getter so no network calls are made.
"""
from __future__ import annotations

from pathlib import Path

from book_meta_fix import enrichers
from book_meta_fix.enrichers import Enricher, EnrichedMeta, lookup_databazeknih, lookup_databazeknih_isbn

FIX = Path(__file__).parent / "fixtures" / "databazeknih"


def _load(name: str) -> str:
	return (FIX / name).read_text(encoding="utf-8")


class TestParseJsonLd:
	def test_parses_book_metadata(self):
		from book_meta_fix.enrichers import _parse_jsonld

		html = _load("detail_1984.html")
		ld = _parse_jsonld(html)
		assert ld is not None
		assert ld["@type"] == "Book"
		assert ld["name"] == "1984"
		# Unicode escapes (\u002D = '-') decoded
		assert ld["isbn"] == "80-7309-999-3"
		assert ld["author"][0]["name"] == "George Orwell"
		assert ld["inLanguage"] == "cs"
		assert ld["genre"] == ["Literatura světová", "Romány", "Sci-fi"]
		assert ld["publisher"][0]["name"] == "Levné knihy"

	def test_returns_none_when_no_jsonld(self):
		from book_meta_fix.enrichers import _parse_jsonld

		assert _parse_jsonld("<html><body>no json-ld here</body></html>") is None

	def test_returns_none_on_malformed_json(self):
		from book_meta_fix.enrichers import _parse_jsonld

		html = '<script type="application/ld+json">{not valid json}</script>'
		assert _parse_jsonld(html) is None


class TestSearchDatabazeknih:
	def test_finds_best_match_by_title(self, monkeypatch):
		from book_meta_fix.enrichers import _search_databazeknih

		monkeypatch.setattr(enrichers, "_http_get_html", lambda url, **kw: _load("search_1984.html"))
		path = _search_databazeknih("1984", "George Orwell")
		assert path == "/prehled-knihy/1984-283"

	def test_rejects_low_fuzzy_match(self, monkeypatch):
		from book_meta_fix.enrichers import _search_databazeknih

		# "1984" search results won't fuzzy-match "The Great Gatsby" well
		monkeypatch.setattr(enrichers, "_http_get_html", lambda url, **kw: _load("search_1984.html"))
		path = _search_databazeknih("Alicina dobrodruzstvi v risi divu", None)
		assert path is None

	def test_returns_none_on_http_failure(self, monkeypatch):
		from book_meta_fix.enrichers import _search_databazeknih

		monkeypatch.setattr(enrichers, "_http_get_html", lambda url, **kw: None)
		assert _search_databazeknih("1984", None) is None

	def test_no_author_still_searches(self, monkeypatch):
		from book_meta_fix.enrichers import _search_databazeknih

		monkeypatch.setattr(enrichers, "_http_get_html", lambda url, **kw: _load("search_1984.html"))
		path = _search_databazeknih("1984", None)
		assert path == "/prehled-knihy/1984-283"

	def _editions_html(self) -> str:
		"""Two same-titled editions of one work, different years (2003, 2010)."""
		return (
			"<html><body>"
			"<p class='new'>"
			"<a class='new' type='book' href='/prehled-knihy/1984-v1-1'>1984</a>"
			"<br /><span class='pozn'>2003, George Orwell (p)</span></p>"
			"<p class='new'>"
			"<a class='new' type='book' href='/prehled-knihy/1984-v2-2'>1984</a>"
			"<br /><span class='pozn'>2010, George Orwell (p)</span></p>"
			"</body></html>"
		)

	def test_year_prefers_matching_edition(self, monkeypatch):
		"""With a target year, the edition published that year (±1) is chosen."""
		from book_meta_fix.enrichers import _search_databazeknih

		monkeypatch.setattr(enrichers, "_http_get_html", lambda url, **kw: self._editions_html())
		assert _search_databazeknih("1984", "George Orwell", year=2010) == "/prehled-knihy/1984-v2-2"
		assert _search_databazeknih("1984", "George Orwell", year=2003) == "/prehled-knihy/1984-v1-1"

	def test_year_no_match_falls_back_to_best_fuzzy(self, monkeypatch):
		"""No edition matches the target year → fall back to best fuzzy (same
		work, other edition) rather than rejecting — maximise autodetection."""
		from book_meta_fix.enrichers import _search_databazeknih

		monkeypatch.setattr(enrichers, "_http_get_html", lambda url, **kw: self._editions_html())
		path = _search_databazeknih("1984", "George Orwell", year=1990)
		# Both editions match the title equally; either is acceptable (no reject).
		assert path in ("/prehled-knihy/1984-v1-1", "/prehled-knihy/1984-v2-2")

	def test_no_year_picks_best_fuzzy(self, monkeypatch):
		from book_meta_fix.enrichers import _search_databazeknih

		monkeypatch.setattr(enrichers, "_http_get_html", lambda url, **kw: self._editions_html())
		path = _search_databazeknih("1984", "George Orwell")
		assert path in ("/prehled-knihy/1984-v1-1", "/prehled-knihy/1984-v2-2")


class TestLookupDatabazeknih:
	def test_full_lookup_returns_enriched_meta(self, monkeypatch):
		"""search returns the path, detail returns metadata + tags."""
		responses = {
			"search": _load("search_1984.html"),
			"detail": _load("detail_1984.html"),
		}

		def fake_get(url, **kw):
			if "/search?" in url:
				return responses["search"]
			if "/prehled-knihy/" in url:
				return responses["detail"]
			return None

		monkeypatch.setattr(enrichers, "_http_get_html", fake_get)
		em = lookup_databazeknih(title="1984", author="George Orwell")
		assert em is not None
		assert em.source == "databazeknih"
		assert em.title == "1984"
		assert em.authors == ["George Orwell"]
		# ISBN canonicalized (ISBN-10 -> ISBN-13)
		assert em.isbn is not None
		assert len(em.isbn) == 13
		assert em.publisher == "Levné knihy"
		assert em.language == "cs"
		assert em.cover_url is not None
		# Genres: JSON-LD broad categories first, then user tags
		assert "Literatura světová" in em.genres
		assert "Romány" in em.genres
		assert "Sci-fi" in em.genres
		# User tags appended after
		assert "totalitní stát" in em.genres
		assert "zfilmováno" in em.genres

	def test_returns_none_when_search_fails(self, monkeypatch):
		monkeypatch.setattr(enrichers, "_http_get_html", lambda url, **kw: None)
		assert lookup_databazeknih(title="1984", author="George Orwell") is None

	def test_returns_none_when_detail_fetch_fails(self, monkeypatch):
		def fake_get(url, **kw):
			if "/search?" in url:
				return _load("search_1984.html")
			return None  # detail fetch fails

		monkeypatch.setattr(enrichers, "_http_get_html", fake_get)
		assert lookup_databazeknih(title="1984", author="George Orwell") is None


class TestDatabazeknihIsbnLookup:
	def test_isbn_returns_direct_profile(self, monkeypatch):
		"""ISBN search returns the book's detail page directly → parsed in one
		HTTP call (no fuzzy title scoring needed)."""
		def fake_get(url, **kw):
			# ISBN search lands on the detail page directly.
			return _load("detail_1984.html")

		monkeypatch.setattr(enrichers, "_http_get_html", fake_get)
		em = lookup_databazeknih_isbn("9788073099993")
		assert em is not None
		assert em.source == "databazeknih"
		assert em.title == "1984"

	def test_isbn_no_results_returns_none(self, monkeypatch):
		"""A 'no results' page has no book JSON-LD → None."""
		monkeypatch.setattr(enrichers, "_http_get_html", lambda url, **kw: "<html>nenalezeno žádný výsledek</html>")
		assert lookup_databazeknih_isbn("0000000000") is None

	def test_isbn_network_failure_returns_none(self, monkeypatch):
		monkeypatch.setattr(enrichers, "_http_get_html", lambda url, **kw: None)
		assert lookup_databazeknih_isbn("9788073099993") is None


class TestEnricherLookupOrder:
	def test_databazeknih_disabled_by_default(self):
		e = Enricher()
		assert e.databazeknih_enabled is False

	def test_databazeknih_enabled_flag(self):
		e = Enricher(databazeknih_enabled=True)
		assert e.databazeknih_enabled is True

	def test_lookup_uses_databazeknih_first(self, monkeypatch):
		"""When enabled, databazeknih should be consulted before OpenLibrary."""
		calls: list[str] = []

		def fake_dk_isbn(isbn):
			calls.append("databazeknih_isbn")
			return None  # ISBN miss → fall through to title lookup

		def fake_dk(*, title, author=None, year=None):
			calls.append("databazeknih")
			return EnrichedMeta(title=title, source="databazeknih", genres=["Romány"])

		def fake_ol_isbn(isbn):
			calls.append("openlibrary_isbn")
			return None

		monkeypatch.setattr(enrichers, "lookup_databazeknih_isbn", fake_dk_isbn)
		monkeypatch.setattr(enrichers, "lookup_databazeknih", fake_dk)
		monkeypatch.setattr(enrichers, "lookup_openlibrary_isbn", fake_ol_isbn)

		e = Enricher(databazeknih_enabled=True)
		em = e.lookup(title="1984", author="George Orwell", isbn="9788073099993")
		assert em is not None
		assert em.source == "databazeknih"
		# ISBN lookup tried first (miss), then title lookup hits; OL never called.
		assert calls == ["databazeknih_isbn", "databazeknih"]

	def test_lookup_skips_databazeknih_when_disabled(self, monkeypatch):
		calls: list[str] = []

		def fake_dk(*, title, author=None, year=None):
			calls.append("databazeknih")
			return EnrichedMeta(title=title, source="databazeknih")

		monkeypatch.setattr(enrichers, "lookup_databazeknih", fake_dk)
		# Disable all sources so lookup returns None without network
		e = Enricher(databazeknih_enabled=False, openlibrary_enabled=False, google_books_enabled=False)
		em = e.lookup(title="1984", author="George Orwell")
		assert em is None
		assert calls == []  # DK never called

	def test_caching_persists_genres(self, tmp_path, monkeypatch):
		"""Genres must survive a cache round-trip (cache payload includes them)."""
		call_count = [0]

		def fake_dk(*, title, author=None, year=None):
			call_count[0] += 1
			return EnrichedMeta(title=title, source="databazeknih", genres=["Sci-fi", "antiutopie"])

		monkeypatch.setattr(enrichers, "lookup_databazeknih", fake_dk)

		cache = tmp_path / "cache.db"
		e = Enricher(cache_db=cache, databazeknih_enabled=True)
		em1 = e.lookup(title="1984", author="George Orwell")
		assert em1 is not None and em1.genres == ["Sci-fi", "antiutopie"]

		e2 = Enricher(cache_db=cache, databazeknih_enabled=True)
		em2 = e2.lookup(title="1984", author="George Orwell")
		assert em2 is not None
		assert em2.genres == ["Sci-fi", "antiutopie"]
		assert call_count[0] == 1  # second lookup served from cache

	def test_cached_negative_returns_none_not_sentinel_string(self, tmp_path, monkeypatch):
		"""Regression: a cached '__NOT_FOUND__' must return None, not the
		sentinel string. Previously `cached or None` returned "__NOT_FOUND__"
		(because a non-empty string is truthy), which then crashed callers
		expecting an EnrichedMeta (AttributeError: 'str' has no attribute
		'source')."""
		call_count = [0]

		def fake_ol_isbn(isbn):
			call_count[0] += 1
			return None  # always a miss

		monkeypatch.setattr(enrichers, "lookup_openlibrary_isbn", fake_ol_isbn)
		monkeypatch.setattr(enrichers, "lookup_google_books_isbn", lambda isbn: None)

		cache = tmp_path / "cache.db"
		# First lookup: miss, caches the negative as __NOT_FOUND__.
		e1 = Enricher(cache_db=cache, openlibrary_enabled=True, google_books_enabled=True, databazeknih_enabled=False)
		em1 = e1.lookup(isbn="9788073099992")
		assert em1 is None
		e1.close()

		# Second lookup: served from cache. MUST be None, not "__NOT_FOUND__".
		e2 = Enricher(cache_db=cache, openlibrary_enabled=True, google_books_enabled=True, databazeknih_enabled=False)
		em2 = e2.lookup(isbn="9788073099992")
		assert em2 is None
		assert em2 != "__NOT_FOUND__"
		# The source was not re-queried (cached negative short-circuits).
		assert call_count[0] == 1
