"""Audiobookshelf server client + the engine of `bmf abs-rescan`.

bmf writes metadata.json/.opf directly on disk, but Audiobookshelf keeps
its own database and a plain "Scan library" skips every folder it
considers unchanged (mtime gate — and when ABS mounts the library over
NFS, the server's attribute cache can mask even a genuinely new mtime).
The reliable way to push our writes into ABS is a per-item re-scan via
the API, which bypasses the mtime gate entirely:

	GET  /api/libraries                     -> id/name/mediaType/folders
	GET  /api/libraries/{id}/items?limit=0  -> items incl. path + relPath
	POST /api/items/batch/scan              -> {"libraryItemIds": [...]}
	                                           (200 immediately; the server
	                                           scans the items in background)
	POST /api/items/{id}/scan               -> per-item fallback for older
	                                           ABS builds without the batch
	                                           endpoint (404 -> fall back)
	POST /api/libraries/{id}/scan?force=1   -> the --force-all escape hatch
	PATCH /api/items/{id}/media             -> series deletions: the scanner
	                                           never REMOVES a series, only
	                                           the metadata update path does
	                                           (see clear_item_series)
	GET  /api/libraries/{id}/series         -> the ABS-side series rows with
	                                           their books embedded (paged;
	                                           limit=0 means ZERO here)

	Auth is `Authorization: Bearer <token>` and the scan endpoints require an
	ADMIN token (a regular user's token gets 403). Everything here is
	best-effort: network/HTTP failures return None/False and log at debug —
	a rescan we cannot deliver is a hint in the command output, not a crash.

	GET  /api/libraries/{id}/items  also yields each item's media.coverPath —
	the stored cover row abs-rescan --fix-covers audits. A coverPath written
	by an older ABS build can point at a non-image file (metadata.json,
	cover.html) and while that file exists the scanner never re-picks, so
	every cover-cache refresh feeds it to ffmpeg ("Invalid data found").
	DELETE /api/items/{id}/cover nulls the row + purges the cache; the
	follow-up rescan lets ABS choose a real cover again.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from .covers import ABS_IMAGE_EXTS
from .library import _stat_folder, iter_book_folders
from .models import series_entry_pair

log = logging.getLogger(__name__)

USER_AGENT = "book-meta-fix/0.1 (https://github.com/konikvranik/book-meta-fix)"

# Pause between two consecutive scan POSTs of the SAME worker thread: keeps
# the aggregate burst off a tiny LAN server (with N workers the aggregate
# rate is N/(scan_time + this), still gentle per connection).
SCAN_CALL_PAUSE = 0.15


def _http_get_json(
	url: str, *, params: dict | None = None, timeout: float = 15.0, headers: dict[str, str] | None = None,
	session: requests.Session | None = None,
) -> dict | None:
	"""GET returning parsed JSON, or None on any failure (network/HTTP/JSON)."""
	try:
		r = (session or requests).get(url, params=params, timeout=timeout, headers=headers)
	except requests.RequestException as e:
		log.debug("HTTP GET failed for %s: %s", url, e)
		return None
	if r.status_code != 200:
		log.debug("HTTP GET %s -> %s", url, r.status_code)
		return None
	try:
		return r.json()
	except ValueError:
		return None


def _http_get_bytes(
	url: str, *, timeout: float = 15.0, headers: dict[str, str] | None = None,
	session: requests.Session | None = None,
) -> bytes | None:
	"""GET returning the raw response body, or None on any failure.

	Same monkeypatch seam shape as _http_get_json/_http_delete (the
	no-network tests stub this too): used to fetch an item's stored cover
	image bytes for the generated-marker check of the --fix-covers audit —
	coverPath targets outside the library folders (ABS's own /metadata
	cache) are not on our mount, so the only way to see their bytes is the
	server's own /api/items/{id}/cover endpoint.
	"""
	try:
		r = (session or requests).get(url, timeout=timeout, headers=headers)
	except requests.RequestException as e:
		log.debug("HTTP GET failed for %s: %s", url, e)
		return None
	if r.status_code != 200:
		log.debug("HTTP GET %s -> %s", url, r.status_code)
		return None
	return r.content


def _http_post(
	url: str,
	*,
	params: dict | None = None,
	json_body: dict | None = None,
	timeout: float = 15.0,
	headers: dict[str, str] | None = None,
	session: requests.Session | None = None,
) -> requests.Response | None:
	"""POST returning the whole response, or None on network failure.

	Unlike _http_get_json the response object is returned whole: the scan
	endpoints answer with a status we must distinguish (200 delivered /
	403 non-admin token / 404 endpoint missing) and a tiny or empty body.
	"""
	try:
		return (session or requests).post(url, params=params, json=json_body, timeout=timeout, headers=headers)
	except requests.RequestException as e:
		log.debug("HTTP POST failed for %s: %s", url, e)
		return None


def _http_post_file(
	url: str,
	*,
	files: dict,
	timeout: float = 15.0,
	headers: dict[str, str] | None = None,
	session: requests.Session | None = None,
) -> requests.Response | None:
	"""Multipart file-upload POST (requests' ``files=`` shape) — the monkeypatch
	seam twin of _http_post, used solely by the cover-cache purge dance in
	AudiobookshelfClient.clear_item_cover."""
	try:
		return (session or requests).post(url, files=files, timeout=timeout, headers=headers)
	except requests.RequestException as e:
		log.debug("HTTP POST (file) failed for %s: %s", url, e)
		return None


def _http_delete(
	url: str,
	*,
	timeout: float = 15.0,
	headers: dict[str, str] | None = None,
	session: requests.Session | None = None,
) -> requests.Response | None:
	"""DELETE returning the whole response, or None on network failure.

	Same whole-response shape as _http_post: the cover endpoint answers with
	a bare status we must distinguish (200 cleared / 403 non-admin token).
	"""
	try:
		return (session or requests).delete(url, timeout=timeout, headers=headers)
	except requests.RequestException as e:
		log.debug("HTTP DELETE failed for %s: %s", url, e)
		return None


def _http_patch(
	url: str,
	*,
	json_body: dict | None = None,
	timeout: float = 15.0,
	headers: dict[str, str] | None = None,
	session: requests.Session | None = None,
) -> requests.Response | None:
	"""PATCH returning the whole response, or None on network failure.

	Same whole-response shape as _http_post/_http_delete and the same
	monkeypatch seam for the no-network tests: the media-update endpoint
	answers with a bare status we must distinguish (200 applied / 403
	non-admin token) and a body we do not need.
	"""
	try:
		return (session or requests).patch(url, json=json_body, timeout=timeout, headers=headers)
	except requests.RequestException as e:
		log.debug("HTTP PATCH failed for %s: %s", url, e)
		return None


@dataclass(frozen=True)
class AbsItem:
	"""An Audiobookshelf library item — the fields a rescan needs."""

	id: str
	path: str = ""  # absolute path as the ABS server sees it (its own mount)
	rel_path: str = ""  # relative to the ABS library folder
	title: str = ""
	cover_path: str = ""  # media.coverPath — the stored DB row --fix-covers audits


class AudiobookshelfClient:
	"""Minimal Audiobookshelf client: list libraries/items, trigger rescans."""

	def __init__(self, base_url: str, token: str) -> None:
		self.base_url = base_url.rstrip("/")
		self._headers = {
			"User-Agent": USER_AGENT,
			"Accept": "application/json",
			"Authorization": f"Bearer {token}",
		}
		# One shared session for every call of this client: module-level
		# requests.get/post/delete would open (and TLS-handshake) a NEW
		# connection per call — measured as a visible chunk of the per-item
		# rescan time over ~3000 calls. urllib3's pool is thread-safe and the
		# API is stateless (Bearer auth, no cookies), so worker threads share
		# it; the oversized pool keeps parallel workers from discarding
		# keep-alive connections.
		self._session = requests.Session()
		adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32)
		self._session.mount("https://", adapter)
		self._session.mount("http://", adapter)

	def libraries(self) -> list[dict] | None:
		"""All libraries on the server (id, name, mediaType, folders)."""
		data = _http_get_json(f"{self.base_url}/api/libraries", headers=self._headers, session=self._session)
		if not isinstance(data, dict):
			return None
		libs = data.get("libraries")
		return libs if isinstance(libs, list) else None

	def get_item(self, item_id: str) -> dict | None:
		"""One library item as raw JSON (GET /api/items/{id}).

		Feeds clear_item_cover's purge dance: after the eviction stub upload,
		the item's fresh coverPath tells where the stub FILE landed (libraries
		that store covers with items) so it can be removed again.
		"""
		data = _http_get_json(
			f"{self.base_url}/api/items/{item_id}",
			headers=self._headers, session=self._session,
		)
		return data if isinstance(data, dict) else None

	def items(self, library_id: str) -> list[AbsItem] | None:
		"""All items of a library (limit=0 = server-side no-limit)."""
		data = _http_get_json(
			f"{self.base_url}/api/libraries/{library_id}/items",
			params={"limit": 0},
			headers=self._headers,
			session=self._session,
		)
		if not isinstance(data, dict):
			return None
		results = data.get("results")
		if not isinstance(results, list):
			return None
		items: list[AbsItem] = []
		for row in results:
			if not isinstance(row, dict) or not row.get("id"):
				continue
			media = row.get("media")
			title = ""
			cover = ""
			if isinstance(media, dict):
				if isinstance(media.get("metadata"), dict):
					title = str(media["metadata"].get("title") or "")
				cover = str(media.get("coverPath") or "")
			items.append(
				AbsItem(
					id=str(row["id"]),
					path=str(row.get("path") or ""),
					rel_path=str(row.get("relPath") or ""),
					title=title,
					cover_path=cover,
				)
			)
		return items

	def scan_items(self, item_ids: list[str], progress_callback: Any = None, workers: int = 1) -> int:
		"""Ask ABS to re-scan the given items; returns the FAILURE count (0 = all delivered).

		Deliberately PER-ITEM, not POST /api/items/batch/scan: the batch
		endpoint answers 200 immediately and is supposed to scan in the
		background, but on a real server it was measured accepting ~1100 ids
		and then processing NONE of them (no visible job, no item changed)
		while the per-item endpoint synchronously re-scans and returns the
		result.

		Each POST blocks until ABS has re-read that one item, but the ITEMS
		are independent — *workers* > 1 runs the per-item scans on a thread
		pool (ABS is a Node server and serves concurrent per-item scans
		fine; it parallelises its own library scans the same way). The
		default 1 keeps the historical serial behaviour for direct callers;
		the CLI passes the BMF_ABS_WORKERS/--abs-workers knob. The per-thread
		SCAN_CALL_PAUSE keeps each connection's burst gentle, so N workers
		raise the aggregate rate roughly N-fold without a thundering herd.

		A few thousand items run for many minutes even in parallel —
		*progress_callback* (called as ``callback(done, total)`` after every
		item, same contract as mover/crosscheck) lets the CLI drive a
		progress bar with an ETA so the run does not look hung. It is invoked
		under the counter lock, so implementations see monotonically
		increasing *done* even from worker threads.
		"""
		total = len(item_ids)
		failed = 0
		done = 0
		lock = threading.Lock()
		paused_once = threading.local()

		def _scan_one(item_id: str) -> None:
			nonlocal done, failed
			# Sleep BETWEEN two calls of the same worker thread (the serial
			# behaviour); the first call of each thread goes out immediately.
			if getattr(paused_once, "v", False):
				time.sleep(SCAN_CALL_PAUSE)
			paused_once.v = True
			r = _http_post(f"{self.base_url}/api/items/{item_id}/scan", headers=self._headers, session=self._session)
			with lock:
				if r is None or r.status_code != 200:
					log.debug("item scan failed for %s (HTTP %s)", item_id, None if r is None else r.status_code)
					failed += 1
				done += 1
				if progress_callback is not None:
					progress_callback(done, total)

		w = max(1, int(workers))
		if w == 1 or total <= 1:
			for item_id in item_ids:
				_scan_one(item_id)
		else:
			with ThreadPoolExecutor(max_workers=w) as pool:
				list(pool.map(_scan_one, item_ids))
		return failed

	def scan_library(self, library_id: str, *, force: bool = False) -> bool:
		"""Trigger a library scan (force = re-scan every item)."""
		r = _http_post(
			f"{self.base_url}/api/libraries/{library_id}/scan",
			params={"force": 1} if force else None,
			headers=self._headers,
			session=self._session,
		)
		return r is not None and r.status_code == 200

	# A 2x3 near-white JPEG — the eviction stub for the cover-cache purge
	# dance below. Deliberately NOT generated on the fly: fixed bytes make the
	# dance testable and the stub recognisable in logs.
	_PURGE_STUB_JPEG = (
		b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00"
		b"\r\t\n\x0b\n\x08\r\x0b\n\x0b\x0e\x0e\r\x0f\x13 \x15\x13\x12\x12\x13'\x1c\x1e\x17 .)"
		b"10.)-,3:J>36F7,-@WAFLNRSR2>ZaZP`JQRO\xff\xdb\x00C\x01\x0e\x0e\x0e\x13\x11\x13&\x15"
		b"\x15&O5-5OOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOO\xff\xc0\x00\x11\x08\x00"
		b"\x03\x00\x02\x03\x01\"\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x1f\x00\x00\x01\x05"
		b"\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07"
		b"\x08\t\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00"
		b"\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07\"q\x142\x81\x91\xa1\x08#B"
		b"\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZ"
		b"cdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99"
		b"\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3"
		b"\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5"
		b"\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xc4\x00\x1f\x01\x00"
		b"\x03\x01\x01\x01\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05"
		b"\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x11\x00\x02\x01\x02\x04\x04\x03\x04\x07\x05\x04"
		b"\x04\x00\x01\x02w\x00\x01\x02\x03\x11\x04\x05!1\x06\x12AQ\x07aq\x13\"2\x81\x08\x14B"
		b"\x91\xa1\xb1\xc1\t#3R\xf0\x15br\xd1\n\x16$4\xe1%\xf1\x17\x18\x19\x1a&'()*56789:CDEF"
		b"GHIJSTUVWXYZcdefghijstuvwxyz\x82\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96"
		b"\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9"
		b"\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe2\xe3"
		b"\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x0c\x03"
		b"\x01\x00\x02\x11\x03\x11\x00?\x00\xf4Z(\xa2\x80?\xff\xd9"
	)

	def clear_item_cover(
		self, item_id: str, *,
		library_root: Path | None = None, abs_folders: list[str] | None = None,
	) -> bool:
		"""Null an item's stored cover row (DELETE /api/items/{id}/cover) and
		evict the server-side cover CACHE when it survives the delete.

		The ABS-database counterpart of the strip-covers file cleanup: a stale
		``media.coverPath`` (ffmpeg fed metadata.json as the cover) never
		self-heals, because the scanner only re-picks a cover when the row is
		NULL or its file vanished — and a metadata.json neither vanishes nor
		counts as an image file. The DELETE nulls the row; the caller follows
		up with a rescan so ABS picks a real cover from the folder again.

		Measured 2026-09-17 (ABS 2.36.0): the row is only HALF the story. The
		cover cache under ABS's /metadata keeps serving bytes after the row is
		null — through a per-item rescan and through any number of further
		DELETEs (a delete on an already-null row is a no-op that never reaches
		the cache-purge branch). A book whose every disk cover source was
		cleaned up kept showing its old generated cover forever. The only way
		out is the upload+delete dance: uploading any cover sets coverPath
		again, and the FOLLOWING delete then runs the "has cover" path that
		purges row AND cache. The upload drops a stub file into the book
		folder on libraries that store covers with items — when
		*library_root*/*abs_folders* are given, the stub is mapped and
		unlinked after the dance so the next scan cannot re-pick it. Returns
		False on network/HTTP failure.
		"""
		r = _http_delete(f"{self.base_url}/api/items/{item_id}/cover", headers=self._headers, session=self._session)
		if r is None or r.status_code != 200:
			return False
		# Still serving bytes → the cache survived; dance.
		if _http_get_bytes(
			f"{self.base_url}/api/items/{item_id}/cover",
			headers=self._headers, session=self._session,
		) is None:
			return True
		up = _http_post_file(
			f"{self.base_url}/api/items/{item_id}/cover",
			files={"cover": ("bmf-purge.jpg", self._PURGE_STUB_JPEG, "image/jpeg")},
			headers=self._headers, session=self._session,
		)
		if up is None or up.status_code not in (200, 201):
			log.warning("cover-cache purge: upload failed for item %s", item_id)
			return False
		# Where did the upload land? On store-cover-with-item libraries it is a
		# stub file inside the book folder — remember it so it can be removed.
		stub_local: Path | None = None
		if library_root is not None and abs_folders:
			item = self.get_item(item_id)
			cover = _norm_posix(str((item or {}).get("media", {}).get("coverPath") or ""))
			if cover:
				rel = ""
				for folder in abs_folders:
					f = _norm_posix(str(folder))
					if f and cover.startswith(f + "/"):
						rel = cover[len(f) + 1:]
						break
				if rel:
					stub_local = library_root / rel
		r2 = _http_delete(f"{self.base_url}/api/items/{item_id}/cover", headers=self._headers, session=self._session)
		if r2 is None or r2.status_code != 200:
			log.warning("cover-cache purge: second delete failed for item %s", item_id)
			return False
		if stub_local is not None:
			try:
				stub_local.unlink(missing_ok=True)
			except OSError as exc:
				log.warning("cover-cache purge: stub removal failed for %s: %s", stub_local, exc)
		return True

	def get_item_cover(self, item_id: str) -> bytes | None:
		"""Fetch the item's currently stored cover image bytes (GET cover).

		The read-side companion of clear_item_cover, feeding the
		--fix-covers audit for coverPath targets OUTSIDE the library folders
		(ABS's own /metadata cache): those files are not on our mount, so
		their content — the only witness of a calibre-generated placeholder
		hiding in a cached row — is only reachable through the API. Returns
		None on any failure (the audit then leaves the row alone).
		"""
		return _http_get_bytes(
			f"{self.base_url}/api/items/{item_id}/cover",
			headers=self._headers, session=self._session,
		)

	def item_series_map(self, library_id: str) -> dict[str, list[str]] | None:
		"""item id -> ABS series names, from the paged series listing.

		The listing embeds each series' books, so a few GETs yield the whole
		item->series mapping of the library — far cheaper than one item GET
		per book, and the only cheap source of ABS-side series (the items
		listing minifies metadata and carries no series). Deliberately NOT
		limit=0: unlike /items, this endpoint reads limit=0 as "zero per
		page" (measured on 2.36.0: ``{"results": [], "total": 372}``), so
		pages must be requested with a positive limit. Returns None when the
		listing cannot be fetched (best-effort contract of this module).
		"""
		out: dict[str, list[str]] = {}
		page = 0
		while True:
			data = _http_get_json(
				f"{self.base_url}/api/libraries/{library_id}/series",
				params={"limit": 500, "page": page},
				headers=self._headers,
				session=self._session,
			)
			if not isinstance(data, dict) or not isinstance(data.get("results"), list):
				return None
			for se in data["results"]:
				if not isinstance(se, dict):
					continue
				name = str(se.get("name") or "")
				if not name:
					continue
				for b in se.get("books") or []:
					if isinstance(b, dict) and b.get("id"):
						out.setdefault(str(b["id"]), []).append(name)
			try:
				total = int(data.get("total") or 0)
			except (TypeError, ValueError):
				total = 0
			page += 1
			if page * 500 >= total:
				return out

	def clear_item_series(self, item_id: str) -> bool:
		"""Remove every series from the item's ABS DB row (PATCH media metadata).

		WHY a PATCH and not just the rescan: BookScanner NEVER removes a
		series — an empty or absent series list in metadata.json is "no
		information" to the scanner, so a series DELETION decided in bmf
		(apply wrote ``series: []``) is invisible to every scan, per-item or
		forced (measured 2026-09-14 on ABS 2.36.0: 46 books with the series
		gone from disk kept their junk series in the ABS database after a
		per-item rescan that demonstrably ran). Only the metadata UPDATE path
		removes series (updateSeriesFromRequest in LibraryItemController),
		and the server itself cleans up series rows left without books
		afterwards. The list must sit at ``metadata.series`` in the payload —
		a top-level ``series`` key is silently ignored (measured: HTTP 200,
		nothing changes). Returns False on network/HTTP failure.
		"""
		r = _http_patch(
			f"{self.base_url}/api/items/{item_id}/media",
			json_body={"metadata": {"series": []}},
			headers=self._headers,
			session=self._session,
		)
		return r is not None and r.status_code == 200


# ---------------------------------------------------------------------------
# Engine: stale series rows (`abs-rescan` series sweep)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StaleSeries:
	"""A matched book whose ABS row still holds series the disk manifest dropped."""

	item: AbsItem
	series_names: tuple[str, ...]


def stale_series_items(
	matched: list[tuple[Path, AbsItem]], series_map: dict[str, list[str]], progress_callback: Any = None,
) -> list[StaleSeries]:
	"""Matched items whose ABS row holds series while the disk manifest has none.

	The DELETION half of pushing series into ABS: renames and re-sequencings
	propagate through the per-item scan itself (it re-reads metadata.json),
	but a REMOVED series does not (see AudiobookshelfClient.clear_item_series),
	so abs-rescan diffs every matched book's series against the ABS rows and
	clears the leftovers via the media PATCH. Only the no-series-on-disk case
	is actionable on purpose:

	- a non-empty series difference is the scan's own job, and
	- parse-limited shapes cannot be PATCHed durably: an index containing a
	  space ("John Sinclair #Speciál 07") is kept WHOLE as the series name
	  by the ABS parser (its sequence regex ``/ #([^#\\s]+)$/`` wants a
	  single word), so a PATCH splitting it into name+sequence would be
	  re-glued by the next scan — the durable fix is renaming the series or
	  its index in bmf.

	A book whose metadata.json cannot be read is skipped: without the disk
	truth there is no verdict, and clearing must never happen on a guess.
	*series_map* is AudiobookshelfClient.item_series_map's output; an item
	missing from it simply holds no ABS series. *progress_callback*
	(done, total) mirrors broken_cover_items — one metadata.json read per
	matched book over NFS wants a bar.
	"""
	out: list[StaleSeries] = []
	total = len(matched)
	done = 0
	for folder, item in matched:
		names = series_map.get(item.id) or []
		if names and _disk_series_empty(folder):
			out.append(StaleSeries(item=item, series_names=tuple(names)))
		done += 1
		if progress_callback is not None:
			progress_callback(done, total)
	return out


def _disk_series_empty(folder: Path) -> bool:
	"""True when the folder's metadata.json lists no series entry.

	False means "has a series" OR "cannot be read" — deliberately the same
	safe answer, the caller must never clear on an unreadable manifest.
	A plain json read on purpose: the question is only whether the source of
	truth still lists a series, and readers.read_book_folder would drag in
	the OPF fallback and the rest of the stack. The wild manifest shapes
	(plain "Name #N" strings, {name, index} dicts, the legacy `sequence`
	key, a bare string) are normalised by models.series_entry_pair.
	"""
	try:
		data = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return False
	series = data.get("series") if isinstance(data, dict) else None
	if series is None:
		return True
	if isinstance(series, dict):
		series = [series]
	if isinstance(series, str):
		series = [series]
	if not isinstance(series, list):
		return False
	return not any(name.strip() for name, _idx in (series_entry_pair(s) for s in series))


# ---------------------------------------------------------------------------
# Engine: find changed book folders and map them to ABS items
# ---------------------------------------------------------------------------


def parse_since_duration(text: str) -> float:
	"""Parse a --since window ('45s', '90m', '2h', '3d'; plain number = seconds)."""
	text = (text or "").strip().lower()
	mult = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}.get(text[-1:]) if text else None
	if mult is not None:
		text = text[:-1]
	else:
		mult = 1.0
	try:
		value = float(text)
	except ValueError:
		raise ValueError(f"invalid duration: {text!r}") from None
	if value < 0:
		raise ValueError(f"negative duration: {text!r}") from None
	return value * mult


def changed_folders(library_root: Path, since_ts: float, progress_callback: Any = None) -> list[Path]:
	"""Book folders under *library_root* whose files changed at/after *since_ts*.

	The check is the max file mtime of the folder (the same signal the
	library cache uses), so a swapped cover.jpg counts even when the
	metadata sidecars were not rewritten. iter_book_folders does not read
	any file — this is a stat-only walk. The mtimes come from the machine
	bmf runs on, i.e. the same client that just wrote the files, so a stale
	NFS attribute cache on some other host cannot mislead it.

	*progress_callback* (called as ``callback(done)`` after every folder
	checked) lets the CLI drive a progress bar — the walk is the slow part
	over NFS (tens of seconds for ~5k folders) and without feedback it
	looks hung. The total is deliberately NOT part of the contract: the
	walk is lazy, folders are discovered while descending, and a counting
	pre-pass would double the stat round trips.
	"""
	out: list[Path] = []
	done = 0
	for folder in iter_book_folders(library_root):
		max_mtime, _ = _stat_folder(folder)
		if max_mtime >= since_ts:
			out.append(folder)
		done += 1
		if progress_callback is not None:
			progress_callback(done)
	return out


@dataclass
class MatchResult:
	"""Outcome of mapping changed bmf folders onto ABS library items."""

	matched: list[tuple[Path, AbsItem]] = field(default_factory=list)
	unmatched: list[Path] = field(default_factory=list)

	@property
	def item_ids(self) -> list[str]:
		"""Unique ABS item ids of the matches, in first-seen order."""
		seen: dict[str, None] = {}
		for _, item in self.matched:
			seen.setdefault(item.id, None)
		return list(seen)


# ---------------------------------------------------------------------------
# Engine: audit stored cover rows (`abs-rescan --fix-covers`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrokenCover:
	"""An ABS item whose stored coverPath row can only yield a broken cover."""

	item: AbsItem
	cover_path: str
	# "ext" (target is not an image file) | "missing" (target gone)
	# | "unreadable" (target exists but no decoder reads it)
	# | "generated" (outside-library target carrying calibre's generated
	#   marker — e.g. ABS's own cached copy of a stripped placeholder)
	reason: str


def broken_cover_items(
	items: list[AbsItem], library_root: Path, abs_folder_paths: list[str],
	progress_callback: Any = None, fetch_cover: Any = None,
) -> list[BrokenCover]:
	"""Pick the items whose ABS-DB coverPath row is junk, not a real cover.

	ABS chooses item covers from image-extension files only (cover.* first —
	BookScanner.js), but a coverPath written by an older ABS build can point
	at ANY file in the folder; while that file exists the scanner keeps the
	row forever, and every cover-cache refresh feeds the file to ffmpeg
	("Invalid data found when processing input" on metadata.json or
	cover.html). A row is broken when its target's extension is not an
	image type, or — when the target maps under an ABS library folder onto
	*library_root* — the mapped file no longer exists or no decoder reads
	it. Targets outside the library folders (ABS's own uploaded/cached
	covers under its /metadata dir) are not on our mount: with
	*fetch_cover* (AudiobookshelfClient.get_item_cover) the audit fetches
	their bytes through the API and breaks the row when the image carries
	calibre's "Generated cover" marker — the cached screenshot of a
	strip-covers cleanup. Marker-only on purpose (no pixel math): a false
	positive here deletes a cover a user uploaded through the ABS UI.
	Without *fetch_cover* those rows keep the extension check only.

	*progress_callback* (called as ``callback(done, total)`` after every
	item audited, same contract as scan_items) lets the CLI drive a progress
	bar: auditing EVERY item of a ~5k-book library costs one ``exists()``
	stat per stored cover row, another slow NFS sweep after the item
	listing itself.
	"""
	folders = [_norm_posix(f) for f in abs_folder_paths if str(f or "").strip("/")]
	total = len(items)
	out: list[BrokenCover] = []
	done = 0
	for item in items:
		broken = _broken_cover_row(item, library_root, folders, fetch_cover)
		if broken is not None:
			out.append(broken)
		done += 1
		if progress_callback is not None:
			progress_callback(done, total)
	return out


def _broken_cover_row(
	item: AbsItem, library_root: Path, folders: list[str], fetch_cover: Any = None,
) -> BrokenCover | None:
	"""The broken-cover verdict for one item (None = the row is fine)."""
	cover = _norm_posix(item.cover_path or "")
	if not cover or "/" not in cover:
		return None  # nothing stored — the scanner is free to pick a cover
	if Path(cover).suffix.lower() not in ABS_IMAGE_EXTS:
		return BrokenCover(item=item, cover_path=cover, reason="ext")
	rel = ""
	for folder in folders:
		if cover.startswith(folder + "/"):
			rel = cover[len(folder) + 1:]
			break
	if rel:
		mapped = library_root / rel
		if not mapped.exists():
			return BrokenCover(item=item, cover_path=cover, reason="missing")
		# The file is there and image-named — but a cover that no decoder can
		# READ (a 0-byte leftover, a truncated download) feeds ffmpeg the same
		# "Invalid data found" as the missing/extension cases above. Measured
		# on the real library: 746 zero-byte cover.jpg rows passed the old
		# exists()-only audit as healthy.
		from .covers import image_is_readable

		if not image_is_readable(mapped):
			return BrokenCover(item=item, cover_path=cover, reason="unreadable")
		# STALE-CACHE check. The API serves ABS's CACHED cover, and that cache
		# survives every row-level healing (a null row's delete no longer
		# reaches the purge branch; a rescan re-caches only what it re-picks).
		# Measured 2026-09-17: a book whose disk cover had become a small but
		# REAL thumbnail (100x169) kept serving the old cached 400x565 DBK
		# page-scan — the row looked healthy, the disk was healthy, only the
		# served image was junk. Stale caches concentrate in exactly that
		# thumbnail class (the big real covers were re-picked and re-cached by
		# earlier rescans), so ONLY a disk cover under 400 px on its shorter
		# side pays the extra GET. Junk verdicts are the unambiguous page
		# shapes only (marker / vendor / text_page / doc_scan) — a minimalist
		# few-colour cache entry is an accepted real cover, not a break.
		if fetch_cover is not None:
			from .covers import analyze_cover

			info = analyze_cover(mapped)
			if info.width and min(info.width, info.height) < 400:
				data = fetch_cover(item.id)
				if data:
					from .covers import analyze_cover_bytes, is_calibre_generated_cover

					if is_calibre_generated_cover(data):
						return BrokenCover(item=item, cover_path=cover, reason="generated")
					served = analyze_cover_bytes(data)
					if served.is_generated and any(
						s.startswith(("text_page", "doc_scan", "vendor_placeholder"))
						for s in served.signals
					):
						return BrokenCover(item=item, cover_path=cover, reason="generated")
		return None
	# Outside every library folder: the target is not on our mount (ABS's own
	# /metadata cache or an uploaded cover). Existence cannot be judged — but
	# when the caller can fetch the bytes, calibre's generated marker is
	# decisive and unfalsifiable; a fetch failure or a marker-free image
	# leaves the row alone (never clear a possibly-user-uploaded cover on a
	# guess).
	if fetch_cover is not None:
		data = fetch_cover(item.id)
		if data:
			from .covers import is_calibre_generated_cover

			if is_calibre_generated_cover(data):
				return BrokenCover(item=item, cover_path=cover, reason="generated")
	return None


def _norm_posix(path: str | Path) -> str:
	"""Normalize a path for comparison (posix form, no trailing slash)."""
	return Path(str(path)).as_posix().rstrip("/")


def match_items(folders: list[Path], items: list[AbsItem], library_root: Path) -> MatchResult:
	"""Map bmf book folders to ABS items by path, in decreasing strictness.

	(a) exact absolute path — same mount, trivial case;
	(b) relPath vs the folder's path relative to *library_root* — the ABS
	    server usually mounts the very same storage at a different prefix
	    (container/NFS vs the workstation's mount), so relative is the
	    expected common ground;
	(c) unique match on the folder NAME (the last component) — a book moved
	    by apply's placement changed its whole path, but the title folder
	    (typically 'Title (id)') survives a rename; an ambiguous or missing
	    name leaves the folder unmatched rather than guessing.
	"""
	by_abs: dict[str, AbsItem] = {}
	by_rel: dict[str, AbsItem] = {}
	by_name: dict[str, list[AbsItem]] = {}
	for item in items:
		if item.path:
			by_abs.setdefault(_norm_posix(item.path), item)
		# No relPath fallback derivation from path — the ABS-side library
		# prefix is unknown here, so a guessed relative path would be wrong.
		if item.rel_path:
			by_rel.setdefault(_norm_posix(item.rel_path), item)
		name = (item.rel_path or item.path).rstrip("/").rsplit("/", 1)[-1]
		if name:
			by_name.setdefault(name, []).append(item)

	result = MatchResult()
	for folder in folders:
		try:
			rel = _norm_posix(folder.relative_to(library_root))
		except ValueError:
			rel = ""
		item = by_abs.get(_norm_posix(folder))
		if item is None and rel:
			item = by_rel.get(rel)
		if item is None:
			candidates = by_name.get(folder.name, [])
			if len(candidates) == 1:
				item = candidates[0]
		if item is None:
			result.unmatched.append(folder)
		else:
			result.matched.append((folder, item))
	return result
