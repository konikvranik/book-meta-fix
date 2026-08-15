"""Unit tests for the legie.info scraper enricher.

Uses fixtures captured from the real site (a povídka detail page, a direct-hit
search response, a multi-result search list, and a no-results page) and
monkeypatches the HTTP getter so no network calls are made.
"""
from __future__ import annotations

from pathlib import Path

from book_meta_fix import enrichers
from book_meta_fix.enrichers import EnrichedMeta, Enricher, lookup_legie

FIX = Path(__file__).parent / "fixtures" / "legie"


def _load(name: str) -> str:
	return (FIX / name).read_text(encoding="utf-8")


class TestParseLegieDetail:
	def test_parses_povidka_detail(self):
		from book_meta_fix.enrichers import _parse_legie_detail

		em = _parse_legie_detail(_load("detail_povidka.html"))
		assert em is not None
		assert em.source == "legie"
		assert em.title == "Ženská intuice"
		assert em.authors == ["Isaac Asimov"]
		# Universe/world is exposed as the series.
		assert em.series == "Nadace"
		# Original (foreign) title is stashed in description for LLM/cross-check.
		assert em.description is not None
		assert em.description.startswith("originál: Feminine Intuition")
		# legie.info does not carry CZ-edition ISBN/Year/Publisher.
		assert em.isbn is None
		assert em.year is None
		assert em.publisher is None

	def test_returns_none_when_no_title(self):
		from book_meta_fix.enrichers import _parse_legie_detail

		# A bare search form / 'no results' page has no nazev headline.
		assert _parse_legie_detail("<html><body><h1>Vyhledávání</h1></body></html>") is None

	def test_no_results_search_page_is_not_a_detail(self):
		from book_meta_fix.enrichers import _parse_legie_detail

		assert _parse_legie_detail(_load("search_none.html")) is None


class TestLegiePickFromList:
	def test_picks_best_title_match(self):
		from book_meta_fix.enrichers import _legie_pick_from_list

		href = _legie_pick_from_list(_load("search_list.html"), "Zkáza")
		assert href is not None
		assert href.startswith("kniha/") or href.startswith("povidka/")

	def test_returns_none_when_nothing_matches(self):
		from book_meta_fix.enrichers import _legie_pick_from_list

		# A title nothing on the list fuzzy-matches (>= 70).
		assert _legie_pick_from_list(_load("search_list.html"), "Bla XYZ Úplně Nonexist") is None


class TestLookupLegie:
	def test_direct_hit_returns_detail(self, monkeypatch):
		"""A strong/unique match returns the work's detail page directly from the
		search endpoint → parsed in a single HTTP call."""
		monkeypatch.setattr(enrichers, "_http_get_html", lambda url, **kw: _load("search_direct.html"))
		em = lookup_legie(title="Ženská intuice", author="Isaac Asimov")
		assert em is not None
		assert em.source == "legie"
		assert em.title == "Ženská intuice"
		assert em.authors == ["Isaac Asimov"]

	def test_list_path_fetches_detail_page(self, monkeypatch):
		"""When the search returns a results list, the best fuzzy match's detail
		page is fetched and parsed."""
		calls: list[str] = []

		def fake_get(url, **kw):
			calls.append(url)
			if "search_text=" in url:
				return _load("search_list.html")  # a list, not a detail
			if "/kniha/" in url or "/povidka/" in url:
				return _load("detail_povidka.html")  # the chosen result's detail
			return None

		monkeypatch.setattr(enrichers, "_http_get_html", fake_get)
		em = lookup_legie(title="Zkáza")
		assert em is not None
		assert em.source == "legie"
		# Two calls: the search, then the chosen detail page.
		assert len(calls) == 2
		assert "search_text=" in calls[0]
		assert calls[1].startswith("https://www.legie.info/")

	def test_no_results_returns_none(self, monkeypatch):
		monkeypatch.setattr(enrichers, "_http_get_html", lambda url, **kw: _load("search_none.html"))
		assert lookup_legie(title="Qwxzzy Nonexistent") is None

	def test_list_with_no_fuzzy_match_returns_none(self, monkeypatch):
		"""A results list where nothing fuzzy-matches the title (>= 70) → None,
		without fetching a wrong book's detail page."""
		detail_fetched = [False]

		def fake_get(url, **kw):
			if "search_text=" in url:
				return _load("search_list.html")
			detail_fetched[0] = True
			return None

		monkeypatch.setattr(enrichers, "_http_get_html", fake_get)
		assert lookup_legie(title="Bla XYZ Úplně Nonexist") is None
		assert detail_fetched == [False]  # no detail fetched for a non-matching list

	def test_http_failure_returns_none(self, monkeypatch):
		monkeypatch.setattr(enrichers, "_http_get_html", lambda url, **kw: None)
		assert lookup_legie(title="Ženská intuice") is None

	def test_detail_fetch_failure_returns_none(self, monkeypatch):
		def fake_get(url, **kw):
			if "search_text=" in url:
				return _load("search_list.html")
			return None  # the chosen detail page fails to load

		monkeypatch.setattr(enrichers, "_http_get_html", fake_get)
		assert lookup_legie(title="Zkáza") is None


class TestEnricherLegieIntegration:
	def test_legie_disabled_by_default(self):
		assert Enricher().legie_enabled is False

	def test_legie_enabled_flag(self):
		assert Enricher(legie_enabled=True).legie_enabled is True

	def test_lookup_uses_legie_when_enabled(self, monkeypatch):
		"""With legie enabled (and databazeknih disabled), legie.info is consulted
		and its result returned."""
		called = [0]

		def fake_legie(*, title, author=None):
			called[0] += 1
			return EnrichedMeta(title=title, source="legie", authors=["Isaac Asimov"])

		monkeypatch.setattr(enrichers, "lookup_legie", fake_legie)
		e = Enricher(legie_enabled=True, openlibrary_enabled=False, google_books_enabled=False)
		em = e.lookup(title="Ženská intuice")
		assert em is not None
		assert em.source == "legie"
		assert called[0] == 1

	def test_lookup_skips_legie_when_disabled(self, monkeypatch):
		called = [0]

		def fake_legie(*, title, author=None):
			called[0] += 1
			return EnrichedMeta(title=title, source="legie")

		monkeypatch.setattr(enrichers, "lookup_legie", fake_legie)
		e = Enricher(legie_enabled=False, openlibrary_enabled=False, google_books_enabled=False)
		assert e.lookup(title="Ženská intuice") is None
		assert called == [0]

	def test_databazeknih_tried_before_legie(self, monkeypatch):
		"""legie is a fallback AFTER databazeknih: a dbk hit must short-circuit
		and legie must not be called."""
		order: list[str] = []

		def fake_dk(*, title, author=None, year=None):
			order.append("databazeknih")
			return EnrichedMeta(title=title, source="databazeknih")

		def fake_legie(*, title, author=None):
			order.append("legie")
			return EnrichedMeta(title=title, source="legie")

		monkeypatch.setattr(enrichers, "lookup_databazeknih", fake_dk)
		monkeypatch.setattr(enrichers, "lookup_legie", fake_legie)
		e = Enricher(databazeknih_enabled=True, legie_enabled=True,
					 openlibrary_enabled=False, google_books_enabled=False)
		em = e.lookup(title="1984")
		assert em is not None
		assert em.source == "databazeknih"
		assert order == ["databazeknih"]  # legie never reached
