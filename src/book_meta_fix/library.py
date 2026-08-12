"""Library traversal and SQLite cache.

A "library" is a directory of Calibre-style book folders:
    <library>/<Author>/<Title> (<id>)/ {metadata.json, metadata.opf, *.epub, cover.jpg}

Traversal excludes Calibre scratch dirs, dotfiles, and (concrete) MS-Word
lock FILES. Author directories whose name starts with ``~$`` are NOT pruned:
a book whose author metadata was polluted to ``~$Foo`` lives under such a
folder and must stay visible so the C6 detector can flag it for review.
Results are cached in a SQLite database so repeated runs skip unchanged folders.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path

from .models import BookMeta
from .readers import read_book_folder

log = logging.getLogger(__name__)

# Top-level directory names to skip entirely (Calibre scratch dir).
# NOTE: `needfix` is intentionally NOT excluded — bmf organize moves broken
# books there, but they must stay visible so subsequent report/organize/apply
# runs can re-diagnose them and move fixed books back out.
_EXCLUDE_DIRS = {"temp_calibre"}

# A book folder is recognized by having metadata.opf OR metadata.json
_META_FILES = ("metadata.opf", "metadata.json")


def iter_book_folders(library: Path):
	"""Yield book folder paths under *library*, recursively.

	Descends into subdirectories at any depth so books relocated by `organize`
	(e.g. into needfix/) remain discoverable. A folder is yielded when it
	contains at least one of _META_FILES. Excluded entries (calibre scratch
	dirs, dotfiles, MS-Word lock files) are pruned throughout the tree.

	Yields paths in deterministic (name-sorted) order.
	"""
	library = Path(library)
	if not library.is_dir():
		raise FileNotFoundError(f"library not found: {library}")

	yield from _walk_for_book_folders(library)


def _walk_for_book_folders(folder: Path):
	"""Recurse into *folder*, yielding directories that hold a metadata file.

	We descend depth-first but yield in name-sorted order for determinism.
	A folder that is itself a book is not descended into further (a book's
	subdirectories are not separate books). Excluded entries are pruned.
	"""
	for entry in _scandir_sorted(folder):
		if not entry.is_dir():
			continue
		if _is_excluded(entry.name):
			continue
		if any((entry / mf).is_file() for mf in _META_FILES):
			yield entry
			continue  # book folder — don't descend into its contents
		yield from _walk_for_book_folders(entry)


def _scandir_sorted(path: Path):
	"""os.scandir results sorted by name (deterministic order)."""
	try:
		entries = list(Path(path).iterdir())
	except (PermissionError, OSError) as e:
		log.warning("cannot list %s: %s", path, e)
		return []
	return sorted(entries, key=lambda p: p.name)


def _is_excluded(name: str, *, is_file: bool = False) -> bool:
	"""Should this entry be skipped?

	For directories, the ``~$`` (MS-Word lock-file) prefix is NOT excluded —
	a book whose author metadata got polluted to ``~$Foo`` lands in a
	``~$Foo/`` author folder, and the C6 detector must be able to see it.
	Only concrete ``~$`` files (the lock files themselves) are pruned, when
	*is_file* is True.
	"""
	if name in _EXCLUDE_DIRS:
		return True
	if name.startswith("calibre-"):  # calibre-* scratch dirs
		return True
	if is_file and name.startswith("~$"):  # MS-Word lock FILES only
		return True
	if name.startswith("."):  # dotfiles / hidden
		return True
	return False


# ---------------------------------------------------------------------------
# SQLite cache
# ---------------------------------------------------------------------------


class Cache:
	"""Simple SQLite cache of parsed BookMeta, keyed by folder path + mtime.

	On load, folders whose (path, mtime, size) match the cache are reused
	without re-parsing. Mutating commands (apply, organize) must call
	invalidate()/invalidate_many() for the folders they change, so the next
	run re-parses them instead of trusting a possibly-stale mtime (e.g. on
	NFS, where the client attribute cache can serve an old mtime for seconds
	after a write).
	"""

	SCHEMA_VERSION = 1

	def __init__(self, db_path: Path):
		self.db_path = Path(db_path)
		self.conn = sqlite3.connect(str(self.db_path))
		self.conn.execute("PRAGMA journal_mode=WAL")
		self._init_schema()

	def _init_schema(self) -> None:
		self.conn.executescript(
			f"""
			CREATE TABLE IF NOT EXISTS schema_meta (
				key TEXT PRIMARY KEY,
				value TEXT NOT NULL
			);
			CREATE TABLE IF NOT EXISTS books (
				path TEXT PRIMARY KEY,
				mtime REAL NOT NULL,
				size INTEGER NOT NULL,
				payload TEXT NOT NULL,
				scanned_at REAL NOT NULL
			);
			INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', '{self.SCHEMA_VERSION}');
			"""
		)

	def get(self, folder: Path) -> BookMeta | None:
		"""Return cached BookMeta if folder mtime/size unchanged, else None."""
		row = self.conn.execute(
			"SELECT mtime, size, payload FROM books WHERE path = ?", (str(folder),)
		).fetchone()
		if row is None:
			return None
		cached_mtime, cached_size, payload = row
		cur_mtime, cur_size = _stat_folder(folder)
		if cur_mtime == cached_mtime and cur_size == cached_size:
			try:
				return _bookmeta_from_payload(payload)
			except Exception:  # noqa: BLE001
				return None
		return None

	def put(self, meta: BookMeta) -> None:
		mtime, size = _stat_folder(Path(meta.path))
		payload = _bookmeta_to_payload(meta)
		self.conn.execute(
			"INSERT OR REPLACE INTO books(path, mtime, size, payload, scanned_at) VALUES (?,?,?,?,?)",
			(str(meta.path), mtime, size, payload, time.time()),
		)

	def invalidate(self, path: str | Path) -> None:
		"""Drop the cached entry for *path* so the next scan re-parses it.

		Safe to call with a path that has no cached entry (no-op). The key is
		normalised to ``str(Path(path))`` to match get()/put().
		"""
		self.conn.execute("DELETE FROM books WHERE path = ?", (str(Path(path)),))

	def invalidate_many(self, paths) -> None:
		"""Drop cached entries for several paths at once."""
		keys = [(str(Path(p)),) for p in paths]
		if keys:
			self.conn.executemany("DELETE FROM books WHERE path = ?", keys)

	def clear(self) -> None:
		"""Drop every cached entry (the table stays)."""
		self.conn.execute("DELETE FROM books")

	def commit(self) -> None:
		self.conn.commit()

	def close(self) -> None:
		try:
			self.conn.commit()
		finally:
			self.conn.close()


def _stat_folder(folder: Path) -> tuple[float, int]:
	"""Return (max_mtime, total_size) of files in *folder* for change detection."""
	max_mtime = 0.0
	total_size = 0
	try:
		for entry in folder.iterdir():
			if entry.is_file():
				try:
					st = entry.stat()
					max_mtime = max(max_mtime, st.st_mtime)
					total_size += st.st_size
				except OSError:
					continue
	except OSError:
		pass
	return max_mtime, total_size


def _bookmeta_to_payload(meta: BookMeta) -> str:
	"""Serialize BookMeta to a JSON payload for the cache."""
	d = meta.to_dict()
	# Keep payload small/stable: drop filesystem-derived path-only noise if needed
	return json.dumps(d, ensure_ascii=False, sort_keys=True)


def _bookmeta_from_payload(payload: str) -> BookMeta:
	"""Deserialize a BookMeta from a JSON payload."""
	from dataclasses import fields

	d = json.loads(payload)
	valid = {f.name for f in fields(BookMeta)}
	kwargs = {k: v for k, v in d.items() if k in valid}
	return BookMeta(**kwargs)


# ---------------------------------------------------------------------------
# High-level scan
# ---------------------------------------------------------------------------


def scan_library(
	library: Path,
	cache: Cache | None = None,
	use_cache: bool = True,
	progress_callback=None,
) -> list[BookMeta]:
	"""Scan the whole library and return a list of BookMeta.

	If *cache* is given and *use_cache* is True, unchanged folders are loaded
	from the cache instead of re-parsing.

	*progress_callback*, if given, is called as ``callback(done)`` after each
	book folder is processed (cache hit or fresh parse), with the running count
	(1-based). The caller knows the total (e.g. from a pre-count) and can drive
	a progress bar with ETA.
	"""
	library = Path(library)
	results: list[BookMeta] = []
	n_cached = n_fresh = 0
	done = 0  # folders processed (cache hit + fresh parse + errors)

	for folder in iter_book_folders(library):
		if use_cache and cache is not None:
			cached = cache.get(folder)
			if cached is not None:
				results.append(cached)
				n_cached += 1
				done += 1
				if progress_callback is not None:
					progress_callback(done)
				continue
		try:
			meta = read_book_folder(folder)
		except Exception as e:  # noqa: BLE001
			log.error("failed to read %s: %s", folder, e)
			done += 1
			if progress_callback is not None:
				progress_callback(done)
			continue
		results.append(meta)
		if cache is not None:
			cache.put(meta)
		n_fresh += 1
		done += 1
		if progress_callback is not None:
			progress_callback(done)

	if cache is not None:
		cache.commit()
	log.info("scan: %d books (%d cached, %d fresh)", len(results), n_cached, n_fresh)
	return results
