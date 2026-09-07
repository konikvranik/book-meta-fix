"""Unit tests for the self-hosted audiobookshelf_czech_metadata enricher.

The provider (github.com/stecik/audiobookshelf_czech_metadata) is a FastAPI
aggregator over ~17 CZ audiobook storefronts speaking Audiobookshelf's
custom-metadata-provider contract: GET {base}/search → {"matches": [...]}.
All HTTP is monkeypatched; no network calls are made.
"""
from __future__ import annotations

import pytest

from book_meta_fix import enrichers
from book_meta_fix.config import Config
from book_meta_fix.enrichers import Enricher, lookup_abs_czech

# A realistic ranked match list: the FIRST entry is a wrong-author hit (a
# storefront search is keyword-based), the SECOND is the right book. The
# picker must prefer the author-agreeing entry.
_MATCHES = {
	"matches": [
		{
			"title": "1984",
			"author": "Arthur C. Clarke",
			"narrator": "Someone Else",
			"publisher": "Jiné nakladatelství",
			"publishedYear": "2019",
			"description": "Jiná kniha.",
			"cover": "https://example.cz/jina.jpg",
			"genres": ["Jiné"],
			"language": "cs",
			"duration": 500,
		},
		{
			"title": "1984",
			"author": "George Orwell",
			"narrator": "Jan Vondráček",
			"publisher": "Audiostory",
			"publishedYear": "2021",
			"description": "Antiutopický román o všudypřítomném dohledu.",
			"cover": "https://example.cz/1984.jpg",
			"genres": ["Klasická díla", "Sci-fi a fantasy"],
			"language": "cs",
			"duration": 711,
		},
	]
}


class _Resp:
	"""Minimal requests.Response stand-in."""

	def __init__(self, status_code: int = 200, payload=None, bad_json: bool = False) -> None:
		self.status_code = status_code
		self._payload = payload
		self._bad_json = bad_json

	def json(self):
		if self._bad_json:
			raise ValueError("not json")
		return self._payload


class _Recorder:
	"""Captures _http_get kwargs and serves a canned response."""

	def __init__(self, resp: _Resp | None) -> None:
		self.resp = resp
		self.calls: list[dict] = []

	def __call__(self, url, params=None, timeout=15.0, rate=1.0, headers=None):
		self.calls.append({"url": url, "params": params, "timeout": timeout, "rate": rate, "headers": headers})
		return self.resp


class TestAbsSearchUrl:
	def test_plain_base(self):
		assert enrichers._abs_search_url("http://provider:8000") == "http://provider:8000/search"

	def test_trailing_slash(self):
		assert enrichers._abs_search_url("http://provider:8000/") == "http://provider:8000/search"

	def test_tolerates_search_suffix(self):
		# A base configured with the endpoint already appended (the ABS
		# convention is to configure the BASE url) must not get /search twice.
		assert enrichers._abs_search_url("http://provider:8000/search") == "http://provider:8000/search"


class TestPickAbsMatch:
	def test_prefers_author_agreeing_entry(self):
		pick = enrichers._pick_abs_match(_MATCHES["matches"], "1984", "George Orwell")
		assert pick is not None
		assert pick["author"] == "George Orwell"

	def test_no_author_takes_first(self):
		pick = enrichers._pick_abs_match(_MATCHES["matches"], "1984", None)
		assert pick is not None
		assert pick["author"] == "Arthur C. Clarke"

	def test_title_floor_rejects_weak_matches(self):
		assert enrichers._pick_abs_match(_MATCHES["matches"], "Úplně Jiná Nonexistující Kniha", "George Orwell") is None

	def test_empty_list(self):
		assert enrichers._pick_abs_match([], "1984", None) is None


class TestLookupAbsCzech:
	def test_maps_match_to_enriched_meta(self, monkeypatch):
		rec = _Recorder(_Resp(payload=_MATCHES))
		monkeypatch.setattr(enrichers, "_http_get", rec)
		em = lookup_abs_czech(base_url="http://provider:8000", title="1984", author="George Orwell")
		assert em is not None
		assert em.source == "abs_czech"
		assert em.title == "1984"
		assert em.authors == ["George Orwell"]
		assert em.publisher == "Audiostory"
		assert em.year == 2021
		assert em.language == "cs"
		assert em.description is not None and "Antiutopický" in em.description
		assert em.cover_url == "https://example.cz/1984.jpg"
		assert em.genres == ["Klasická díla", "Sci-fi a fantasy"]

	def test_request_shape_and_auth(self, monkeypatch):
		rec = _Recorder(_Resp(payload=_MATCHES))
		monkeypatch.setattr(enrichers, "_http_get", rec)
		lookup_abs_czech(base_url="http://provider:8000/", title="1984", author="George Orwell", token="s3cret")
		(c,) = rec.calls
		assert c["url"] == "http://provider:8000/search"
		assert c["params"] == {"query": "1984", "author": "George Orwell"}
		assert c["headers"] == {"Authorization": "Bearer s3cret"}
		# The aggregator fans out to storefront scrapers under its own budget
		# — it needs a longer timeout than the default 15 s.
		assert c["timeout"] > 15.0

	def test_no_token_no_header(self, monkeypatch):
		rec = _Recorder(_Resp(payload=_MATCHES))
		monkeypatch.setattr(enrichers, "_http_get", rec)
		lookup_abs_czech(base_url="http://provider:8000", title="1984")
		(c,) = rec.calls
		assert "author" not in c["params"]
		assert c["headers"] is None

	def test_splits_comma_joined_authors(self, monkeypatch):
		payload = {"matches": [{"title": "Bájevné příběhy", "author": "Jan Werich, Jiří Voskovec"}]}
		rec = _Recorder(_Resp(payload=payload))
		monkeypatch.setattr(enrichers, "_http_get", rec)
		em = lookup_abs_czech(base_url="http://p:8000", title="Bájevné příběhy")
		assert em is not None
		assert em.authors == ["Jan Werich", "Jiří Voskovec"]

	def test_http_error_returns_none(self, monkeypatch):
		monkeypatch.setattr(enrichers, "_http_get", _Recorder(_Resp(status_code=503)))
		assert lookup_abs_czech(base_url="http://p:8000", title="1984") is None

	def test_network_failure_returns_none(self, monkeypatch):
		monkeypatch.setattr(enrichers, "_http_get", _Recorder(None))
		assert lookup_abs_czech(base_url="http://p:8000", title="1984") is None

	def test_empty_matches_returns_none(self, monkeypatch):
		# The provider returns an empty list when every scraper times out.
		monkeypatch.setattr(enrichers, "_http_get", _Recorder(_Resp(payload={"matches": []})))
		assert lookup_abs_czech(base_url="http://p:8000", title="1984") is None

	def test_bad_json_returns_none(self, monkeypatch):
		monkeypatch.setattr(enrichers, "_http_get", _Recorder(_Resp(bad_json=True)))
		assert lookup_abs_czech(base_url="http://p:8000", title="1984") is None

	def test_weak_title_match_returns_none(self, monkeypatch):
		monkeypatch.setattr(enrichers, "_http_get", _Recorder(_Resp(payload=_MATCHES)))
		assert lookup_abs_czech(base_url="http://p:8000", title="Encyklopedie hub prakticky") is None


class TestHttpGetHeaderMerge:
	def test_headers_merge_over_defaults(self, monkeypatch):
		seen: dict = {}

		def fake_get(url, params=None, timeout=None, headers=None):
			seen["headers"] = headers
			return _Resp(payload={})

		monkeypatch.setattr(enrichers.requests, "get", fake_get)
		enrichers._http_get("http://abs-czech-test.local/x", rate=0.0, headers={"Authorization": "Bearer t"})
		assert seen["headers"]["Authorization"] == "Bearer t"
		assert seen["headers"]["User-Agent"] == enrichers.USER_AGENT
		assert seen["headers"]["Accept"] == "application/json"


class TestEnricherWiring:
	def test_consulted_after_databazeknih_isbn_before_title_sources(self, monkeypatch):
		"""With an ISBN + title identity: databazeknih ISBN (exact) first, then
		the provider; the databazeknih TITLE lookup never fires (provider hit)."""
		calls: list[str] = []
		monkeypatch.setattr(enrichers, "lookup_databazeknih_isbn", lambda isbn: calls.append("dk_isbn") or None)
		monkeypatch.setattr(enrichers, "lookup_databazeknih", lambda **kw: calls.append("dk_title") or None)
		monkeypatch.setattr(enrichers, "lookup_legie", lambda **kw: calls.append("legie") or None)
		monkeypatch.setattr(enrichers, "lookup_openlibrary_isbn", lambda isbn: calls.append("ol_isbn") or None)
		monkeypatch.setattr(
			enrichers, "lookup_abs_czech",
			lambda **kw: calls.append("abs_czech") or enrichers._abs_match_to_meta(_MATCHES["matches"][1]),
		)
		e = Enricher(databazeknih_enabled=True, legie_enabled=True, abs_czech_url="http://p:8000")
		em = e.lookup(title="1984", author="George Orwell", isbn="9788073099993")
		assert em is not None
		assert em.source == "abs_czech"
		assert calls == ["dk_isbn", "abs_czech"]

	def test_falls_through_to_databazeknih_title_on_miss(self, monkeypatch):
		calls: list[str] = []
		monkeypatch.setattr(enrichers, "lookup_abs_czech", lambda **kw: calls.append("abs_czech") or None)
		monkeypatch.setattr(enrichers, "lookup_databazeknih_isbn", lambda isbn: calls.append("dk_isbn") or None)
		monkeypatch.setattr(enrichers, "lookup_databazeknih", lambda **kw: calls.append("dk_title") or None)
		monkeypatch.setattr(enrichers, "lookup_openlibrary_isbn", lambda isbn: calls.append("ol_isbn") or None)
		monkeypatch.setattr(enrichers, "lookup_google_books_isbn", lambda isbn: calls.append("gb_isbn") or None)
		monkeypatch.setattr(enrichers, "lookup_openlibrary_title", lambda t, author=None: calls.append("ol_title") or None)
		e = Enricher(databazeknih_enabled=True, abs_czech_url="http://p:8000")
		assert e.lookup(title="1984", author="George Orwell", isbn="9788073099993") is None
		assert calls == ["dk_isbn", "abs_czech", "dk_title", "ol_isbn", "gb_isbn", "ol_title"]

	def test_disabled_without_url(self, monkeypatch):
		calls: list[str] = []

		def _fail(**kw):
			calls.append("abs_czech")
			return None

		monkeypatch.setattr(enrichers, "lookup_abs_czech", _fail)
		e = Enricher(openlibrary_enabled=False, google_books_enabled=False)
		assert e.lookup(title="1984", author="George Orwell") is None
		assert calls == []

	def test_provider_result_survives_cache_round_trip(self, tmp_path, monkeypatch):
		monkeypatch.setattr(
			enrichers, "lookup_abs_czech",
			lambda **kw: enrichers._abs_match_to_meta(_MATCHES["matches"][1]),
		)
		cache = tmp_path / "cache.db"
		e1 = Enricher(cache_db=cache, abs_czech_url="http://p:8000", openlibrary_enabled=False, google_books_enabled=False)
		em1 = e1.lookup(title="1984", author="George Orwell")
		assert em1 is not None and em1.source == "abs_czech" and em1.genres
		# Second Enricher on the same cache: served from SQLite, no HTTP.
		e2 = Enricher(cache_db=cache, abs_czech_url="http://p:8000", openlibrary_enabled=False, google_books_enabled=False)
		monkeypatch.setattr(enrichers, "lookup_abs_czech", lambda **kw: (_ for _ in ()).throw(AssertionError("HTTP after cache hit")))
		em2 = e2.lookup(title="1984", author="George Orwell")
		assert em2 is not None
		assert em2.source == "abs_czech"
		assert em2.genres == em1.genres


class TestConfig:
	@pytest.fixture(autouse=True)
	def _no_dotenv(self, monkeypatch):
		# The developer's real .env (walk-up loaded by from_env) may carry a
		# live BMF_ABS_CZECH_URL — isolate these tests from any .env file.
		monkeypatch.setattr("book_meta_fix.config.load_dotenv_walk_up", lambda **kw: None)

	def test_env_url_and_token(self, monkeypatch):
		monkeypatch.setenv("BMF_ABS_CZECH_URL", "http://provider:8000")
		monkeypatch.setenv("BMF_ABS_CZECH_TOKEN", "s3cret")
		cfg = Config.from_env()
		assert cfg.abs_czech_url == "http://provider:8000"
		assert cfg.abs_czech_token == "s3cret"

	def test_defaults_when_unset(self, monkeypatch):
		monkeypatch.delenv("BMF_ABS_CZECH_URL", raising=False)
		monkeypatch.delenv("BMF_ABS_CZECH_TOKEN", raising=False)
		cfg = Config.from_env()
		assert cfg.abs_czech_url == ""
		assert cfg.abs_czech_token is None

	def test_empty_token_means_none(self, monkeypatch):
		monkeypatch.setenv("BMF_ABS_CZECH_TOKEN", "")
		assert Config.from_env().abs_czech_token is None


class TestIdentitySources:
	def test_counts_as_online_source_for_identity_verified(self):
		# review_writer._identity_verified pre-fills the `verified` flag only
		# for _ONLINE_SOURCES hits — the provider is a real online
		# bibliographic source (aggregating storefront DBs), so it must count.
		from book_meta_fix import review_writer

		assert "abs_czech" in review_writer._ONLINE_SOURCES
