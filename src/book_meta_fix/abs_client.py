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

	def clear_item_cover(self, item_id: str) -> bool:
		"""Null an item's stored cover row (DELETE /api/items/{id}/cover).

		The ABS-database counterpart of the strip-covers file cleanup: a stale
		``media.coverPath`` (ffmpeg fed metadata.json as the cover) never
		self-heals, because the scanner only re-picks a cover when the row is
		NULL or its file vanished — and a metadata.json neither vanishes nor
		counts as an image file. The DELETE nulls the row and purges ABS's
		cover cache; the caller follows up with a rescan so ABS picks a real
		cover from the folder again. Returns False on network/HTTP failure.
		"""
		r = _http_delete(f"{self.base_url}/api/items/{item_id}/cover", headers=self._headers, session=self._session)
		return r is not None and r.status_code == 200


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


def changed_folders(library_root: Path, since_ts: float) -> list[Path]:
	"""Book folders under *library_root* whose files changed at/after *since_ts*.

	The check is the max file mtime of the folder (the same signal the
	library cache uses), so a swapped cover.jpg counts even when the
	metadata sidecars were not rewritten. iter_book_folders does not read
	any file — this is a stat-only walk. The mtimes come from the machine
	bmf runs on, i.e. the same client that just wrote the files, so a stale
	NFS attribute cache on some other host cannot mislead it.
	"""
	out: list[Path] = []
	for folder in iter_book_folders(library_root):
		max_mtime, _ = _stat_folder(folder)
		if max_mtime >= since_ts:
			out.append(folder)
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
	reason: str  # "ext" (target is not an image file) | "missing" (target gone)


def broken_cover_items(
	items: list[AbsItem], library_root: Path, abs_folder_paths: list[str],
) -> list[BrokenCover]:
	"""Pick the items whose ABS-DB coverPath row is junk, not a real cover.

	ABS chooses item covers from image-extension files only (cover.* first —
	BookScanner.js), but a coverPath written by an older ABS build can point
	at ANY file in the folder; while that file exists the scanner keeps the
	row forever, and every cover-cache refresh feeds the file to ffmpeg
	("Invalid data found when processing input" on metadata.json or
	cover.html). A row is broken when its target's extension is not an
	image type, or — when the target maps under an ABS library folder onto
	*library_root* — the mapped file no longer exists. Targets outside the
	library folders (ABS's own uploaded covers under its /metadata dir) get
	the extension check only: their storage belongs to the ABS server, not
	to our mount.
	"""
	folders = [_norm_posix(f) for f in abs_folder_paths if str(f or "").strip("/")]
	out: list[BrokenCover] = []
	for item in items:
		cover = _norm_posix(item.cover_path or "")
		if not cover or "/" not in cover:
			continue  # nothing stored — the scanner is free to pick a cover
		if Path(cover).suffix.lower() not in ABS_IMAGE_EXTS:
			out.append(BrokenCover(item=item, cover_path=cover, reason="ext"))
			continue
		rel = ""
		for folder in folders:
			if cover.startswith(folder + "/"):
				rel = cover[len(folder) + 1:]
				break
		if rel and not (library_root / rel).exists():
			out.append(BrokenCover(item=item, cover_path=cover, reason="missing"))
	return out


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
