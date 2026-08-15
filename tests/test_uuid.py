"""Tests for the bmf uuid: the stable per-book identity.

The uuid lives in metadata.json (source of truth), is mirrored to metadata.opf,
and is the single key used by the review system (carry-over + prune) and the
cache (PK). These tests pin the cross-cutting contract that spans readers,
writers, and the cache, plus the lazy-mint lifecycle.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from book_meta_fix.library import Cache, scan_library
from book_meta_fix.models import BookMeta
from book_meta_fix.readers import read_book_folder
from book_meta_fix.writers import _render_json, ensure_uuid, write_book_meta


def _opf(uuid_ident: str | None = None, title: str = "T") -> str:
	"""A minimal valid OPF, optionally carrying a uuid identifier."""
	uuid_el = (
		f'<dc:identifier opf:scheme="uuid" id="uuid_id">{uuid_ident}</dc:identifier>'
		if uuid_ident else ""
	)
	return (
		'<?xml version="1.0"?>'
		'<package xmlns="http://www.idpf.org/2007/opf" xmlns:opf="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">'
		f'<metadata><dc:title>{title}</dc:title>{uuid_el}</metadata></package>'
	)


# ---------------------------------------------------------------------------
# readers: uuid is sourced from json (primary) and opf (fallback)
# ---------------------------------------------------------------------------


class TestReadUuid:
	def test_json_uuid_read(self, tmp_path: Path) -> None:
		folder = tmp_path / "A" / "T (1)"
		folder.mkdir(parents=True)
		(folder / "metadata.json").write_text('{"uuid": "from-json", "title": "T"}', encoding="utf-8")
		assert read_book_folder(folder).uuid == "from-json"

	def test_opf_uuid_used_for_opf_only_book(self, tmp_path: Path) -> None:
		"""An opf-only book (no metadata.json) gets its uuid from the OPF
		identifier. (When a manifest is present it is read exclusively, so the
		opf fallback matters only for calibre-only / opf-only books.)"""
		folder = tmp_path / "A" / "T (1)"
		folder.mkdir(parents=True)
		(folder / "metadata.opf").write_text(_opf(uuid_ident="from-opf"), encoding="utf-8")
		assert read_book_folder(folder).uuid == "from-opf"

	def test_json_uuid_wins_over_opf(self, tmp_path: Path) -> None:
		"""The manifest value is authoritative; the OPF uuid never overrides it."""
		folder = tmp_path / "A" / "T (1)"
		folder.mkdir(parents=True)
		(folder / "metadata.json").write_text('{"uuid": "from-json", "title": "T"}', encoding="utf-8")
		(folder / "metadata.opf").write_text(_opf(uuid_ident="from-opf"), encoding="utf-8")
		assert read_book_folder(folder).uuid == "from-json"


# ---------------------------------------------------------------------------
# writers: ensure_uuid (lazy mint) + render/round-trip
# ---------------------------------------------------------------------------


class TestEnsureUuid:
	def _book(self, tmp_path: Path, *, manifest: dict | None = None, with_opf: bool = True) -> Path:
		folder = tmp_path / "A" / "T (1)"
		folder.mkdir(parents=True)
		(folder / "metadata.json").write_text(json.dumps(manifest or {"title": "T"}), encoding="utf-8")
		if with_opf:
			(folder / "metadata.opf").write_text(_opf(), encoding="utf-8")
		return folder

	def test_mints_and_persists_to_json(self, tmp_path: Path) -> None:
		folder = self._book(tmp_path)
		meta = read_book_folder(folder)
		assert meta.uuid is None
		u = ensure_uuid(meta)
		assert u is not None and meta.uuid == u
		# Persisted to the manifest.
		data = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
		assert data["uuid"] == u

	def test_idempotent_second_call_noop(self, tmp_path: Path) -> None:
		folder = self._book(tmp_path)
		meta = read_book_folder(folder)
		first = ensure_uuid(meta)
		# Re-read from disk (where it was persisted) and ensure again.
		meta2 = read_book_folder(folder)
		second = ensure_uuid(meta2)
		assert second == first  # same uuid, no remint

	def test_preserves_abs_specific_fields(self, tmp_path: Path) -> None:
		"""The lazy mint is a minimal inject: ABS fields bmf does not model
		(narrators, chapters, asin, explicit) must survive — _render_json would
		otherwise clobber them with defaults."""
		folder = self._book(tmp_path, manifest={
			"title": "T", "narrators": ["Petr"], "chapters": [{"s": 1}],
			"asin": "B123", "explicit": True,
		})
		meta = read_book_folder(folder)
		ensure_uuid(meta)
		data = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
		assert data["narrators"] == ["Petr"]
		assert data["chapters"] == [{"s": 1}]
		assert data["asin"] == "B123"
		assert data["explicit"] is True
		assert "uuid" in data

	def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
		folder = self._book(tmp_path)
		before = (folder / "metadata.json").read_text(encoding="utf-8")
		meta = read_book_folder(folder)
		u = ensure_uuid(meta, dry_run=True)
		assert u is not None and meta.uuid == u  # minted in-memory
		# But nothing persisted.
		assert (folder / "metadata.json").read_text(encoding="utf-8") == before

	def test_missing_folder_returns_none(self, tmp_path: Path) -> None:
		meta = BookMeta(path=str(tmp_path / "nope" / "x"), title="T")
		assert ensure_uuid(meta) is None

	def test_opf_gets_uuid_identifier(self, tmp_path: Path) -> None:
		"""The mint mirrors the uuid into the OPF too (Calibre/Kavita compat)."""
		folder = self._book(tmp_path)  # opf has no uuid identifier
		meta = read_book_folder(folder)
		u = ensure_uuid(meta)
		opf_text = (folder / "metadata.opf").read_text(encoding="utf-8")
		assert u in opf_text


class TestRenderAndRoundTrip:
	def test_render_json_includes_uuid(self) -> None:
		meta = BookMeta(uuid="u-xyz", title="T", authors=["A"])
		data = json.loads(_render_json(meta))
		assert data["uuid"] == "u-xyz"

	def test_uuid_survives_write_then_read(self, tmp_path: Path) -> None:
		"""Regression: uuid used to be regenerated on every OPF write. Now it is
		read back and preserved across a write cycle."""
		folder = tmp_path / "A" / "T (1)"
		folder.mkdir(parents=True)
		(folder / "metadata.json").write_text('{"uuid": "stable-1", "title": "T"}', encoding="utf-8")
		(folder / "metadata.opf").write_text(_opf(), encoding="utf-8")
		meta = read_book_folder(folder)
		write_book_meta(meta, dry_run=False, backup=False)
		# Re-read: the same uuid, not a fresh one.
		assert read_book_folder(folder).uuid == "stable-1"

	def test_write_preserves_abs_owned_fields(self, tmp_path: Path) -> None:
		"""Regression: adding an ISBN (or any bmf edit) must NOT wipe ABS-owned
		fields bmf does not model. The manifest is a surgical merge onto the
		existing file, not a full re-render from a fixed dict."""
		folder = tmp_path / "A" / "T (1)"
		folder.mkdir(parents=True)
		(folder / "metadata.json").write_text(json.dumps({
			"title": "T", "authors": ["A"], "isbn": None,
			"narrators": ["Petr"], "chapters": [{"start": 0}],
			"asin": "B123", "explicit": True, "abridged": False,
			"publishedDate": "2010-05-01",
		}), encoding="utf-8")
		(folder / "metadata.opf").write_text(_opf(), encoding="utf-8")
		meta = read_book_folder(folder)
		meta.isbn = "9780306406157"  # bmf adds an ISBN, nothing else
		write_book_meta(meta, dry_run=False, backup=False)
		data = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
		# The bmf edit landed:
		assert data["isbn"] == "9780306406157"
		# ...and every ABS-owned field bmf does not model survived untouched:
		assert data["narrators"] == ["Petr"]
		assert data["chapters"] == [{"start": 0}]
		assert data["asin"] == "B123"
		assert data["explicit"] is True
		assert data["abridged"] is False
		assert data["publishedDate"] == "2010-05-01"


# ---------------------------------------------------------------------------
# cache: uuid PK, path lookup, schema rebuild, repoint
# ---------------------------------------------------------------------------


class TestCacheUuidKeying:
	def test_put_then_get_by_path(self, tmp_path: Path) -> None:
		cache = Cache(tmp_path / "c.db")
		folder = tmp_path / "A" / "T (1)"
		folder.mkdir(parents=True)
		(folder / "metadata.json").write_text('{"uuid": "u1", "title": "T"}', encoding="utf-8")
		cache.put(read_book_folder(folder))
		cache.commit()
		got = cache.get(folder)
		assert got is not None and got.uuid == "u1"
		cache.close()

	def test_put_without_uuid_is_skipped(self, tmp_path: Path) -> None:
		"""Guard: a None uuid must never land in the unique path index."""
		cache = Cache(tmp_path / "c.db")
		meta = BookMeta(path=str(tmp_path / "A" / "T (1)"), title="T")  # uuid None
		cache.put(meta)
		cache.commit()
		assert cache.get(tmp_path / "A" / "T (1)") is None
		cache.close()

	def test_schema_rebuild_on_version_mismatch(self, tmp_path: Path) -> None:
		"""An old (path-PK) cache db is disposable: opening it with the new code
		must rebuild the books table to the uuid-PK shape, not crash."""
		db = tmp_path / "c.db"
		# Hand-build a v1-style cache: path PK, no uuid column, version=1.
		conn = sqlite3.connect(str(db))
		conn.executescript(
			"""
			CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
			INSERT INTO schema_meta VALUES ('version', '1');
			CREATE TABLE books (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, payload TEXT, scanned_at REAL);
			INSERT INTO books VALUES ('/old/path', 0.0, 0, '{}', 0.0);
			"""
		)
		conn.commit()
		conn.close()
		# Opening with the new code rebuilds.
		cache = Cache(db)
		n = cache.conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
		assert n == 0  # old rows dropped on rebuild
		# New schema: uuid PK present, path indexed.
		cols = {r[1] for r in cache.conn.execute("PRAGMA table_info(books)").fetchall()}
		assert "uuid" in cols and "path" in cols
		cache.close()

	def test_repoint_moves_row_to_destination(self, tmp_path: Path) -> None:
		cache = Cache(tmp_path / "c.db")
		src = tmp_path / "A" / "T (1)"
		dst = tmp_path / "needfix" / "A" / "T (1)"
		src.mkdir(parents=True)
		(src / "metadata.json").write_text('{"uuid": "u1", "title": "T"}', encoding="utf-8")
		cache.put(read_book_folder(src))
		cache.commit()
		# A stale row already at dst must not block the repoint (unique path).
		cache.conn.execute(
			"INSERT INTO books(uuid, path, mtime, size, payload, scanned_at) VALUES (?,?,?,?,?,?)",
			("stale", str(dst), 0.0, 0, "{}", 0.0),
		)
		cache.commit()

		assert cache.repoint(src, dst) is True
		# Source path no longer has a row; destination does (the row followed).
		assert cache.conn.execute("SELECT 1 FROM books WHERE path = ?", (str(src),)).fetchone() is None
		row = cache.conn.execute("SELECT uuid, payload FROM books WHERE path = ?", (str(dst),)).fetchone()
		assert row is not None and row[0] == "u1"  # stale row replaced, not kept
		# The payload's baked-in path was updated to the destination.
		assert json.loads(row[1])["path"] == str(dst)
		cache.close()

	def test_repoint_returns_false_when_no_source_row(self, tmp_path: Path) -> None:
		cache = Cache(tmp_path / "c.db")
		assert cache.repoint(tmp_path / "never", tmp_path / "seen") is False
		cache.close()

	def test_init_does_not_lock_out_second_connection(self, tmp_path: Path) -> None:
		"""Regression (hit on a fresh db): bmf_cache.db is shared by Cache AND the
		Enricher — two connections in one process, with no close in between (the
		real `bmf analyze` flow: ``cache = Cache(cfg.cache_db)`` then
		``Enricher(cache_db=cfg.cache_db)``). An earlier _init_schema left the
		schema-version write uncommitted, so the Enricher's CREATE TABLE on its
		own connection blocked until SQLITE_BUSY and raised 'database is locked'.
		Construction must commit, leaving the file writable to a peer connection."""
		db = tmp_path / "c.db"
		cache = Cache(db)  # fresh db -> rebuild branch; must NOT hold a write lock
		# A second connection (the Enricher) writes while cache is still open.
		c2 = sqlite3.connect(str(db), timeout=2.0)
		c2.execute("CREATE TABLE enrich_cache (k TEXT PRIMARY KEY)")  # raises if locked
		c2.commit()
		c2.close()
		cache.close()


# ---------------------------------------------------------------------------
# scan_library: lazy mint on cache miss
# ---------------------------------------------------------------------------


class TestScanLazyMint:
	def test_uuidless_book_gets_uuid_on_scan(self, tmp_path: Path) -> None:
		"""The first scan of a book lacking a uuid mints and persists one (the
		cache PK needs it), and caches the resulting BookMeta."""
		library = tmp_path / "lib"
		folder = library / "A" / "T (1)"
		folder.mkdir(parents=True)
		(folder / "metadata.json").write_text('{"title": "T"}', encoding="utf-8")  # no uuid
		cache = Cache(tmp_path / "c.db")
		books = scan_library(library, cache=cache)
		cache.close()
		assert len(books) == 1
		u = books[0].uuid
		assert u is not None
		# Persisted to the manifest.
		assert json.loads((folder / "metadata.json").read_text(encoding="utf-8"))["uuid"] == u
