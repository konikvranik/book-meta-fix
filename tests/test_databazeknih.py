"""Unit tests for the databazeknih.cz scraper enricher.

Uses fixtures captured from the real site (search results + book detail page)
and monkeypatches the HTTP getter so no network calls are made.
"""
from __future__ import annotations

from pathlib import Path

from book_meta_fix import enrichers
from book_meta_fix.enrichers import EnrichedMeta, Enricher, lookup_databazeknih, lookup_databazeknih_isbn

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

	def test_lookup_databazeknih_isbn_anchors_over_title_sources(self, monkeypatch):
		"""With an ISBN + title identity, the databazeknih ISBN hit (exact) is
		the ANCHOR of the parallel fan-out's merged result. All applicable
		sources run, but the anchor's identity fields win."""
		calls: list[str] = []

		def fake_dk_isbn(isbn):
			calls.append("databazeknih_isbn")
			return EnrichedMeta(title="1984", source="databazeknih", genres=["Romány"])

		def fake_dk(*, title, author=None, year=None):
			calls.append("databazeknih")
			return None  # title lookup misses

		def fake_ol_isbn(isbn):
			calls.append("openlibrary_isbn")
			return None

		monkeypatch.setattr(enrichers, "lookup_databazeknih_isbn", fake_dk_isbn)
		monkeypatch.setattr(enrichers, "lookup_databazeknih", fake_dk)
		monkeypatch.setattr(enrichers, "lookup_openlibrary_isbn", fake_ol_isbn)
		monkeypatch.setattr(enrichers, "lookup_openlibrary_title", lambda t, author=None: None)
		monkeypatch.setattr(enrichers, "lookup_google_books_isbn", lambda isbn: None)

		e = Enricher(databazeknih_enabled=True)
		em = e.lookup(title="1984", author="George Orwell", isbn="9788073099993")
		assert em is not None
		assert em.source == "databazeknih"
		assert em.genres == ["Romány"]
		# The fan-out consulted the ISBN source and the title source alike.
		assert set(calls) == {"databazeknih_isbn", "databazeknih", "openlibrary_isbn"}

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
		e = Enricher(cache_db=cache, databazeknih_enabled=True,
					 openlibrary_enabled=False, google_books_enabled=False)
		em1 = e.lookup(title="1984", author="George Orwell")
		assert em1 is not None and em1.genres == ["Sci-fi", "antiutopie"]

		e2 = Enricher(cache_db=cache, databazeknih_enabled=True,
					  openlibrary_enabled=False, google_books_enabled=False)
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

	def test_negative_expires_after_ttl(self, tmp_path, monkeypatch):
		"""A cached __NOT_FOUND__ older than negative_ttl_sec is re-queried, so a
		transient miss (or a pre-fix identity) gets a second chance instead of
		being pinned forever."""
		class _Clock:
			def __init__(self) -> None:
				self.t = 1_000_000.0

			def time(self) -> float:
				return self.t

		clock = _Clock()
		monkeypatch.setattr(enrichers, "time", clock)

		call_count = [0]

		def fake_ol_isbn(isbn):
			call_count[0] += 1
			return None

		monkeypatch.setattr(enrichers, "lookup_openlibrary_isbn", fake_ol_isbn)
		monkeypatch.setattr(enrichers, "lookup_google_books_isbn", lambda isbn: None)

		cache = tmp_path / "cache.db"
		kwargs = dict(openlibrary_enabled=True, google_books_enabled=True, databazeknih_enabled=False, negative_ttl_sec=100)

		# First lookup at t: miss, caches __NOT_FOUND__ (cached_at = t).
		e1 = Enricher(cache_db=cache, **kwargs)
		assert e1.lookup(isbn="9788073099992") is None
		assert call_count[0] == 1
		e1.close()

		# Within TTL: served from cache, no re-query.
		clock.t += 50
		e2 = Enricher(cache_db=cache, **kwargs)
		assert e2.lookup(isbn="9788073099992") is None
		assert call_count[0] == 1
		e2.close()

		# Past TTL: treated as a miss -> re-query -> re-cached.
		clock.t += 200
		e3 = Enricher(cache_db=cache, **kwargs)
		assert e3.lookup(isbn="9788073099992") is None
		assert call_count[0] == 2
		e3.close()

	def test_negative_never_expires_when_ttl_zero(self, tmp_path, monkeypatch):
		"""negative_ttl_sec <= 0 keeps the old forever-unchanging behaviour."""
		class _Clock:
			def __init__(self) -> None:
				self.t = 1_000_000.0

			def time(self) -> float:
				return self.t

		clock = _Clock()
		monkeypatch.setattr(enrichers, "time", clock)

		call_count = [0]

		def fake_ol_isbn(isbn):
			call_count[0] += 1
			return None

		monkeypatch.setattr(enrichers, "lookup_openlibrary_isbn", fake_ol_isbn)
		monkeypatch.setattr(enrichers, "lookup_google_books_isbn", lambda isbn: None)

		cache = tmp_path / "cache.db"
		kwargs = dict(openlibrary_enabled=True, google_books_enabled=True, databazeknih_enabled=False, negative_ttl_sec=0)

		e1 = Enricher(cache_db=cache, **kwargs)
		assert e1.lookup(isbn="9788073099992") is None
		e1.close()

		clock.t += 10_000_000  # far in the future
		e2 = Enricher(cache_db=cache, **kwargs)
		assert e2.lookup(isbn="9788073099992") is None  # still cached
		assert call_count[0] == 1  # never re-queried
		e2.close()


class TestLookupDatabazeknihSeries:
	"""lookup_databazeknih_series: identify a volume by series name + order.
	The serie page lists the volumes as schema.org hasPart microdata in
	reading order — volume N is parts[N-1]. No network: _http_get_html is
	routed to inline fixture HTML by URL."""

	@staticmethod
	def _serie_page(names: list[str], h1: str = "Ocelová krysa") -> str:
		parts = "".join(
			f'<div itemprop="hasPart" itemscope itemtype="https://schema.org/Book">'
			f'<meta itemprop="name" content="{name}">'
			f'<link itemprop="url" href="/prehled-knihy/{slug}-{100 + i}">'
			f"</div>"
			for i, (name, slug) in enumerate(names)
		)
		return f"<html><body><h1>{h1}<em>knihy</em></h1>{parts}</body></html>"

	_RAT_NAMES = [
		("Ocelová krysa", "ocelova-krysa"),
		("Ocelová krysa se mstí", "se-msti"),
		("Krysa z nerez oceli", "krysa-z-nerez"),
		("Ocelová krysa jde po tobě!", "jde-po-tobe"),
		("Ocelová krysa prezidentem", "prezidentem"),
		("Zrození ocelové krysy", "zrozeni"),
		("Ocelová krysa rukuje", "rukuje"),
	]

	def _route(self, monkeypatch, serie_page: str, search_html: str | None = None, detail: str | None = None):
		search_html = search_html or '<html><body><a href="/serie/ocelova-krysa-503">Ocelová krysa</a></body></html>'
		detail = detail or _load("detail_1984.html")
		state = {"calls": []}

		def fake_get(url, **kw):
			state["calls"].append(url)
			if "/vyhledavani/serie" in url:
				return search_html
			if "/serie/" in url:
				return serie_page
			if "/prehled-knihy/" in url:
				return detail
			return None

		monkeypatch.setattr(enrichers, "_http_get_html", fake_get)
		return state

	def test_volume_five_resolves_to_fifth_part(self, monkeypatch):
		from book_meta_fix.enrichers import lookup_databazeknih_series

		self._route(monkeypatch, self._serie_page(self._RAT_NAMES))
		em = lookup_databazeknih_series(series="Ocelová krysa", index=5)
		# The detail fixture (1984) proves the THIRD call hit the right path.
		assert em is not None and em.title == "1984"
		em2 = lookup_databazeknih_series(series="Ocelová krysa", index="5")
		assert em2 is not None  # string index (series_entry_pair's form) works

	def test_index_out_of_range_returns_none(self, monkeypatch):
		from book_meta_fix.enrichers import lookup_databazeknih_series

		self._route(monkeypatch, self._serie_page(self._RAT_NAMES))
		assert lookup_databazeknih_series(series="Ocelová krysa", index=9) is None
		assert lookup_databazeknih_series(series="Ocelová krysa", index=0) is None

	def test_serie_name_mismatch_rejected(self, monkeypatch):
		"""The serie search's first hit fuzzily disagrees with the queried
		name — a near-titled different series would silently serve wrong
		volumes."""
		from book_meta_fix.enrichers import lookup_databazeknih_series

		self._route(monkeypatch, self._serie_page(self._RAT_NAMES, h1="Agent JFK"))
		assert lookup_databazeknih_series(series="Ocelová krysa", index=1) is None

	def test_no_serie_link_returns_none(self, monkeypatch):
		from book_meta_fix.enrichers import lookup_databazeknih_series

		self._route(monkeypatch, self._serie_page(self._RAT_NAMES), search_html="<html><body>nic</body></html>")
		assert lookup_databazeknih_series(series="Ocelová krysa", index=1) is None

	def test_enricher_lookup_series_cached(self, tmp_path, monkeypatch):
		"""Enricher.lookup_series caches under its own series: key — a second
		call is served from the cache, the scraper runs once."""
		state = self._route(monkeypatch, self._serie_page(self._RAT_NAMES))
		cache = tmp_path / "cache.db"
		e = Enricher(cache_db=cache, databazeknih_enabled=True, openlibrary_enabled=False, google_books_enabled=False)
		em1 = e.lookup_series(series="Ocelová krysa", index=5)
		assert em1 is not None
		n_calls = len(state["calls"])
		assert n_calls == 3  # serie search + serie page + detail
		em2 = e.lookup_series(series="Ocelová krysa", index=5)
		assert em2 is not None and em2.title == em1.title
		assert len(state["calls"]) == n_calls  # cache hit, no re-scrape
		# Disabled source never queries.
		e.close()
		e_off = Enricher(cache_db=None, databazeknih_enabled=False)
		assert e_off.lookup_series(series="Ocelová krysa", index=5) is None
