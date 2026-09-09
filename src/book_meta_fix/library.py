"""Library traversal and SQLite cache.

A "library" is a directory of Calibre-style book folders:
    <library>/<Author>/<Title> (<id>)/ {metadata.json, metadata.opf, *.epub, cover.jpg}

Traversal excludes Calibre scratch dirs, dotfiles, and (concrete) MS-Word
lock FILES. Author directories whose name starts with ``~$`` are NOT pruned:
a book whose author metadata got polluted to ``~$Foo`` lives under such a
folder and must stay visible so the C6 detector can flag it for review.
Results are cached in a SQLite database so repeated runs skip unchanged folders.

The scan is threaded by default (see DEFAULT_SCAN_WORKERS): on NFS every
directory listing and file stat is a network round trip, so a serial walk of
~5500 book folders costs minutes; the GUI's index build measured the walk
alone at 38 s over NFS v3. All per-folder work here is either read-only or
per-folder-atomic (uuid minting), so threads are safe.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .models import BookMeta
from .readers import read_book_folder
from .writers import ensure_uuid

log = logging.getLogger(__name__)

# Top-level directory names to skip entirely (Calibre scratch dir).
# NOTE: `needfix` is intentionally NOT excluded — bmf organize moves broken
# books there, but they must stay visible so subsequent report/organize/apply
# runs can re-diagnose them and move fixed books back out.
_EXCLUDE_DIRS = {"temp_calibre"}

# A book folder is recognized by having metadata.opf OR metadata.json
_META_FILES = ("metadata.opf", "metadata.json")

# Default thread count for the parallel scan (walk + per-folder reads).
# NFS latency, not CPU, is the scan's bottleneck, so a small pool hides the
# round trips well; 8 measured as a good default on the real library. 1 in
# run_pipeline/scan_library restores the historical serial behaviour.
DEFAULT_SCAN_WORKERS = 8


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


def iter_book_folders_parallel(library: Path, workers: int) -> list[Path]:
	"""``iter_book_folders`` with the tree walk split per top-level directory.

	``iter_book_folders`` alone costs tens of seconds on the real library
	(measured: 38 s for 5339 folders over NFS v3 — each directory entry
	probes for a metadata sidecar, one RPC round trip each). Splitting the
	walk by top-level folder and running the subtrees in a small pool cuts
	that to a few seconds. Exclusions are applied to the top level manually
	(iter_book_folders applies them only below the root). The concatenated
	result keeps the same deterministic order as the serial walk.
	"""
	library = Path(library)
	try:
		tops = sorted(
			e.path for e in os.scandir(library)
			if e.is_dir() and not _is_excluded(e.name)
		)
	except OSError:
		return []

	def _walk(top: str) -> list[Path]:
		top_path = Path(top)
		# A top-level directory may itself be a BOOK folder (a book sitting
		# directly in the library root) — _walk_for_book_folders treats its
		# argument as a container, so test the top itself first and don't
		# descend into it (the same rule the walk applies below the root).
		if any((top_path / mf).is_file() for mf in _META_FILES):
			return [top_path]
		return list(_walk_for_book_folders(top_path))

	if workers <= 1:
		return [f for top in tops for f in _walk(top)]
	with ThreadPoolExecutor(max_workers=workers) as ex:
		parts = list(ex.map(_walk, tops))
	return [f for part in parts for f in part]


def _walk_for_book_folders(folder: Path):
	"""Recurse into *folder*, yielding directories that hold a metadata file.

	We descend depth-first but yield in name-sorted order for determinism.
	A folder that is itself a book is not descended into further (a book's
	subdirectories are not separate books). Excluded entries are pruned.
	"""
	for entry in _scandir_sorted(folder):
		# DirEntry.is_dir() is free on Linux (d_type from readdir) — unlike
		# Path.is_dir() it costs no extra stat RPC on NFS.
		if not entry.is_dir():
			continue
		if _is_excluded(entry.name):
			continue
		probe = Path(entry.path)
		if any((probe / mf).is_file() for mf in _META_FILES):
			yield probe
			continue  # book folder — don't descend into its contents
		yield from _walk_for_book_folders(probe)


def _scandir_sorted(path: Path):
	"""os.scandir entries sorted by name (deterministic order)."""
	try:
		with os.scandir(path) as it:
			entries = list(it)
	except (PermissionError, OSError) as e:
		log.warning("cannot list %s: %s", path, e)
		return []
	return sorted(entries, key=lambda e: e.name)


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


class CacheError(RuntimeError):
	"""Raised when the SQLite cache database cannot be opened or initialized."""


class Cache:
	"""SQLite cache of parsed BookMeta records, keyed by folder path.

	Records are kept by ABS UUID primary key (surviving outside-of-bmf
	folder renames/moves), but the LOOKUP is by ``path`` — the only key
	available cheaply from the directory walk, before any metadata is parsed.
	On load, a folder whose :func:`_folder_fingerprint` still matches the
	cache is reused without re-parsing. Mutating commands must call
	invalidate() / invalidate_many() for folders they change (apply), or
	repoint() for a move/rename (organize), so the cache never serves a
	stale entry — important on NFS, where the client attribute cache can
	mask a new mtime.

	Thread-safe for the parallel scan: the single connection is opened with
	``check_same_thread=False`` (same pattern as the Enricher's cache) and
	every SQL statement runs under :attr:`_lock`. Filesystem I/O
	(:func:`_stat_folder`, reads, uuid minting) happens OUTSIDE the lock —
	that is the part the scan's thread pool exists to overlap.
	"""

	SCHEMA_VERSION = 3

	def __init__(self, db_path: Path):
		self.db_path = Path(db_path)
		self._lock = threading.Lock()
		self._verified_authors_cache: list[str] | None = None
		self._verified_series_cache: list[str] | None = None
		try:
			if not self.db_path.parent.exists():
				try:
					self.db_path.parent.mkdir(parents=True, exist_ok=True)
				except OSError as e:
					raise CacheError(
						f"Cannot create directory for cache database '{self.db_path.parent}': {e}"
					) from e
			self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
			self.conn.execute("PRAGMA journal_mode=WAL")
			self._init_schema()
		except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
			raise CacheError(
				f"Unable to open cache database '{self.db_path}': {e}"
			) from e

	def _init_schema(self) -> None:
		self.conn.executescript(
			"""
			CREATE TABLE IF NOT EXISTS schema_meta (
				key TEXT PRIMARY KEY,
				value TEXT NOT NULL
			);
			"""
		)
		# Cover-analysis verdicts (covers.py analyze_cover): decoding a JPEG
		# with Pillow is the most expensive thing the detector does per book,
		# so an unchanged cover.jpg must never be re-decoded — not even in the
		# next run. Same (mtime, size) invalidation contract as the books
		# table; CREATE IF NOT EXISTS so old caches gain it without a drop.
		self.conn.executescript(
			"""
			CREATE TABLE IF NOT EXISTS covers (
				path TEXT PRIMARY KEY,
				mtime_ns INTEGER NOT NULL,
				size INTEGER NOT NULL,
				payload TEXT NOT NULL
			);
			"""
		)
		# Migrate on version mismatch (including a fresh db): the cache is
		# disposable, so we drop+recreate — the next scan rebuilds it and, via
		# ensure_uuid, backfills the uuid for every book. v3 changed the books
		# validation columns from (mtime, size) of a per-file folder scan to
		# the slim fingerprint (dir_mtime, meta_mtime, meta_size — see
		# _folder_fingerprint).
		row = self.conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
		current = int(row[0]) if row and str(row[0]).isdigit() else 0
		if current != self.SCHEMA_VERSION:
			self.conn.executescript(
				"""
				DROP TABLE IF EXISTS books;
				CREATE TABLE books (
					uuid TEXT PRIMARY KEY,
					path TEXT NOT NULL,
					dir_mtime REAL NOT NULL,
					meta_mtime REAL NOT NULL,
					meta_size INTEGER NOT NULL,
					payload TEXT NOT NULL,
					scanned_at REAL NOT NULL
				);
				CREATE UNIQUE INDEX IF NOT EXISTS idx_books_path ON books(path);
				"""
			)
			self.conn.execute(
				"INSERT INTO schema_meta(key, value) VALUES ('version', ?) "
				"ON CONFLICT(key) DO UPDATE SET value = excluded.value",
				(str(self.SCHEMA_VERSION),),
			)
		# Commit any pending write (the version UPDATE above starts a transaction
		# that executescript would otherwise leave open). bmf_cache.db is shared
		# with the Enricher (a second connection in the same process); an
		# uncommitted transaction here would lock out its CREATE TABLE and raise
		# "database is locked" at Enricher.__init__.
		self.conn.commit()

	def is_verified_author(self, name: str) -> bool:
		"""Return True if the exact author exists on any `verified` book in the library."""
		with self._lock:
			cur = self.conn.execute('''
				SELECT 1
				FROM books, json_each(json_extract(payload, '$.authors'))
				WHERE json_extract(payload, '$.verified') = 1
				  AND lower(value) = lower(?)
				LIMIT 1
			''', (name,))
			return cur.fetchone() is not None

	def is_verified_series(self, name: str) -> bool:
		"""Return True if the exact series exists on any `verified` book in the library."""
		from .models import series_entry_pair

		with self._lock:
			cur = self.conn.execute('''
				SELECT json_extract(payload, '$.series')
				FROM books
				WHERE json_extract(payload, '$.verified') = 1
			''')
			name_lower = name.lower()
			for (series_json,) in cur:
				if not series_json:
					continue
				try:
					series_arr = json.loads(series_json)
					if not isinstance(series_arr, list):
						continue
					for s in series_arr:
						s_name, _ = series_entry_pair(s)
						if s_name.lower() == name_lower:
							return True
				except json.JSONDecodeError:
					continue
			return False

	def get_verified_authors(self) -> list[str]:
		"""Return list of distinct author names from books marked verified."""
		with self._lock:
			if self._verified_authors_cache is not None:
				return self._verified_authors_cache
			cur = self.conn.execute('''
				SELECT DISTINCT value
				FROM books, json_each(json_extract(payload, '$.authors'))
				WHERE json_extract(payload, '$.verified') = 1
			''')
			authors = [str(r[0]).strip() for r in cur if r[0] and str(r[0]).strip()]
			self._verified_authors_cache = authors
			return authors

	def get_verified_series(self) -> list[str]:
		"""Return list of distinct series names from books marked verified."""
		from .models import series_entry_pair

		with self._lock:
			if self._verified_series_cache is not None:
				return self._verified_series_cache
			cur = self.conn.execute('''
				SELECT json_extract(payload, '$.series')
				FROM books
				WHERE json_extract(payload, '$.verified') = 1
			''')
			names: set[str] = set()
			for (series_json,) in cur:
				if not series_json:
					continue
				try:
					series_arr = json.loads(series_json)
					if isinstance(series_arr, list):
						for s in series_arr:
							s_name, _ = series_entry_pair(s)
							if s_name and s_name.strip():
								names.add(s_name.strip())
				except json.JSONDecodeError:
					continue
			series_list = sorted(names)
			self._verified_series_cache = series_list
			return series_list

	def find_similar_verified_authors(self, query: str, limit: int = 3, cutoff: int = 75) -> list[str]:
		"""Find verified authors similar to *query* using token sort ratio."""
		from rapidfuzz import fuzz, process, utils

		if not query or not query.strip():
			return []
		authors = self.get_verified_authors()
		if not authors:
			return []
		q = query.strip()
		matches = process.extract(
			q,
			authors,
			scorer=fuzz.token_sort_ratio,
			processor=utils.default_process,
			limit=limit,
			score_cutoff=cutoff,
		)
		results = [m[0] for m in matches]
		# If query is a single word (e.g. surname "Asimov"), also match verified authors
		# who have this surname (first or last token).
		q_tokens = q.split()
		if len(q_tokens) == 1 and len(q) >= 3 and len(results) < limit:
			q_norm = q.lower()
			for a in authors:
				if a in results:
					continue
				a_tokens = [t.rstrip(".,").lower() for t in a.split() if t]
				if a_tokens and (a_tokens[0] == q_norm or a_tokens[-1] == q_norm):
					results.append(a)
					if len(results) >= limit:
						break
		return results

	def find_similar_verified_series(self, query: str, limit: int = 3, cutoff: int = 75) -> list[str]:
		"""Find verified series similar to *query* using token set ratio."""
		from rapidfuzz import fuzz, process, utils

		if not query or not query.strip():
			return []
		series_list = self.get_verified_series()
		if not series_list:
			return []
		matches = process.extract(
			query.strip(),
			series_list,
			scorer=fuzz.token_set_ratio,
			processor=utils.default_process,
			limit=limit,
			score_cutoff=cutoff,
		)
		return [m[0] for m in matches]

	def get(self, folder: Path) -> BookMeta | None:
		"""Return cached BookMeta if the folder fingerprint is unchanged, else None."""
		with self._lock:
			row = self.conn.execute(
				"SELECT dir_mtime, meta_mtime, meta_size, payload FROM books WHERE path = ?", (str(folder),)
			).fetchone()
		if row is None:
			return None
		if _folder_fingerprint(folder) == (row[0], row[1], row[2]):
			try:
				return _bookmeta_from_payload(row[3])
			except Exception:  # noqa: BLE001
				return None
		return None

	def load_all(self) -> dict[str, tuple[float, float, int, str]]:
		"""All rows as ``{path: (dir_mtime, meta_mtime, meta_size, payload)}``.

		One SELECT for the whole scan instead of a per-folder query: thousands
		of single-row lookups under the connection lock measured ~1 s per scan
		— pure overhead next to batching them. The dict is read-only once
		returned (worker threads only ``.get()`` it).
		"""
		with self._lock:
			cur = self.conn.execute("SELECT path, dir_mtime, meta_mtime, meta_size, payload FROM books")
			return {str(r[0]): (r[1], r[2], r[3], r[4]) for r in cur}

	def put(self, meta: BookMeta) -> None:
		if meta.uuid is None:
			# No uuid -> no stable PK. Should not happen: scan calls ensure_uuid
			# before put. Guard so a None never lands in the unique index.
			log.warning("cache.put without uuid for %s; skipping", meta.path)
			return
		dir_mtime, meta_mtime, meta_size = _folder_fingerprint(Path(meta.path))
		payload = _bookmeta_to_payload(meta)
		with self._lock:
			self.conn.execute(
				"INSERT OR REPLACE INTO books(uuid, path, dir_mtime, meta_mtime, meta_size, payload, scanned_at) VALUES (?,?,?,?,?,?,?)",
				(meta.uuid, str(meta.path), dir_mtime, meta_mtime, meta_size, payload, time.time()),
			)

	def get_cover(self, path: str | Path, mtime_ns: int, size: int) -> dict | None:
		"""Return the stored cover-verdict payload dict if (mtime_ns, size) match.

		Used by :func:`covers.analyze_cover` through its attached persistent
		store (see ``covers.set_cover_cache``); the payload is a plain dict so
		neither module needs to import the other's types.
		"""
		with self._lock:
			row = self.conn.execute(
				"SELECT mtime_ns, size, payload FROM covers WHERE path = ?",
				(str(Path(path)),),
			).fetchone()
		if row is None:
			return None
		c_mtime, c_size, payload = row
		if c_mtime == mtime_ns and c_size == size:
			try:
				return json.loads(payload)
			except Exception:  # noqa: BLE001
				return None
		return None

	def put_cover(self, path: str | Path, mtime_ns: int, size: int, payload: dict) -> None:
		"""Store a cover-verdict payload keyed by path + (mtime_ns, size)."""
		with self._lock:
			self.conn.execute(
				"INSERT OR REPLACE INTO covers(path, mtime_ns, size, payload) VALUES (?,?,?,?)",
				(str(Path(path)), mtime_ns, size, json.dumps(payload, ensure_ascii=False)),
			)
			self.conn.commit()

	def invalidate(self, path: str | Path) -> None:
		"""Drop the cached entry for *path* so the next scan re-parses it.

		Safe to call with a path that has no cached entry (no-op). The key is
		normalised to ``str(Path(path))`` to match get()/put().
		"""
		with self._lock:
			self._verified_authors_cache = None
			self._verified_series_cache = None
			self.conn.execute("DELETE FROM books WHERE path = ?", (str(Path(path)),))
			self.conn.commit()

	def invalidate_many(self, paths) -> None:
		"""Drop cached entries for several paths at once."""
		keys = [(str(Path(p)),) for p in paths]
		if keys:
			with self._lock:
				self._verified_authors_cache = None
				self._verified_series_cache = None
				self.conn.executemany("DELETE FROM books WHERE path = ?", keys)
				self.conn.commit()

	def repoint(self, src_path: str | Path, dst_path: str | Path) -> bool:
		"""Re-attach a cached entry from *src_path* to *dst_path* (folder moved).

		Used by organize so a book's cache row FOLLOWS it across a move/rename
		instead of being dropped — the metadata did not change, only the
		location, so re-parsing would be wasted work. Updates both the ``path``
		column and the ``path`` baked into the payload. Any stale row already at
		the destination is removed first so the UNIQUE(path) index holds.
		Returns True if a row was repointed, False if none matched *src_path*.
		"""
		src = str(Path(src_path))
		dst = str(Path(dst_path))
		with self._lock:
			row = self.conn.execute("SELECT payload FROM books WHERE path = ?", (src,)).fetchone()
			if row is None:
				return False
			payload = row[0]
			try:
				meta = _bookmeta_from_payload(payload)
				meta.path = dst
				new_payload = _bookmeta_to_payload(meta)
			except Exception:  # noqa: BLE001
				new_payload = payload  # keep old payload; path column is what matters
			self.conn.execute("DELETE FROM books WHERE path = ?", (dst,))
			self.conn.execute("UPDATE books SET path = ?, payload = ? WHERE path = ?", (dst, new_payload, src))
		return True

	def clear(self) -> None:
		"""Drop every cached entry (the table stays)."""
		with self._lock:
			self.conn.execute("DELETE FROM books")

	def commit(self) -> None:
		with self._lock:
			self.conn.commit()

	def close(self) -> None:
		with self._lock:
			try:
				self.conn.commit()
			finally:
				self.conn.close()


def _folder_fingerprint(folder: Path) -> tuple[float, float, int]:
	"""Cheap change fingerprint of a book folder: ``(dir_mtime, meta_mtime, meta_size)``.

	The cached payload depends on exactly two things: the folder's FILE LIST
	(formats / primary file — any add, remove or rename bumps the directory's
	own mtime) and the content of the metadata source (``metadata.json``, else
	the OPF fallback — the reader's precedence). Two stat RPCs cover both.

	This replaced a per-file scan of the whole folder (max mtime + total size,
	~7 GETATTRs per book): on the real NFS v3 library that measured ~100 s per
	scan — dominant over everything else in the load — and immune to threading
	because the NAS serializes concurrent GETATTRs. Cover/asset files
	deliberately do NOT participate: they cannot change the parsed BookMeta,
	and cover replacement (apply / strip-covers) used to falsely invalidate
	rows. abs_client's changed_folders still wants per-file mtimes and keeps
	using :func:`_stat_folder`.
	"""
	try:
		dir_mtime = os.stat(folder).st_mtime
	except OSError:
		return (0.0, 0.0, 0)
	for name in ("metadata.json", "metadata.opf"):
		try:
			st = os.stat(folder / name)
		except OSError:
			continue
		return (dir_mtime, st.st_mtime, st.st_size)
	return (dir_mtime, 0.0, 0)


def _stat_folder(folder: Path) -> tuple[float, int]:
	"""Return (max_mtime, total_size) of files in *folder*.

	Used by abs_client's ``changed_folders`` (stat-only change detection over
	the tree), NOT for cache validation — that is :func:`_folder_fingerprint`
	since the per-file GETATTR storm measured ~100 s per scan over NFS.
	"""
	max_mtime = 0.0
	total_size = 0
	try:
		# os.scandir's DirEntry caches each entry's stat after the first call,
		# so is_file()+stat() below costs ONE stat syscall per file — a
		# Path-based iterdir would pay two, and on NFS every stat is a
		# network round trip.
		with os.scandir(folder) as it:
			for entry in it:
				try:
					if not entry.is_file():
						continue
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
	workers: int = DEFAULT_SCAN_WORKERS,
) -> list[BookMeta]:
	"""Scan the whole library and return a list of BookMeta.

	If *cache* is given and *use_cache* is True, unchanged folders are loaded
	from the cache instead of re-parsing.

	*workers* threads parallelize both the tree walk (per top-level
	directory — measured 38 s -> a few seconds over NFS v3) and the
	per-folder work (cache stat / metadata parse / uuid mint), which is
	either read-only or per-folder atomic, so threads are safe. The result
	comes back in deterministic path-sorted order regardless of completion
	order. ``workers=1`` restores the historical serial scan.

	*progress_callback*, if given, is called as ``callback(done, total)`` after
	each book folder is processed (cache hit or fresh parse), with the running
	1-based count and the total folder count. In the parallel scan it fires
	from the consuming main thread in COMPLETION order (monotonic counts),
	which matches the contract the abs-rescan progress bar already follows.
	The folder list is materialized upfront (one dir walk) precisely so
	*total* is known and reported from the very first call — letting a
	progress bar show an ETA immediately instead of pulsing blindly.
	"""
	library = Path(library)
	# Materialize the folder list once so we know the total up front (lets the
	# caller render a determinate bar with ETA from the first callback). The
	# walk runs in the same pool (split per top-level dir) — its readdir +
	# metadata-file probes are NFS round trips too.
	if workers > 1:
		folders = iter_book_folders_parallel(library, workers)
	else:
		folders = list(iter_book_folders(library))
	total = len(folders)
	# One batched SELECT up front (see Cache.load_all): the per-folder lookup
	# under the connection lock was ~1 s of pure overhead per scan. Read-only
	# afterwards — the worker threads only .get() it.
	rows = cache.load_all() if (use_cache and cache is not None) else {}
	results: list[BookMeta] = []
	n_cached = n_fresh = 0
	done = 0  # folders processed (cache hit + fresh parse + errors)

	def _scan_one(folder: Path) -> tuple[BookMeta | None, bool]:
		"""Process one folder: cache hit, or fresh parse (+uuid mint+cache)."""
		row = rows.get(str(folder))
		if row is not None and _folder_fingerprint(folder) == row[:3]:
			try:
				return _bookmeta_from_payload(row[3]), True
			except Exception:  # noqa: BLE001
				pass  # corrupt payload — fall through to a fresh parse
		try:
			meta = read_book_folder(folder)
		except Exception as e:  # noqa: BLE001
			log.error("failed to read %s: %s", folder, e)
			return None, False
		# Lazily mint + persist a uuid the first time a book is parsed (it is
		# needed as the cache PK and the review identity). Non-fatal: a write
		# error must never abort the scan — the book is still usable in-memory.
		if meta.uuid is None:
			try:
				ensure_uuid(meta)
			except Exception as e:  # noqa: BLE001
				log.warning("could not ensure uuid for %s: %s", folder, e)
		if cache is not None:
			cache.put(meta)
		return meta, False

	if workers > 1 and total > 1:
		interrupted = False
		futures = []
		with ThreadPoolExecutor(max_workers=min(workers, total)) as pool:
			try:
				futures = [pool.submit(_scan_one, f) for f in folders]
				for fut in as_completed(futures):
					meta, from_cache = fut.result()
					if meta is not None:
						results.append(meta)
						if from_cache:
							n_cached += 1
						else:
							n_fresh += 1
					done += 1
					if progress_callback is not None:
						progress_callback(done, total)
			except KeyboardInterrupt:
				interrupted = True
				for f in futures:
					f.cancel()
		if interrupted:
			log.warning(
				"scan interrupted by user after %d/%d folders; keeping partial results", done, total,
			)
		# Completion order is nondeterministic; restore the walk's path order
		# so `limit`, logs and review entry order stay stable across runs.
		results.sort(key=lambda m: m.path)
	else:
		for folder in folders:
			meta, from_cache = _scan_one(folder)
			if meta is not None:
				results.append(meta)
				if from_cache:
					n_cached += 1
				else:
					n_fresh += 1
			done += 1
			if progress_callback is not None:
				progress_callback(done, total)

	if cache is not None:
		cache.commit()
	log.info("scan: %d books (%d cached, %d fresh)", len(results), n_cached, n_fresh)
	return results
