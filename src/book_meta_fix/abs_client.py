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
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .library import _stat_folder, iter_book_folders

log = logging.getLogger(__name__)

USER_AGENT = "book-meta-fix/0.1 (https://github.com/konikvranik/book-meta-fix)"


def _http_get_json(
	url: str, *, params: dict | None = None, timeout: float = 15.0, headers: dict[str, str] | None = None
) -> dict | None:
	"""GET returning parsed JSON, or None on any failure (network/HTTP/JSON)."""
	try:
		r = requests.get(url, params=params, timeout=timeout, headers=headers)
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
) -> requests.Response | None:
	"""POST returning the whole response, or None on network failure.

	Unlike _http_get_json the response object is returned whole: the scan
	endpoints answer with a status we must distinguish (200 delivered /
	403 non-admin token / 404 endpoint missing) and a tiny or empty body.
	"""
	try:
		return requests.post(url, params=params, json=json_body, timeout=timeout, headers=headers)
	except requests.RequestException as e:
		log.debug("HTTP POST failed for %s: %s", url, e)
		return None


@dataclass(frozen=True)
class AbsItem:
	"""An Audiobookshelf library item — the fields a rescan needs."""

	id: str
	path: str = ""  # absolute path as the ABS server sees it (its own mount)
	rel_path: str = ""  # relative to the ABS library folder
	title: str = ""


class AudiobookshelfClient:
	"""Minimal Audiobookshelf client: list libraries/items, trigger rescans."""

	def __init__(self, base_url: str, token: str) -> None:
		self.base_url = base_url.rstrip("/")
		self._headers = {
			"User-Agent": USER_AGENT,
			"Accept": "application/json",
			"Authorization": f"Bearer {token}",
		}

	def libraries(self) -> list[dict] | None:
		"""All libraries on the server (id, name, mediaType, folders)."""
		data = _http_get_json(f"{self.base_url}/api/libraries", headers=self._headers)
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
			if isinstance(media, dict) and isinstance(media.get("metadata"), dict):
				title = str(media["metadata"].get("title") or "")
			items.append(
				AbsItem(
					id=str(row["id"]),
					path=str(row.get("path") or ""),
					rel_path=str(row.get("relPath") or ""),
					title=title,
				)
			)
		return items

	def scan_items(self, item_ids: list[str]) -> bool:
		"""Ask ABS to re-scan the given items (their metadata is re-read).

		Batch endpoint first — one call, the server scans in background. A
		404 means an older ABS without it, then we fall back to per-item
		POSTs (the small sleep keeps the burst off a tiny LAN server).
		"""
		if not item_ids:
			return True
		r = _http_post(
			f"{self.base_url}/api/items/batch/scan",
			json_body={"libraryItemIds": list(item_ids)},
			headers=self._headers,
		)
		if r is not None and r.status_code == 200:
			return True
		if r is None or r.status_code != 404:
			return False
		log.debug("batch/scan missing (HTTP %s) — falling back to per-item scans", r.status_code)
		ok = True
		for i, item_id in enumerate(item_ids):
			if i:
				time.sleep(0.1)
			rr = _http_post(f"{self.base_url}/api/items/{item_id}/scan", headers=self._headers)
			if rr is None or rr.status_code != 200:
				ok = False
		return ok

	def scan_library(self, library_id: str, *, force: bool = False) -> bool:
		"""Trigger a library scan (force = re-scan every item)."""
		r = _http_post(
			f"{self.base_url}/api/libraries/{library_id}/scan",
			params={"force": 1} if force else None,
			headers=self._headers,
		)
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
