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
from book_meta_fix.enrichers import EnrichedMeta, Enricher, lookup_abs_czech

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

	def test_no_author_takes_first(self, monkeypatch):
		# Both entries tie on title; with probing disabled (None) the ranked
		# winner (provider order) keeps its place.
		monkeypatch.setattr(enrichers, "probe_image_size", lambda u, **kw: None)
		pick = enrichers._pick_abs_match(_MATCHES["matches"], "1984", None)
		assert pick is not None
		assert pick["author"] == "Arthur C. Clarke"

	def test_title_floor_rejects_weak_matches(self):
		assert enrichers._pick_abs_match(_MATCHES["matches"], "Úplně Jiná Nonexistující Kniha", "George Orwell") is None

	def test_empty_list(self):
		assert enrichers._pick_abs_match([], "1984", None) is None

	def test_conflicting_author_only_returns_none(self):
		# Storefront junk: title-similar rows whose author disagrees (incl.
		# the Rozhlas '?' author) are never returned, even when they are the
		# ONLY matches — a lookup miss falls through to the next source.
		matches = [
			{"title": "Válka s mloky", "author": "Martin Goffa"},
			{"title": "Válka s mloky", "author": "?"},
		]
		assert enrichers._pick_abs_match(matches, "Válka s mloky", "Karel Čapek") is None

	def test_authorless_match_needs_near_exact_title(self):
		# ~84 token_sort: title-similar but author-less → cannot be told
		# apart from junk → rejected. ~93: near-exact storefront row without
		# an author field → acceptable fallback.
		assert enrichers._pick_abs_match([{"title": "Nová válka s mloky"}], "Válka s mloky", "Karel Čapek") is None
		assert enrichers._pick_abs_match([{"title": "Válka s mloky 2"}], "Válka s mloky", "Karel Čapek") is not None

	def test_authored_match_beats_authorless_at_equal_score(self):
		matches = [
			{"title": "Válka s mloky"},  # author-less, provider-ranked first
			{"title": "Válka s mloky", "author": "Karel Čapek"},
		]
		pick = enrichers._pick_abs_match(matches, "Válka s mloky", "Karel Čapek")
		assert pick["author"] == "Karel Čapek"


class TestCoverPreference:
	"""Same book, several storefronts: near-tied matches are decided by cover
	resolution (probe_image_size), not the provider's arbitrary order."""

	@staticmethod
	def _sizes(monkeypatch, mapping: dict[str, tuple[int, int]]) -> list[str]:
		calls: list[str] = []
		monkeypatch.setattr(enrichers, "probe_image_size", lambda u, **kw: calls.append(u) or mapping.get(u))
		return calls

	@staticmethod
	def _same_book_matches() -> list[dict]:
		# Two storefront listings of the SAME book: identical title/author,
		# different cover hosts.
		return [
			{"title": "Harry Potter a Kámen mudrců", "author": "Joanne K. Rowlingová", "publisher": "Albatros", "cover": "https://cdn-a/small.jpg"},
			{"title": "Harry Potter a Kámen mudrců", "author": "Joanne K. Rowlingová", "publisher": "Albatros", "cover": "https://cdn-b/big.jpg"},
		]

	def test_higher_resolution_cover_wins(self, monkeypatch):
		calls = self._sizes(monkeypatch, {"https://cdn-a/small.jpg": (300, 300), "https://cdn-b/big.jpg": (1200, 1200)})
		pick = enrichers._pick_abs_match(self._same_book_matches(), "Harry Potter a Kámen mudrců", "Joanne K. Rowlingová")
		assert pick["cover"] == "https://cdn-b/big.jpg"
		assert len(calls) == 2  # both covers probed

	def test_unprobeable_winner_is_not_displaced(self, monkeypatch):
		# The ranked winner's cover fails to probe → unknown, NOT small → a
		# lesser KNOWN cover must not displace it.
		self._sizes(monkeypatch, {"https://cdn-a/small.jpg": None, "https://cdn-b/big.jpg": (300, 300)})
		pick = enrichers._pick_abs_match(self._same_book_matches(), "Harry Potter a Kámen mudrců", "Joanne K. Rowlingová")
		assert pick["cover"] == "https://cdn-a/small.jpg"

	def test_winner_without_cover_takes_first_probeable(self, monkeypatch):
		matches = [
			{"title": "Kniha", "author": "Autor", "cover": None},
			{"title": "Kniha", "author": "Autor", "cover": "https://cdn/b.jpg"},
		]
		self._sizes(monkeypatch, {"https://cdn/b.jpg": (200, 200)})
		pick = enrichers._pick_abs_match(matches, "Kniha", "Autor")
		assert pick["cover"] == "https://cdn/b.jpg"

	def test_far_score_is_not_a_tie(self, monkeypatch):
		matches = self._same_book_matches() + [
			{"title": "Harry Potter a Kámen mudrců (speciální edice s ilustracemi)", "author": "Joanne K. Rowlingová", "cover": "https://cdn-c/huge.jpg"},
		]
		calls = self._sizes(monkeypatch, {"https://cdn-c/huge.jpg": (4000, 4000)})
		pick = enrichers._pick_abs_match(matches, "Harry Potter a Kámen mudrců", "Joanne K. Rowlingová")
		# The huge cover belongs to a lower-scored match: never probed, never
		# picked. The winner fails to probe → fallback; the second tie cover
		# is not probed either (an unknown winner must not be displaced).
		assert pick["cover"] == "https://cdn-a/small.jpg"
		assert calls == ["https://cdn-a/small.jpg"]

	def test_probe_budget_is_capped(self, monkeypatch):
		matches = [{"title": "Kniha", "author": "Autor", "cover": f"https://cdn/{i}.jpg"} for i in range(7)]
		calls = self._sizes(monkeypatch, {f"https://cdn/{i}.jpg": (100 * i, 100 * i) for i in range(7)})
		enrichers._pick_abs_match(matches, "Kniha", "Autor")
		assert len(calls) <= enrichers._ABS_MAX_COVER_PROBES


class TestProbeImageSize:
	def test_reads_size_from_png_header(self, monkeypatch):
		import io

		from PIL import Image

		buf = io.BytesIO()
		Image.new("RGB", (7, 5)).save(buf, format="PNG")

		class _Stream:
			status_code = 200

			def iter_content(self, chunk_size):
				yield buf.getvalue()

			def close(self):
				pass

		monkeypatch.setattr(enrichers.requests, "get", lambda url, **kw: _Stream())
		assert enrichers.probe_image_size("https://x/cover.png") == (7, 5)

	def test_http_error_returns_none(self, monkeypatch):
		class _Stream:
			status_code = 404

			def iter_content(self, chunk_size):
				return iter(())
			def close(self):
				pass

		monkeypatch.setattr(enrichers.requests, "get", lambda url, **kw: _Stream())
		assert enrichers.probe_image_size("https://x/cover.png") is None

	def test_garbage_bytes_return_none(self, monkeypatch):
		class _Stream:
			status_code = 200

			def iter_content(self, chunk_size):
				yield b"not an image at all"

			def close(self):
				pass

		monkeypatch.setattr(enrichers.requests, "get", lambda url, **kw: _Stream())
		assert enrichers.probe_image_size("https://x/cover.png") is None


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
	def test_abs_czech_hit_anchors_parallel_fan_out(self, monkeypatch):
		"""With an ISBN + title identity every applicable source runs in
		parallel; the provider's hit ANCHORS the merged result (it outranks the
		databazeknih TITLE lookup in priority — the exact ISBN source only
		anchors when it hits)."""
		calls: list[str] = []
		monkeypatch.setattr(enrichers, "lookup_databazeknih_isbn", lambda isbn: calls.append("dk_isbn") or None)
		monkeypatch.setattr(enrichers, "lookup_databazeknih", lambda **kw: calls.append("dk_title") or None)
		monkeypatch.setattr(enrichers, "lookup_legie", lambda **kw: calls.append("legie") or None)
		monkeypatch.setattr(enrichers, "lookup_openlibrary_isbn", lambda isbn: calls.append("ol_isbn") or None)
		monkeypatch.setattr(enrichers, "lookup_openlibrary_title", lambda t, author=None: calls.append("ol_title") or None)
		monkeypatch.setattr(
			enrichers, "lookup_abs_czech",
			lambda **kw: calls.append("abs_czech") or enrichers._abs_match_to_meta(_MATCHES["matches"][1]),
		)
		e = Enricher(databazeknih_enabled=True, legie_enabled=True, abs_czech_url="http://p:8000",
					 openlibrary_enabled=True, google_books_enabled=False)
		em = e.lookup(title="1984", author="George Orwell", isbn="9788073099993")
		assert em is not None
		assert em.source == "abs_czech"
		# The title lookup ran too (parallel fan-out — no short-circuit), it
		# just cannot anchor over the provider hit.
		assert set(calls) == {"dk_isbn", "dk_title", "legie", "ol_isbn", "ol_title", "abs_czech"}

	def test_all_sources_miss_returns_none(self, monkeypatch):
		calls: list[str] = []
		monkeypatch.setattr(enrichers, "lookup_abs_czech", lambda **kw: calls.append("abs_czech") or None)
		monkeypatch.setattr(enrichers, "lookup_databazeknih_isbn", lambda isbn: calls.append("dk_isbn") or None)
		monkeypatch.setattr(enrichers, "lookup_databazeknih", lambda **kw: calls.append("dk_title") or None)
		monkeypatch.setattr(enrichers, "lookup_openlibrary_isbn", lambda isbn: calls.append("ol_isbn") or None)
		monkeypatch.setattr(enrichers, "lookup_google_books_isbn", lambda isbn: calls.append("gb_isbn") or None)
		monkeypatch.setattr(enrichers, "lookup_openlibrary_title", lambda t, author=None: calls.append("ol_title") or None)
		e = Enricher(databazeknih_enabled=True, abs_czech_url="http://p:8000")
		assert e.lookup(title="1984", author="George Orwell", isbn="9788073099993") is None
		assert set(calls) == {"dk_isbn", "abs_czech", "dk_title", "ol_isbn", "gb_isbn", "ol_title"}

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


class TestUpgradeCover:
	"""Cross-source cover upgrade: databazeknih ↔ abs_czech, strictly-better
	resolution wins, cover_url ONLY (identity-anchored fields never change)."""

	@staticmethod
	def _patch_sizes(monkeypatch, mapping: dict[str, tuple[int, int]]) -> None:
		monkeypatch.setattr(enrichers, "probe_image_size", lambda u, **kw: mapping.get(u))

	def test_abs_primary_takes_bigger_databazeknih_cover(self, monkeypatch):
		dk = EnrichedMeta(source="databazeknih", title="1984", authors=["George Orwell"], cover_url="https://dk/big.jpg")
		monkeypatch.setattr(enrichers, "lookup_databazeknih", lambda **kw: dk)
		self._patch_sizes(monkeypatch, {"https://abs/small.jpg": (300, 300), "https://dk/big.jpg": (1200, 1200)})
		primary = EnrichedMeta(source="abs_czech", title="1984", authors=["George Orwell"], publisher="OneHotBook", cover_url="https://abs/small.jpg")
		e = Enricher(databazeknih_enabled=True, abs_czech_url="http://p:8000")
		out = e.upgrade_cover(primary, title="1984", author="George Orwell")
		assert out.cover_url == "https://dk/big.jpg"
		# Only the cover changed — the identity-anchored metadata stayed.
		assert out.source == "abs_czech"
		assert out.publisher == "OneHotBook"

	def test_databazeknih_primary_takes_bigger_abs_cover(self, monkeypatch):
		abm = EnrichedMeta(source="abs_czech", title="1984", authors=["George Orwell"], cover_url="https://abs/big.jpg")
		calls: list[dict] = []
		monkeypatch.setattr(enrichers, "lookup_abs_czech", lambda **kw: calls.append(kw) or abm)
		self._patch_sizes(monkeypatch, {"https://dk/small.jpg": (200, 200), "https://abs/big.jpg": (1000, 1000)})
		primary = EnrichedMeta(source="databazeknih", title="1984", authors=["George Orwell"], cover_url="https://dk/small.jpg")
		e = Enricher(databazeknih_enabled=True, abs_czech_url="http://p:8000")
		out = e.upgrade_cover(primary, title="1984", author="George Orwell", year=2021)
		assert out.cover_url == "https://abs/big.jpg"
		# The alternative is queried by title+author (+year for databazeknih's
		# edition disambiguation) even when the primary was ISBN-anchored.
		assert calls and calls[0].get("title") == "1984"

	def test_smaller_alternative_is_ignored(self, monkeypatch):
		dk = EnrichedMeta(source="databazeknih", title="1984", authors=["George Orwell"], cover_url="https://dk/tiny.jpg")
		monkeypatch.setattr(enrichers, "lookup_databazeknih", lambda **kw: dk)
		self._patch_sizes(monkeypatch, {"https://abs/big.jpg": (1200, 1200), "https://dk/tiny.jpg": (100, 100)})
		primary = EnrichedMeta(source="abs_czech", title="1984", authors=["George Orwell"], cover_url="https://abs/big.jpg")
		e = Enricher(databazeknih_enabled=True, abs_czech_url="http://p:8000")
		assert e.upgrade_cover(primary, title="1984", author="George Orwell").cover_url == "https://abs/big.jpg"

	def test_alternative_without_cover_changes_nothing(self, monkeypatch):
		dk = EnrichedMeta(source="databazeknih", title="1984", authors=["George Orwell"], cover_url=None)
		monkeypatch.setattr(enrichers, "lookup_databazeknih", lambda **kw: dk)
		self._patch_sizes(monkeypatch, {"https://abs/small.jpg": (300, 300)})
		primary = EnrichedMeta(source="abs_czech", title="1984", authors=["George Orwell"], cover_url="https://abs/small.jpg")
		e = Enricher(databazeknih_enabled=True, abs_czech_url="http://p:8000")
		assert e.upgrade_cover(primary, title="1984").cover_url == "https://abs/small.jpg"

	def test_wrong_book_alternative_is_rejected(self, monkeypatch):
		# A different book's cover must never be glued on, however large.
		dk = EnrichedMeta(source="databazeknih", title="Encyklopedie hub prakticky", authors="Jan Borovička".split(), cover_url="https://dk/huge.jpg")
		monkeypatch.setattr(enrichers, "lookup_databazeknih", lambda **kw: dk)
		self._patch_sizes(monkeypatch, {"https://abs/small.jpg": (300, 300), "https://dk/huge.jpg": (4000, 4000)})
		primary = EnrichedMeta(source="abs_czech", title="1984", authors=["George Orwell"], cover_url="https://abs/small.jpg")
		e = Enricher(databazeknih_enabled=True, abs_czech_url="http://p:8000")
		assert e.upgrade_cover(primary, title="1984", author="George Orwell").cover_url == "https://abs/small.jpg"

	def test_conflicting_author_alternative_rejected_same_title(self, monkeypatch):
		# Same title, wrong author, huge cover — exactly the irrelevant-cover
		# shape the provider's keyword search produces.
		abm = EnrichedMeta(source="abs_czech", title="Válka s mloky", authors=["Martin Goffa"], cover_url="https://abs/huge.jpg")
		monkeypatch.setattr(enrichers, "lookup_abs_czech", lambda **kw: abm)
		self._patch_sizes(monkeypatch, {"https://dk/small.jpg": (300, 300), "https://abs/huge.jpg": (4000, 4000)})
		primary = EnrichedMeta(source="databazeknih", title="Válka s mloky", authors=["Karel Čapek"], cover_url="https://dk/small.jpg")
		e = Enricher(databazeknih_enabled=True, abs_czech_url="http://p:8000")
		assert e.upgrade_cover(primary, title="Válka s mloky", author="Karel Čapek").cover_url == "https://dk/small.jpg"

	def test_authorless_alternative_needs_near_exact_title(self, monkeypatch):
		# An author-less alternative (~84 title score) is junk risk → no
		# swap; a near-exact one (~93) may lend its cover.
		junk = EnrichedMeta(source="abs_czech", title="Nová válka s mloky", cover_url="https://abs/huge.jpg")
		monkeypatch.setattr(enrichers, "lookup_abs_czech", lambda **kw: junk)
		self._patch_sizes(monkeypatch, {"https://dk/small.jpg": (300, 300), "https://abs/huge.jpg": (4000, 4000)})
		primary = EnrichedMeta(source="databazeknih", title="Válka s mloky", authors=["Karel Čapek"], cover_url="https://dk/small.jpg")
		e = Enricher(databazeknih_enabled=True, abs_czech_url="http://p:8000")
		assert e.upgrade_cover(primary, title="Válka s mloky", author="Karel Čapek").cover_url == "https://dk/small.jpg"
		exact = EnrichedMeta(source="abs_czech", title="Válka s mloky 2", cover_url="https://abs/huge.jpg")
		monkeypatch.setattr(enrichers, "lookup_abs_czech", lambda **kw: exact)
		assert e.upgrade_cover(primary, title="Válka s mloky", author="Karel Čapek").cover_url == "https://abs/huge.jpg"

	def test_disabled_alternative_skips_lookup(self, monkeypatch):
		def _fail(**kw):
			raise AssertionError("databazeknih consulted while disabled")

		monkeypatch.setattr(enrichers, "lookup_databazeknih", _fail)
		primary = EnrichedMeta(source="abs_czech", title="1984", authors=["George Orwell"], cover_url="https://abs/small.jpg")
		e = Enricher(abs_czech_url="http://p:8000")  # databazeknih NOT enabled
		assert e.upgrade_cover(primary, title="1984").cover_url == "https://abs/small.jpg"

	def test_other_sources_are_untouched(self, monkeypatch):
		self._patch_sizes(monkeypatch, {})
		primary = EnrichedMeta(source="openlibrary", title="1984", cover_url="https://ol/x.jpg")
		e = Enricher(databazeknih_enabled=True, abs_czech_url="http://p:8000")
		assert e.upgrade_cover(primary, title="1984").cover_url == "https://ol/x.jpg"

	def test_missing_primary_cover_takes_probeable_alternative(self, monkeypatch):
		dk = EnrichedMeta(source="databazeknih", title="1984", authors=["George Orwell"], cover_url="https://dk/any.jpg")
		monkeypatch.setattr(enrichers, "lookup_databazeknih", lambda **kw: dk)
		self._patch_sizes(monkeypatch, {"https://dk/any.jpg": (400, 400)})
		primary = EnrichedMeta(source="abs_czech", title="1984", authors=["George Orwell"], cover_url=None)
		e = Enricher(databazeknih_enabled=True, abs_czech_url="http://p:8000")
		assert e.upgrade_cover(primary, title="1984").cover_url == "https://dk/any.jpg"

	def test_alternative_lookup_is_cached(self, tmp_path, monkeypatch):
		"""The coveralt: cache must absorb the alternative lookup — a re-run
		never re-scrapes databazeknih for the same book."""
		dk = EnrichedMeta(source="databazeknih", title="1984", authors=["George Orwell"], cover_url="https://dk/big.jpg")
		calls = {"n": 0}

		def _dk(**kw):
			calls["n"] += 1
			return dk

		monkeypatch.setattr(enrichers, "lookup_databazeknih", _dk)
		self._patch_sizes(monkeypatch, {"https://abs/small.jpg": (300, 300), "https://dk/big.jpg": (1200, 1200)})
		cache = tmp_path / "cache.db"
		e1 = Enricher(cache_db=cache, databazeknih_enabled=True, abs_czech_url="http://p:8000")
		out1 = e1.upgrade_cover(EnrichedMeta(source="abs_czech", title="1984", cover_url="https://abs/small.jpg"), title="1984")
		assert out1.cover_url == "https://dk/big.jpg" and calls["n"] == 1
		monkeypatch.setattr(enrichers, "lookup_databazeknih", lambda **kw: (_ for _ in ()).throw(AssertionError("re-scraped")))
		e2 = Enricher(cache_db=cache, databazeknih_enabled=True, abs_czech_url="http://p:8000")
		out2 = e2.upgrade_cover(EnrichedMeta(source="abs_czech", title="1984", cover_url="https://abs/small.jpg"), title="1984")
		assert out2.cover_url == "https://dk/big.jpg"


class TestOnlineFillWantCover:
	"""_online_fill asks for the cover upgrade only when the book carries a
	cover diagnosis (want_cover from _try_deterministic_fix)."""

	@staticmethod
	def _identity():
		from book_meta_fix.verifier import IdentityResult

		return IdentityResult(title="1984", authors=["George Orwell"])

	def test_want_cover_invokes_upgrade(self):
		from book_meta_fix.pipeline import _online_fill

		class _Enricher:
			def lookup(self, **kw):
				return EnrichedMeta(source="abs_czech", title="1984", authors=["George Orwell"], cover_url="https://x/a.jpg")

			def upgrade_cover(self, result, **kw):
				self.upgraded = True
				result.cover_url = "https://x/b.jpg"
				return result

		enr = _Enricher()
		out = _online_fill(self._identity(), enr, skip_enrich=False, want_cover=True)
		assert out.cover_url == "https://x/b.jpg"
		assert enr.upgraded is True

	def test_no_cover_diagnosis_skips_upgrade(self):
		from book_meta_fix.pipeline import _online_fill

		class _Enricher:
			def lookup(self, **kw):
				return EnrichedMeta(source="abs_czech", title="1984", authors=["George Orwell"], cover_url="https://x/a.jpg")

			def upgrade_cover(self, result, **kw):
				raise AssertionError("upgrade_cover called without a cover diagnosis")

		out = _online_fill(self._identity(), _Enricher(), skip_enrich=False, want_cover=False)
		assert out.cover_url == "https://x/a.jpg"


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
