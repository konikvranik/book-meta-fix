"""Online metadata enrichers.

Looks up book metadata from external sources to fill in missing/incorrect
fields. Each enricher returns an EnrichedMeta or None.

Status of sources (verified against this library's CZ/SK content):
- OpenLibrary ISBN:    works for ~10% of CZ books (international reprints)
- OpenLibrary title:   works for famous books in original language
- Google Books ISBN:   rate-limited without API key (shared quota)
- obalkyknih.cz API:   requires library API key (returns empty otherwise)
- obalkyknih.cz HTML:  works but needs robust scraping (TODO)

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
	source: str = ""  # 'openlibrary' | 'google_books' | 'obalkyknih' | 'llm:high'


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
	em.publishers = [p.get("name", "") for p in bk.get("publishers", []) if p.get("name")]
	if em.publishers:
		em.publisher = em.publishers[0]
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
	em.publishers = doc.get("publisher", [])
	if em.publishers:
		em.publisher = em.publishers[0]
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
# Top-level lookup with caching
# ---------------------------------------------------------------------------


class Enricher:
	"""Coordinates lookups across sources with on-disk caching."""

	def __init__(self, cache_db: Path | None = None, rate_sec: float = 1.0) -> None:
		self.rate_sec = rate_sec
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

	def lookup(self, *, isbn: str | None = None, title: str | None = None, author: str | None = None) -> EnrichedMeta | None:
		"""Try sources in order. Returns first hit or None.

		Order: OpenLibrary by ISBN -> Google Books by ISBN -> OpenLibrary by title.
		Obalkyknih is skipped (requires API key) unless explicitly enabled.
		"""
		cache_key = self._cache_key(isbn=isbn, title=title, author=author)
		cached = self._cache_get(cache_key)
		if cached is not None:
			return cached or None  # cached "not found" as empty payload

		result: EnrichedMeta | None = None
		if isbn:
			result = lookup_openlibrary_isbn(isbn)
			if result is None:
				result = lookup_google_books_isbn(isbn)
		if result is None and title:
			result = lookup_openlibrary_title(title, author)

		self._cache_put(cache_key, result)
		return result

	def close(self) -> None:
		if self._cache_conn is not None:
			self._cache_conn.commit()
			self._cache_conn.close()
			self._cache_conn = None

	def _cache_key(self, *, isbn: str | None, title: str | None, author: str | None) -> str:
		isbn_c = canonicalize(isbn) if isbn else None
		return json.dumps({"isbn": isbn_c, "title": (title or "").lower()[:80], "author": (author or "").lower()[:50]}, sort_keys=True)

	def _cache_get(self, key: str) -> EnrichedMeta | None | str:
		"""Returns: EnrichedMeta on hit, None on miss, '__NOT_FOUND__' on cached negative."""
		if self._cache_conn is None:
			return None
		with self._cache_lock:
			row = self._cache_conn.execute("SELECT payload FROM enrich_cache WHERE key = ?", (key,)).fetchone()
		if row is None:
			return None  # miss
		payload = row[0]
		if payload == "__NOT_FOUND__":
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
					"source": result.source,
				}
			)
		with self._cache_lock:
			self._cache_conn.execute(
				"INSERT OR REPLACE INTO enrich_cache(key, payload, cached_at) VALUES (?,?,?)",
				(key, payload, time.time()),
			)
			self._cache_conn.commit()
