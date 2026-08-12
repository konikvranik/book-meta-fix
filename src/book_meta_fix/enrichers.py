"""Online metadata enrichers.

Looks up book metadata from external sources to fill in missing/incorrect
fields. Each enricher returns an EnrichedMeta or None.

Status of sources (verified against this library's CZ/SK content):
- databazeknih.cz:     CZ/SK-focused, no API key, scrapes search + detail.
                       Best source for CZ/SK genres (JSON-LD `genre` + user
                       `Štítky`). Opt-in (scraping, enabled via config flag).
- OpenLibrary ISBN:    works for ~10% of CZ books (international reprints)
- OpenLibrary title:   works for famous books in original language
- Google Books ISBN:   rate-limited without API key (shared quota)
- obalkyknih.cz API:   requires library API key (returns empty otherwise)

The lookup is best-effort: we try each source in order and take the first
hit. Results are cached in a SQLite table to avoid re-querying.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .isbn import canonicalize

log = logging.getLogger(__name__)

USER_AGENT = "book-meta-fix/0.1 (https://github.com/pvranik/book-meta-fix)"


@dataclass
class EnrichedMeta:
	"""Metadata fetched from an online source or LLM."""

	title: str | None = None
	authors: list[str] = field(default_factory=list)
	isbn: str | None = None  # canonicalized
	publisher: str | None = None
	year: int | None = None
	language: str | None = None
	description: str | None = None
	cover_url: str | None = None
	series: str | None = None  # series name (LLM / obalkyknih)
	series_index: str | None = None  # position within series
	genres: list[str] = field(default_factory=list)  # genre tags (databazeknih / LLM)
	source: str = ""  # 'openlibrary' | 'google_books' | 'databazeknih' | 'llm:high'
	# True when the book's identity was confirmed against its own content
	# (ISBN agreement or title+author in the page text), independent of the
	# online match. When set, the proposal is high-confidence and safe to
	# auto-accept even if it changes title/author — we know which book it is.
	identity_confirmed: bool = False


# ---------------------------------------------------------------------------
# Rate limiter (shared across all enrichers in this process)
# ---------------------------------------------------------------------------


class RateLimiter:
	"""Simple per-host rate limiter (min interval between calls)."""

	def __init__(self) -> None:
		self._last: dict[str, float] = {}
		self._lock = threading.Lock()

	def wait(self, host: str, min_interval: float) -> None:
		with self._lock:
			now = time.monotonic()
			last = self._last.get(host, 0.0)
			sleep = max(0.0, last + min_interval - now)
			if sleep > 0:
				time.sleep(sleep)
			self._last[host] = time.monotonic()


_rate_limiter = RateLimiter()


def _http_get(url: str, params: dict | None = None, timeout: float = 15.0, rate: float = 1.0) -> requests.Response | None:
	"""GET with rate limiting, returning None on network failure."""
	from urllib.parse import urlparse

	host = urlparse(url).netloc
	_rate_limiter.wait(host, rate)
	try:
		r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
		return r
	except requests.RequestException as e:
		log.debug("HTTP GET failed for %s: %s", url, e)
		return None


# ---------------------------------------------------------------------------
# OpenLibrary
# ---------------------------------------------------------------------------


def lookup_openlibrary_isbn(isbn: str) -> EnrichedMeta | None:
	"""Lookup by ISBN on OpenLibrary. Returns None if not found."""
	isbn = canonicalize(isbn) or isbn
	r = _http_get(
		"https://openlibrary.org/api/books",
		params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"},
	)
	if r is None or r.status_code != 200:
		return None
	data = r.json()
	key = f"ISBN:{isbn}"
	if key not in data:
		return None
	bk = data[key]
	em = EnrichedMeta(source="openlibrary", isbn=isbn)
	em.title = bk.get("title")
	em.authors = [a.get("name", "") for a in bk.get("authors", []) if a.get("name")]
	publishers = [p.get("name", "") for p in bk.get("publishers", []) if p.get("name")]
	if publishers:
		em.publisher = publishers[0]
	if bk.get("publish_date"):
		# Often "2005" or "October 2005"
		import re

		m = re.search(r"\d{4}", bk["publish_date"])
		if m:
			em.year = int(m.group(0))
	cover = bk.get("cover", {})
	em.cover_url = cover.get("medium") or cover.get("large") or cover.get("small")
	return em


def lookup_openlibrary_title(title: str, author: str | None = None) -> EnrichedMeta | None:
	"""Lookup by title (+ optional author) on OpenLibrary search API."""
	# Build query. Use the last author token (surname) for better matches.
	q = f"title:{title}"
	if author:
		# Take the last whitespace-separated token as surname
		surname = author.split()[-1] if author.split() else author
		q += f" author:{surname}"
	r = _http_get(
		"https://openlibrary.org/search.json",
		params={"q": q, "limit": 1},
	)
	if r is None or r.status_code != 200:
		return None
	data = r.json()
	if data.get("numFound", 0) == 0 or not data.get("docs"):
		return None
	doc = data["docs"][0]
	em = EnrichedMeta(source="openlibrary")
	em.title = doc.get("title")
	em.authors = doc.get("author_name", [])
	publishers = doc.get("publisher", [])
	if publishers:
		em.publisher = publishers[0]
	years = doc.get("publish_year", [])
	if years:
		em.year = years[0]
	isbns = doc.get("isbn", [])
	if isbns:
		em.isbn = canonicalize(isbns[0])
	cover_i = doc.get("cover_i")
	if cover_i:
		em.cover_url = f"https://covers.openlibrary.org/b/id/{cover_i}-M.jpg"
	return em


# ---------------------------------------------------------------------------
# Google Books
# ---------------------------------------------------------------------------


def lookup_google_books_isbn(isbn: str) -> EnrichedMeta | None:
	"""Lookup by ISBN on Google Books. Often rate-limited without API key."""
	isbn = canonicalize(isbn) or isbn
	r = _http_get(
		"https://www.googleapis.com/books/v1/volumes",
		params={"q": f"isbn:{isbn}"},
	)
	if r is None or r.status_code != 200:
		return None
	data = r.json()
	if data.get("totalItems", 0) == 0 or not data.get("items"):
		return None
	info = data["items"][0].get("volumeInfo", {})
	em = EnrichedMeta(source="google_books", isbn=isbn)
	em.title = info.get("title")
	em.authors = info.get("authors", [])
	em.publisher = info.get("publisher")
	date = info.get("publishedDate", "")
	if date:
		import re

		m = re.search(r"\d{4}", date)
		if m:
			em.year = int(m.group(0))
	em.language = info.get("language")
	em.description = info.get("description")
	img = info.get("imageLinks", {})
	em.cover_url = img.get("extraLarge") or img.get("large") or img.get("thumbnail") or img.get("smallThumbnail")
	return em


# ---------------------------------------------------------------------------
# databazeknih.cz (scraping — best source for CZ/SK genres)
# ---------------------------------------------------------------------------

# Match the book detail URL from search results: /prehled-knihy/<slug>-<id>
import re as _re

# A search result row looks like:
#   <a class='new' type='book' href='/prehled-knihy/<slug>-<id>'>Visible Title</a>
# databazeknih.cz uses single-quoted attributes; the fixture uses double quotes,
# so accept either quote style on every quoted attribute.
_RESULT_RE = _re.compile(
	r'''<a\s+class=["']new["'][^>]*?type=["']book["'][^>]*?href=["'](/prehled-knihy/[^"']+)["'][^>]*>([^<]+)</a>''',
	_re.IGNORECASE,
)
# A whole search-result row: <p class='new'> ... anchor + pozn note ... </p>.
# Used to associate each result's title with its trailing year/author note.
_RESULT_BLOCK_RE = _re.compile(
	r"<p[^>]*class=['\"]new['\"][^>]*>(.*?)</p>",
	_re.DOTALL | _re.IGNORECASE,
)
# Leading 4-digit year inside <span class='pozn'>YEAR, Author</span>.
_POZN_YEAR_RE = _re.compile(r"<span[^>]*class=['\"]pozn['\"][^>]*>\s*(\d{4})")


def _parse_search_results(html: str) -> list[tuple[str, str, int | None]]:
	"""Parse search results into (path, title, year) tuples.

	The year comes from the trailing <span class='pozn'>YEAR, Author</span> note
	in each result's <p> block; None when the note lacks a leading year.
	"""
	results: list[tuple[str, str, int | None]] = []
	seen: set[str] = set()
	for block_m in _RESULT_BLOCK_RE.finditer(html):
		block = block_m.group(1)
		am = _RESULT_RE.search(block)
		if am is None:
			continue
		path = am.group(1)
		rtitle = am.group(2).strip()
		if path in seen or not rtitle:
			continue
		seen.add(path)
		year: int | None = None
		pm = _POZN_YEAR_RE.search(block)
		if pm:
			try:
				year = int(pm.group(1))
			except ValueError:  # noqa: BLE001
				year = None
		results.append((path, rtitle, year))
		if len(results) >= 10:
			break
	return results
# JSON-LD <script type="application/ld+json"> { ... } </script>
_JSONLD_RE = _re.compile(
	r'<script type="application/ld\+json">\s*(\{.*?\})\s*</script>',
	_re.DOTALL,
)
# User tags (Štítky knihy): <a class="tag" href='/stitky/...' title='...'>...</a>
# databazeknih.cz uses single-quoted attributes; the fixture uses double quotes,
# so accept either quote style.
_TAG_RE = _re.compile(
	r'''<a\s+class=["']tag["'][^>]*?title=["']([^"']+)["']''',
	_re.IGNORECASE,
)


def _http_get_html(url: str, *, timeout: float = 15.0, rate: float = 1.0) -> str | None:
	"""GET returning response text, None on failure. Uses browser-like UA
	(databazeknih.cz 403s the default python-requests UA)."""
	from urllib.parse import urlparse

	host = urlparse(url).netloc
	_rate_limiter.wait(host, rate)
	try:
		r = requests.get(
			url,
			timeout=timeout,
			headers={
				"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
				"Accept": "text/html,application/xhtml+xml",
				"Accept-Language": "cs,en;q=0.5",
			},
		)
		if r.status_code != 200:
			log.debug("databazeknih GET %s -> %s", url, r.status_code)
			return None
		# databazeknih.cz serves cp1250 for legacy paths but UTF-8 for /prehled-knihy.
		# requests guesses ISO-8859-1 when charset is missing; force UTF-8.
		if not r.encoding or r.encoding.lower() in ("iso-8859-1",):
			r.encoding = "utf-8"
		return r.text
	except requests.RequestException as e:
		log.debug("databazeknih GET failed for %s: %s", url, e)
		return None


def _search_databazeknih(title: str, author: str | None, year: int | None = None) -> str | None:
	"""Search databazeknih.cz, return the best-matching detail path
	('/prehled-knihy/<slug>-<id>') or None.

	Strategy: query with title (+ author surname). Among results that fuzzy-
	match the title (>= 70), prefer one whose publication year matches *year*
	(±1) so the right edition is chosen among same-titled results. Falls back
	to the best fuzzy match when no year is known or none matches (same work,
	different edition) — this maximises autodetection without rejecting same-
	work editions.
	"""
	from urllib.parse import quote_plus

	from rapidfuzz import fuzz

	q = quote_plus(title)
	if author:
		# Surname only — full names add noise and the DB indexes by title.
		surname = author.split()[-1] if author.split() else author
		q = quote_plus(f"{title} {surname}")
	url = f"https://www.databazeknih.cz/search?q={q}&in=books"
	html = _http_get_html(url)
	if html is None:
		return None

	results = _parse_search_results(html)
	if not results:
		return None

	# Keep candidates with a reasonable fuzzy title match (>= 70) so we don't
	# attach genres from a wrong book (search is keyword-based, many near-misses).
	candidates = [
		(fuzz.token_sort_ratio(title.lower(), rtitle.lower()), path, ryear)
		for path, rtitle, ryear in results
	]
	candidates = [c for c in candidates if c[0] >= 70]
	if not candidates:
		log.debug("databazeknih search '%s' best match score < 70, skipping", title)
		return None

	# Prefer an edition whose year matches the target (±1) among the candidates;
	# this disambiguates editions of the same work. If none matches, keep all
	# (same work, other edition) rather than rejecting.
	if year is not None:
		year_matches = [c for c in candidates if c[2] is not None and abs(c[2] - year) <= 1]
		if year_matches:
			candidates = year_matches

	# Best fuzzy title among the (optionally year-filtered) candidates.
	candidates.sort(key=lambda c: c[0], reverse=True)
	return candidates[0][1]


def _parse_jsonld(html: str) -> dict | None:
	"""Extract and parse the schema.org Book JSON-LD block, or None."""
	m = _JSONLD_RE.search(html)
	if m is None:
		return None
	raw = m.group(1)
	try:
		return json.loads(raw)
	except json.JSONDecodeError as e:
		log.debug("databazeknih JSON-LD parse error: %s", e)
		return None


def _parse_databazeknih_detail(html: str) -> EnrichedMeta | None:
	"""Parse a databazeknih book-detail page (JSON-LD + Štítky) into EnrichedMeta.

	Returns None if the page carries no usable title/isbn (e.g. a 'no results'
	search page). Shared by the title-search and ISBN-search lookups.
	"""
	ld = _parse_jsonld(html) or {}
	em = EnrichedMeta(source="databazeknih")

	# --- JSON-LD metadata (authoritative-ish) ---
	em.title = ld.get("name")
	authors = ld.get("author")
	if isinstance(authors, list):
		em.authors = [a.get("name", "") for a in authors if isinstance(a, dict) and a.get("name")]
	elif isinstance(authors, dict):
		em.authors = [authors.get("name", "")] if authors.get("name") else []
	# ISBN: keep dashes (databazeknih uses them); canonicalize for our store.
	raw_isbn = ld.get("isbn")
	if raw_isbn:
		em.isbn = canonicalize(raw_isbn) or raw_isbn
	publishers = ld.get("publisher")
	if isinstance(publishers, list) and publishers:
		em.publisher = publishers[0].get("name") if isinstance(publishers[0], dict) else None
	elif isinstance(publishers, dict):
		em.publisher = publishers.get("name")
	lang = ld.get("inLanguage")
	if lang:
		em.language = lang
	desc = ld.get("description")
	if desc:
		em.description = desc
	img = ld.get("image")
	if img:
		em.cover_url = img

	# --- Genres: JSON-LD genre (broad) + user tags (rich) ---
	genres: list[str] = []
	raw_genre = ld.get("genre")
	if isinstance(raw_genre, str):
		genres.append(raw_genre)
	elif isinstance(raw_genre, list):
		genres.extend(g for g in raw_genre if isinstance(g, str) and g)
	# User tags (Štítky) — richer and more specific (e.g. 'antiutopie').
	tags = _TAG_RE.findall(html)
	for t in tags:
		if t not in genres:
			genres.append(t)
	em.genres = genres

	if not (em.title or em.isbn):
		return None  # nothing usable
	return em


def lookup_databazeknih(*, title: str, author: str | None = None, year: int | None = None) -> EnrichedMeta | None:
	"""Scrape databazeknih.cz for a book's metadata + genres (by title search).

	Two HTTP calls: (1) search by title to find the detail page, (2) fetch the
	detail page and parse its JSON-LD (schema.org Book) plus the user 'Štítky'.
	When *year* is given, the search prefers an edition published in that year
	(±1) among the title-matching candidates.

	Returns None if the book can't be confidently matched (fuzzy title < 70).
	"""
	from urllib.parse import urljoin

	path = _search_databazeknih(title, author, year=year)
	if path is None:
		return None

	detail_url = urljoin("https://www.databazeknih.cz/", path)
	html = _http_get_html(detail_url)
	if html is None:
		return None
	return _parse_databazeknih_detail(html)


def lookup_databazeknih_isbn(isbn: str) -> EnrichedMeta | None:
	"""Look up a book on databazeknih.cz by ISBN (exact match, no fuzzy score).

	ISBN search returns the book's detail page directly for a hit (one HTTP
	call), or a 'no results' page for a miss. For CZ/SK books this is the most
	reliable entry point — it resolves the correct record even when the library
	metadata title is corrupt (filename-as-title). If the search unexpectedly
	returns a list, the first result is fetched as a fallback.
	"""
	from urllib.parse import quote_plus, urljoin

	url = f"https://www.databazeknih.cz/search?q={quote_plus(isbn)}&in=books"
	html = _http_get_html(url)
	if html is None:
		return None
	# Direct profile hit (the usual case): the page is the book detail.
	em = _parse_databazeknih_detail(html)
	if em is not None:
		return em
	# Fallback: a search-results list — take the first result's detail page.
	for m in _RESULT_RE.finditer(html):
		path = m.group(1)
		detail_html = _http_get_html(urljoin("https://www.databazeknih.cz/", path))
		if detail_html is not None:
			return _parse_databazeknih_detail(detail_html)
		break
	return None


# ---------------------------------------------------------------------------
# Top-level lookup with caching
# ---------------------------------------------------------------------------


class Enricher:
	"""Coordinates lookups across sources with on-disk caching."""

	def __init__(
		self,
		cache_db: Path | None = None,
		rate_sec: float = 1.0,
		*,
		databazeknih_enabled: bool = False,
		openlibrary_enabled: bool = True,
		google_books_enabled: bool = True,
		negative_ttl_sec: float = 7 * 24 * 3600,
	) -> None:
		self.rate_sec = rate_sec
		self.databazeknih_enabled = databazeknih_enabled
		self.openlibrary_enabled = openlibrary_enabled
		self.google_books_enabled = google_books_enabled
		# A cached negative ("__NOT_FOUND__") older than this is treated as a
		# miss and re-queried. <= 0 keeps negatives forever (old behaviour).
		self._negative_ttl = negative_ttl_sec
		self._cache_conn: sqlite3.Connection | None = None
		self._cache_lock = __import__("threading").Lock()
		if cache_db is not None:
			# check_same_thread=False: the Enricher is shared across worker
			# threads. All access goes through self._cache_lock.
			self._cache_conn = sqlite3.connect(str(cache_db), check_same_thread=False)
			self._cache_conn.execute(
				"""
				CREATE TABLE IF NOT EXISTS enrich_cache (
					key TEXT PRIMARY KEY,
					payload TEXT NOT NULL,
					cached_at REAL NOT NULL
				)
				"""
			)
			self._cache_conn.commit()

	def lookup(self, *, isbn: str | None = None, title: str | None = None, author: str | None = None, year: int | None = None) -> EnrichedMeta | None:
		"""Try sources in order. Returns first hit or None.

		Order (gated by *_enabled flags):
		  1. databazeknih.cz by ISBN (exact; best for CZ/SK + genres)
		  2. databazeknih.cz by title (fuzzy >= 70; prefers year-matching edition)
		  3. OpenLibrary by ISBN
		  4. Google Books by ISBN
		  5. OpenLibrary by title

		*year* is used only by the databazeknih title search to disambiguate
		editions; ISBN lookups are exact. databazeknih by ISBN goes first: it's
		an exact match and the only way to reach the strongest CZ/SK source when
		the library title is corrupt.
		"""
		cache_key = self._cache_key(isbn=isbn, title=title, author=author, year=year)
		cached = self._cache_get(cache_key)
		if cached is not None:
			# _cache_get returns one of:
			#   - an EnrichedMeta  -> return it
			#   - "__NOT_FOUND__"   -> a cached negative: return None (we already
			#                          looked once and found nothing)
			#   - None              -> cache miss: fall through and do the lookup
			if cached == "__NOT_FOUND__":
				return None
			return cached

		result: EnrichedMeta | None = None
		# databazeknih by ISBN (exact match) — the strongest CZ/SK source, and
		# the only way to reach it for a book whose title is corrupt but that
		# has an ISBN. Goes first when an ISBN is available.
		if result is None and self.databazeknih_enabled and isbn:
			result = lookup_databazeknih_isbn(isbn)
		# databazeknih by title (search). Best CZ/SK source + genres.
		if result is None and self.databazeknih_enabled and title:
			result = lookup_databazeknih(title=title, author=author, year=year)
		# ISBN-based lookups (authoritative when available).
		if result is None and isbn:
			if self.openlibrary_enabled:
				result = lookup_openlibrary_isbn(isbn)
			if result is None and self.google_books_enabled:
				result = lookup_google_books_isbn(isbn)
		# Fallback: OpenLibrary title search.
		if result is None and title and self.openlibrary_enabled:
			result = lookup_openlibrary_title(title, author)

		self._cache_put(cache_key, result)
		return result

	def close(self) -> None:
		if self._cache_conn is not None:
			self._cache_conn.commit()
			self._cache_conn.close()
			self._cache_conn = None

	def _cache_key(self, *, isbn: str | None, title: str | None, author: str | None, year: int | None = None) -> str:
		isbn_c = canonicalize(isbn) if isbn else None
		return json.dumps({"isbn": isbn_c, "title": (title or "").lower()[:80], "author": (author or "").lower()[:50], "year": year}, sort_keys=True)

	def _cache_get(self, key: str) -> EnrichedMeta | None | str:
		"""Returns: EnrichedMeta on hit, None on miss, '__NOT_FOUND__' on cached negative.

		A cached negative older than the configured TTL is treated as a miss so
		transient online failures (and pre-fix identities) get retried instead
		of being pinned forever.
		"""
		if self._cache_conn is None:
			return None
		with self._cache_lock:
			row = self._cache_conn.execute("SELECT payload, cached_at FROM enrich_cache WHERE key = ?", (key,)).fetchone()
		if row is None:
			return None  # miss
		payload, cached_at = row
		if payload == "__NOT_FOUND__":
			if self._negative_ttl > 0 and (time.time() - cached_at) > self._negative_ttl:
				return None  # expired negative -> re-query and re-cache
			return "__NOT_FOUND__"
		try:
			d = json.loads(payload)
			return EnrichedMeta(**d)
		except Exception:  # noqa: BLE001
			return None

	def _cache_put(self, key: str, result: EnrichedMeta | None) -> None:
		if self._cache_conn is None:
			return
		if result is None:
			payload = "__NOT_FOUND__"
		else:
			payload = json.dumps(
				{
					"title": result.title,
					"authors": result.authors,
					"isbn": result.isbn,
					"publisher": result.publisher,
					"year": result.year,
					"language": result.language,
					"description": result.description,
					"cover_url": result.cover_url,
					"series": result.series,
					"series_index": result.series_index,
					"genres": result.genres,
					"source": result.source,
				}
			)
		with self._cache_lock:
			self._cache_conn.execute(
				"INSERT OR REPLACE INTO enrich_cache(key, payload, cached_at) VALUES (?,?,?)",
				(key, payload, time.time()),
			)
			self._cache_conn.commit()
