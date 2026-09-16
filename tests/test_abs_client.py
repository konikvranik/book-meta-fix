"""Tests for abs_client — Audiobookshelf API client + `bmf abs-rescan` engine.

All HTTP is monkeypatched; no network calls are made.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from book_meta_fix import abs_client
from book_meta_fix.abs_client import (
	AbsItem,
	AudiobookshelfClient,
	broken_cover_items,
	changed_folders,
	match_items,
	parse_since_duration,
	stale_series_items,
)
from book_meta_fix.cli import _select_abs_library


class _Resp:
	"""Minimal requests.Response stand-in."""

	def __init__(self, status_code: int = 200, payload=None) -> None:
		self.status_code = status_code
		self._payload = payload

	def json(self):
		return self._payload


class _GetRecorder:
	"""Captures _http_get_json kwargs and serves a canned payload."""

	def __init__(self, payload) -> None:
		self.payload = payload
		self.calls: list[dict] = []

	def __call__(self, url, *, params=None, timeout=15.0, headers=None, session=None):
		self.calls.append({"url": url, "params": params, "timeout": timeout, "headers": headers, "session": session})
		return self.payload


class _PostRecorder:
	"""Captures _http_post kwargs; replies per-URL from a dict, else 200."""

	def __init__(self, replies: dict[str, _Resp] | None = None) -> None:
		self.replies = replies or {}
		self.calls: list[dict] = []

	def __call__(self, url, *, params=None, json_body=None, timeout=15.0, headers=None, session=None):
		self.calls.append({"url": url, "params": params, "json_body": json_body, "timeout": timeout, "headers": headers, "session": session})
		return self.replies.get(url, _Resp(200))

	@property
	def urls(self) -> list[str]:
		return [c["url"] for c in self.calls]


class _DeleteRecorder:
	"""Captures _http_delete kwargs; replies per-URL from a dict, else 200."""

	def __init__(self, replies: dict[str, _Resp] | None = None) -> None:
		self.replies = replies or {}
		self.calls: list[dict] = []

	def __call__(self, url, *, timeout=15.0, headers=None, session=None):
		self.calls.append({"url": url, "timeout": timeout, "headers": headers, "session": session})
		return self.replies.get(url, _Resp(200))

	@property
	def urls(self) -> list[str]:
		return [c["url"] for c in self.calls]


class _PatchRecorder:
	"""Captures _http_patch kwargs; replies per-URL from a dict, else 200."""

	def __init__(self, replies: dict[str, _Resp] | None = None) -> None:
		self.replies = replies or {}
		self.calls: list[dict] = []

	def __call__(self, url, *, json_body=None, timeout=15.0, headers=None, session=None):
		self.calls.append({"url": url, "json_body": json_body, "timeout": timeout, "headers": headers, "session": session})
		return self.replies.get(url, _Resp(200))

	@property
	def urls(self) -> list[str]:
		return [c["url"] for c in self.calls]


# ---------------------------------------------------------------------------
# parse_since_duration
# ---------------------------------------------------------------------------


class TestParseSinceDuration:
	def test_units(self) -> None:
		assert parse_since_duration("45s") == 45.0
		assert parse_since_duration("90m") == 5400.0
		assert parse_since_duration("2h") == 7200.0
		assert parse_since_duration("3d") == 3 * 86400.0

	def test_plain_number_is_seconds(self) -> None:
		assert parse_since_duration("300") == 300.0

	def test_whitespace_and_case_tolerated(self) -> None:
		assert parse_since_duration(" 2H ") == 7200.0

	@pytest.mark.parametrize("bad", ["", "abc", "2x", "-1h", "m"])
	def test_invalid_raises(self, bad: str) -> None:
		with pytest.raises(ValueError):
			parse_since_duration(bad)


# ---------------------------------------------------------------------------
# changed_folders
# ---------------------------------------------------------------------------


def _make_book(root: Path, name: str, *, age_sec: float | None = None) -> Path:
	folder = root / name
	folder.mkdir(parents=True)
	(folder / "metadata.opf").write_text("<package/>")
	if age_sec is not None:
		old = time.time() - age_sec
		os.utime(folder / "metadata.opf", (old, old))
	return folder


class TestChangedFolders:
	def test_only_recent_folders_returned(self, tmp_path: Path) -> None:
		recent = _make_book(tmp_path, "Autor/Nova kniha")
		_make_book(tmp_path, "Autor/Stara kniha", age_sec=7 * 86400)
		since = time.time() - 24 * 3600
		assert changed_folders(tmp_path, since) == [recent]

	def test_cover_change_counts_without_metadata_rewrite(self, tmp_path: Path) -> None:
		book = _make_book(tmp_path, "Autor/Kniha", age_sec=7 * 86400)
		(book / "cover.jpg").write_bytes(b"\xff\xd8fake")
		since = time.time() - 3600
		# The cover is the newest file in the folder -> the book counts as changed.
		assert changed_folders(tmp_path, since) == [book]

	def test_empty_library(self, tmp_path: Path) -> None:
		assert changed_folders(tmp_path, time.time() - 60) == []

	def test_progress_callback_counts_every_folder(self, tmp_path: Path) -> None:
		# The walk feeds the CLI bar one count per folder checked (the total
		# is unknown ahead — the walk is lazy), so the counts must arrive
		# strictly increasing and end at the folder count.
		_make_book(tmp_path, "Autor/Nova kniha")
		_make_book(tmp_path, "Autor/Stara kniha", age_sec=7 * 86400)
		_make_book(tmp_path, "Jiny/Blika", age_sec=7 * 86400)
		seen: list[int] = []
		assert changed_folders(tmp_path, time.time() - 24 * 3600, progress_callback=seen.append) == [tmp_path / "Autor/Nova kniha"]
		assert seen == [1, 2, 3]


# ---------------------------------------------------------------------------
# match_items
# ---------------------------------------------------------------------------


class TestMatchItems:
	def test_exact_absolute_path(self, tmp_path: Path) -> None:
		book = _make_book(tmp_path, "Autor/Kniha")
		items = [AbsItem(id="i1", path=str(book), rel_path="Autor/Kniha")]
		res = match_items([book], items, tmp_path)
		assert res.unmatched == []
		assert res.matched[0][1].id == "i1"

	def test_relpath_match_different_mount_prefix(self, tmp_path: Path) -> None:
		# bmf sees /tmp/.../lib/Autor/Kniha, the ABS server mounts the same
		# storage as /data/books/Autor/Kniha — relPath is the common ground.
		book = _make_book(tmp_path, "Autor/Kniha")
		items = [AbsItem(id="i2", path="/data/books/Autor/Kniha", rel_path="Autor/Kniha")]
		res = match_items([book], items, tmp_path)
		assert res.matched[0][1].id == "i2"

	def test_folder_name_match_covers_moved_book(self, tmp_path: Path) -> None:
		book = _make_book(tmp_path, "Novy Autor/Kniha (42)")
		items = [AbsItem(id="i3", path="/data/books/Stary Autor/Kniha (42)", rel_path="Stary Autor/Kniha (42)")]
		res = match_items([book], items, tmp_path)
		assert res.matched[0][1].id == "i3"

	def test_ambiguous_folder_name_left_unmatched(self, tmp_path: Path) -> None:
		book = _make_book(tmp_path, "Autor/Kniha")
		items = [
			AbsItem(id="a", path="/data/X/Kniha", rel_path="X/Kniha"),
			AbsItem(id="b", path="/data/Y/Kniha", rel_path="Y/Kniha"),
		]
		res = match_items([book], items, tmp_path)
		assert res.matched == []
		assert res.unmatched == [book]

	def test_no_candidate_unmatched(self, tmp_path: Path) -> None:
		book = _make_book(tmp_path, "Autor/Kniha")
		res = match_items([book], [AbsItem(id="z", path="/data/Jina", rel_path="Jina")], tmp_path)
		assert res.unmatched == [book]

	def test_item_ids_deduplicated(self, tmp_path: Path) -> None:
		# Two bmf folders with the same NAME mapping to one ABS item -> one id.
		b1 = _make_book(tmp_path, "A/Kniha (1)")
		b2 = _make_book(tmp_path, "B/Kniha (1)")
		item = AbsItem(id="same", path="/data/A/Kniha (1)", rel_path="A/Kniha (1)")
		res = match_items([b1, b2], [item], tmp_path)
		assert len(res.matched) == 2
		assert res.item_ids == ["same"]


# ---------------------------------------------------------------------------
# AudiobookshelfClient
# ---------------------------------------------------------------------------


class TestClient:
	def _client(self) -> AudiobookshelfClient:
		return AudiobookshelfClient("http://abs.lan:13378/", "s3cret")

	def test_base_url_trailing_slash_and_auth_header(self, monkeypatch) -> None:  # noqa: ANN001
		rec = _GetRecorder({"libraries": []})
		monkeypatch.setattr(abs_client, "_http_get_json", rec)
		self._client().libraries()
		assert rec.calls[0]["url"] == "http://abs.lan:13378/api/libraries"
		assert rec.calls[0]["headers"]["Authorization"] == "Bearer s3cret"
		assert rec.calls[0]["headers"]["User-Agent"]

	def test_libraries_failure_returns_none(self, monkeypatch) -> None:  # noqa: ANN001
		monkeypatch.setattr(abs_client, "_http_get_json", _GetRecorder(None))
		assert self._client().libraries() is None

	def test_items_parses_rows(self, monkeypatch) -> None:  # noqa: ANN001
		payload = {
			"results": [
				{
					"id": "i1",
					"path": "/data/A/Kniha",
					"relPath": "A/Kniha",
					"media": {"metadata": {"title": "Kniha"}, "coverPath": "/data/A/Kniha/cover.jpg"},
				},
				{"id": "", "path": "/data/x"},  # no id -> skipped
				{"nonsense": True},  # not a dict row -> skipped
			]
		}
		rec = _GetRecorder(payload)
		monkeypatch.setattr(abs_client, "_http_get_json", rec)
		items = self._client().items("lib1")
		assert items is not None and len(items) == 1
		assert items[0] == AbsItem(
			id="i1", path="/data/A/Kniha", rel_path="A/Kniha", title="Kniha",
			cover_path="/data/A/Kniha/cover.jpg",
		)
		assert rec.calls[0]["params"] == {"limit": 0}

	def test_clear_item_cover_deletes_and_reports(self, monkeypatch) -> None:  # noqa: ANN001
		rec = _DeleteRecorder()
		monkeypatch.setattr(abs_client, "_http_delete", rec)
		assert self._client().clear_item_cover("item-7") is True
		assert rec.urls == ["http://abs.lan:13378/api/items/item-7/cover"]
		assert rec.calls[0]["headers"]["Authorization"] == "Bearer s3cret"

	def test_clear_item_cover_rejected_token_returns_false(self, monkeypatch) -> None:  # noqa: ANN001
		rec = _DeleteRecorder({"http://abs.lan:13378/api/items/x/cover": _Resp(403)})
		monkeypatch.setattr(abs_client, "_http_delete", rec)
		assert self._client().clear_item_cover("x") is False

	def test_scan_items_posts_per_item(self, monkeypatch) -> None:  # noqa: ANN001
		# Per-item on purpose: the batch endpoint was measured answering 200
		# while processing nothing (see scan_items docstring).
		rec = _PostRecorder()
		monkeypatch.setattr(abs_client, "_http_post", rec)
		monkeypatch.setattr(abs_client.time, "sleep", lambda s: None)
		assert self._client().scan_items(["a", "b"]) == 0
		assert rec.urls == [
			"http://abs.lan:13378/api/items/a/scan",
			"http://abs.lan:13378/api/items/b/scan",
		]

	def test_scan_items_reports_progress_and_failure_count(self, monkeypatch) -> None:  # noqa: ANN001
		rec = _PostRecorder({"http://abs.lan:13378/api/items/b/scan": _Resp(403)})
		monkeypatch.setattr(abs_client, "_http_post", rec)
		monkeypatch.setattr(abs_client.time, "sleep", lambda s: None)
		seen: list[tuple[int, int]] = []
		failed = self._client().scan_items(["a", "b", "c"], progress_callback=lambda d, t: seen.append((d, t)))
		assert failed == 1
		assert seen == [(1, 3), (2, 3), (3, 3)]

	def test_scan_items_threaded_delivers_all_and_monotonic_progress(self, monkeypatch) -> None:  # noqa: ANN001
		# workers > 1 fans the per-item POSTs over a thread pool; the callback
		# must still fire exactly once per item with strictly increasing
		# counts (it is invoked under the counter lock).
		rec = _PostRecorder({"http://abs.lan:13378/api/items/f/scan": _Resp(403)})
		monkeypatch.setattr(abs_client, "_http_post", rec)
		monkeypatch.setattr(abs_client.time, "sleep", lambda s: None)
		ids = list("abcdef")
		seen: list[int] = []
		failed = self._client().scan_items(ids, progress_callback=lambda d, t: seen.append(d), workers=3)
		assert failed == 1
		assert sorted(seen) == list(range(1, 7))
		assert seen == sorted(seen)  # monotone despite the thread pool
		assert set(rec.urls) == {f"http://abs.lan:13378/api/items/{i}/scan" for i in ids}

	def test_client_passes_shared_session_to_all_calls(self, monkeypatch) -> None:  # noqa: ANN001
		# One Session for the whole client (keep-alive reuse across the
		# per-item loop) instead of a fresh TLS handshake per call.
		get = _GetRecorder({"libraries": []})
		post = _PostRecorder()
		delete = _DeleteRecorder()
		monkeypatch.setattr(abs_client, "_http_get_json", get)
		monkeypatch.setattr(abs_client, "_http_post", post)
		monkeypatch.setattr(abs_client, "_http_delete", delete)
		client = self._client()
		client.libraries()
		client.scan_items(["a"])
		client.clear_item_cover("a")
		assert get.calls[0]["session"] is client._session
		assert post.calls[0]["session"] is client._session
		assert delete.calls[0]["session"] is client._session

	def test_scan_items_rejected_token_counts_failure(self, monkeypatch) -> None:  # noqa: ANN001
		rec = _PostRecorder({"http://abs.lan:13378/api/items/a/scan": _Resp(403)})
		monkeypatch.setattr(abs_client, "_http_post", rec)
		monkeypatch.setattr(abs_client.time, "sleep", lambda s: None)
		assert self._client().scan_items(["a"]) == 1

	def test_scan_items_empty_list_is_noop_zero(self, monkeypatch) -> None:  # noqa: ANN001
		rec = _PostRecorder()
		monkeypatch.setattr(abs_client, "_http_post", rec)
		assert self._client().scan_items([]) == 0
		assert rec.calls == []

	def test_scan_library_force_param(self, monkeypatch) -> None:  # noqa: ANN001
		rec = _PostRecorder()
		monkeypatch.setattr(abs_client, "_http_post", rec)
		assert self._client().scan_library("lib1", force=True) is True
		assert rec.calls[0]["url"] == "http://abs.lan:13378/api/libraries/lib1/scan"
		assert rec.calls[0]["params"] == {"force": 1}

	def test_item_series_map_paginates_with_positive_limit(self, monkeypatch) -> None:  # noqa: ANN001
		# The series endpoint reads limit=0 as "zero per page" (measured on
		# ABS 2.36.0 — unlike /items where 0 = no limit), so pages must be
		# fetched with limit>0 and stop once page*limit covers total.
		pages = {
			0: {"results": [{"id": "s1", "name": "Zaklínač", "books": [{"id": "i1"}, {"id": "i2"}]}], "total": 501},
			1: {"results": [{"id": "s2", "name": "Perry Rhodan", "books": [{"id": "i3"}]}], "total": 501},
		}

		def _serve(url, *, params=None, timeout=15.0, headers=None, session=None):  # noqa: ANN001, ARG001
			return pages[params["page"]]

		monkeypatch.setattr(abs_client, "_http_get_json", _serve)
		m = self._client().item_series_map("lib1")
		assert m == {"i1": ["Zaklínač"], "i2": ["Zaklínač"], "i3": ["Perry Rhodan"]}

	def test_item_series_map_failure_returns_none(self, monkeypatch) -> None:  # noqa: ANN001
		monkeypatch.setattr(abs_client, "_http_get_json", _GetRecorder(None))
		assert self._client().item_series_map("lib1") is None

	def test_item_series_map_empty_library(self, monkeypatch) -> None:  # noqa: ANN001
		rec = _GetRecorder({"results": [], "total": 0})
		monkeypatch.setattr(abs_client, "_http_get_json", rec)
		assert self._client().item_series_map("lib1") == {}
		assert len(rec.calls) == 1

	def test_clear_item_series_payload_shape(self, monkeypatch) -> None:  # noqa: ANN001
		# The series list MUST sit at metadata.series — a top-level series
		# key is silently ignored by ABS (measured: HTTP 200, no change).
		rec = _PatchRecorder()
		monkeypatch.setattr(abs_client, "_http_patch", rec)
		assert self._client().clear_item_series("item-7") is True
		assert rec.urls == ["http://abs.lan:13378/api/items/item-7/media"]
		assert rec.calls[0]["json_body"] == {"metadata": {"series": []}}
		assert rec.calls[0]["headers"]["Authorization"] == "Bearer s3cret"

	def test_clear_item_series_rejected_token_returns_false(self, monkeypatch) -> None:  # noqa: ANN001
		rec = _PatchRecorder({"http://abs.lan:13378/api/items/x/media": _Resp(403)})
		monkeypatch.setattr(abs_client, "_http_patch", rec)
		assert self._client().clear_item_series("x") is False


# ---------------------------------------------------------------------------
# _select_abs_library
# ---------------------------------------------------------------------------


def _lib(id: str, name: str, folders: list[str]) -> dict:
	return {"id": id, "name": name, "folders": [{"fullPath": f} for f in folders]}


class TestSelectAbsLibrary:
	def test_explicit_name_or_id_wins(self, tmp_path: Path) -> None:
		libs = [_lib("l1", "Books", ["/data/x"]), _lib("l2", "Ebooks", ["/data/y"])]
		assert _select_abs_library(libs, wanted="ebooks", library_root=tmp_path) == libs[1]
		assert _select_abs_library(libs, wanted="l1", library_root=tmp_path) == libs[0]

	def test_unknown_name_returns_none(self, tmp_path: Path) -> None:
		assert _select_abs_library([_lib("l1", "Books", [])], wanted="nope", library_root=tmp_path) is None

	def test_single_book_library_autoselected(self, tmp_path: Path) -> None:
		libs = [_lib("l1", "Books", ["/data/x"])]
		assert _select_abs_library(libs, wanted="", library_root=tmp_path) == libs[0]

	def test_path_tail_match(self, tmp_path: Path) -> None:
		libs = [_lib("l1", "Books", ["/mnt/nfs/books"]), _lib("l2", "Podcasts", ["/mnt/nfs/podcasts"])]
		# Our root /home/user/books shares the 'books' tail with l1 only.
		root = Path("/home/user/books")
		assert _select_abs_library(libs, wanted="", library_root=root) == libs[0]

	def test_ambiguous_tail_returns_none(self, tmp_path: Path) -> None:
		libs = [_lib("l1", "A", ["/x/books"]), _lib("l2", "B", ["/y/books"])]
		assert _select_abs_library(libs, wanted="", library_root=Path("/z/books")) is None


# ---------------------------------------------------------------------------
# broken_cover_items
# ---------------------------------------------------------------------------


class TestBrokenCoverItems:
	"""The --fix-covers audit: stored coverPath rows that cannot be a cover."""

	def _item(self, id: str, cover: str) -> AbsItem:  # noqa: A002
		return AbsItem(id=id, path=f"/data/books/{id}", rel_path=id, title=f"Kniha {id}", cover_path=cover)

	def test_non_image_extension_is_broken(self, tmp_path: Path) -> None:
		# The measured ABS failure: ffmpeg fed metadata.json / cover.html.
		items = [self._item("a", "/data/books/a/metadata.json"), self._item("b", "/data/books/b/cover.html")]
		broken = broken_cover_items(items, tmp_path, ["/data/books"])
		assert [(b.item.id, b.reason) for b in broken] == [("a", "ext"), ("b", "ext")]

	def test_valid_existing_cover_not_flagged(self, tmp_path: Path) -> None:
		(tmp_path / "Autor/Kniha (1)").mkdir(parents=True)
		# A REAL decodable image — the audit decodes stored rows since the
		# 0-byte-cover finding (fake magic bytes are now "unreadable", below).
		from PIL import Image

		Image.new("RGB", (40, 60), color=(10, 120, 200)).save(tmp_path / "Autor/Kniha (1)/cover.jpg")
		items = [self._item("a", "/data/books/Autor/Kniha (1)/cover.jpg")]
		assert broken_cover_items(items, tmp_path, ["/data/books"]) == []

	def test_undecodable_existing_cover_is_broken(self, tmp_path: Path) -> None:
		# Exists, image-named, but no decoder reads it — a 0-byte leftover or
		# fake magic bytes feed ffmpeg the same "Invalid data found" as the
		# extension/missing shapes (746 zero-byte rows measured in the wild).
		(tmp_path / "Autor/Kniha (1)").mkdir(parents=True)
		for name, blob in (("cover.jpg", b""), ("fake.jpg", b"\xff\xd8jpg")):
			(tmp_path / "Autor/Kniha (1)").joinpath(name).write_bytes(blob)
		items = [
			self._item("a", "/data/books/Autor/Kniha (1)/cover.jpg"),
			self._item("b", "/data/books/Autor/Kniha (1)/fake.jpg"),
		]
		broken = broken_cover_items(items, tmp_path, ["/data/books"])
		assert [(b.item.id, b.reason) for b in broken] == [("a", "unreadable"), ("b", "unreadable")]

	def test_missing_cover_file_is_broken(self, tmp_path: Path) -> None:
		# Image extension but nothing at the mapped path — e.g. the file was
		# renamed to .bak by strip-covers while ABS still stores the old row.
		items = [self._item("a", "/data/books/Autor/Ztracena/cover.jpg")]
		broken = broken_cover_items(items, tmp_path, ["/data/books"])
		assert [(b.item.id, b.reason) for b in broken] == [("a", "missing")]

	def test_cover_outside_library_folders_only_ext_checked(self, tmp_path: Path) -> None:
		# ABS-uploaded covers live under its own /metadata dir — not on our
		# mount, so existence cannot be judged and must not be attempted.
		items = [self._item("a", "/metadata/items/item-a/cover.jpg")]
		assert broken_cover_items(items, tmp_path, ["/data/books"]) == []

	def test_empty_or_bare_cover_paths_skipped(self, tmp_path: Path) -> None:
		items = [self._item("a", ""), self._item("b", "cover.jpg")]
		assert broken_cover_items(items, tmp_path, ["/data/books"]) == []

	def test_no_folder_prefix_means_ext_check_only(self, tmp_path: Path) -> None:
		# Library dict without folders — degrade to the extension check.
		items = [self._item("a", "/somewhere/else/x/metadata.json")]
		broken = broken_cover_items(items, tmp_path, [])
		assert [(b.item.id, b.reason) for b in broken] == [("a", "ext")]

	def test_progress_callback_reports_done_total(self, tmp_path: Path) -> None:
		# Same (done, total) contract as scan_items — the audit stats one
		# file per item over NFS, so the CLI renders it as a determinate bar.
		items = [
			self._item("a", "/data/books/a/metadata.json"),
			self._item("b", "/data/books/Autor/Kniha (1)/cover.jpg"),
			self._item("c", ""),
		]
		(tmp_path / "Autor/Kniha (1)").mkdir(parents=True)
		from test_covers import _real_cover

		_real_cover(tmp_path / "Autor/Kniha (1)/cover.jpg")
		seen: list[tuple[int, int]] = []
		broken = broken_cover_items(items, tmp_path, ["/data/books"], progress_callback=lambda d, t: seen.append((d, t)))
		assert [b.item.id for b in broken] == ["a"]
		assert seen == [(1, 3), (2, 3), (3, 3)]

	def test_outside_library_calibre_marker_is_broken(self, tmp_path: Path) -> None:
		# A coverPath under ABS's own /metadata cache cannot be judged from
		# the mount — but the bytes fetched through the API carry calibre's
		# "Generated cover" marker (the cached screenshot of a placeholder
		# the disk cleanup already bakked), and that is decisive.
		from test_covers import _calibre_marked_jpeg_bytes

		marked = _calibre_marked_jpeg_bytes(size=(60, 90))
		fetched: list[str] = []
		items = [self._item("a", "/metadata/items/item-a/cover.jpg")]
		broken = broken_cover_items(
			items, tmp_path, ["/data/books"],
			fetch_cover=lambda iid: (fetched.append(iid) or marked),
		)
		assert fetched == ["a"]
		assert [(b.item.id, b.reason) for b in broken] == [("a", "generated")]

	def test_outside_library_clean_bytes_stay(self, tmp_path: Path) -> None:
		# Marker-only on purpose: an uploaded real cover (no marker) is never
		# cleared even though we can fetch its bytes.
		from test_covers import _gradient_jpeg_bytes

		clean = _gradient_jpeg_bytes()
		items = [self._item("a", "/metadata/items/item-a/cover.jpg")]
		assert broken_cover_items(
			items, tmp_path, ["/data/books"], fetch_cover=lambda iid: clean,
		) == []

	def test_outside_library_fetch_failure_skips(self, tmp_path: Path) -> None:
		# A fetch error is "no verdict", never "clear" — rows outside the
		# mount must not be cleared on a guess.
		items = [self._item("a", "/metadata/items/item-a/cover.jpg")]
		assert broken_cover_items(
			items, tmp_path, ["/data/books"], fetch_cover=lambda iid: None,
		) == []

	def test_outside_library_row_untouched_without_fetcher(self, tmp_path: Path) -> None:
		# Without a fetcher (old callers/tests) the audit degrades to the
		# historical extension-only behaviour for outside-library rows.
		items = [self._item("a", "/metadata/items/item-a/cover.jpg")]
		assert broken_cover_items(items, tmp_path, ["/data/books"]) == []

	def test_marker_never_fetched_for_library_rows(self, tmp_path: Path) -> None:
		# Rows mapping INTO the library are judged from disk (missing/
		# unreadable) — the byte fetch is the outside-library tool only.
		(tmp_path / "Autor/Kniha (1)").mkdir(parents=True)
		from test_covers import _real_cover

		_real_cover(tmp_path / "Autor/Kniha (1)/cover.jpg")
		calls: list[str] = []
		items = [self._item("a", "/data/books/Autor/Kniha (1)/cover.jpg")]
		assert broken_cover_items(
			items, tmp_path, ["/data/books"], fetch_cover=lambda iid: calls.append(iid),
		) == []
		assert calls == []


class TestGetItemCover:
	"""AudiobookshelfClient.get_item_cover — the audit's byte fetch."""

	def test_returns_body_bytes(self, monkeypatch) -> None:
		def _fake(url, *, timeout=15.0, headers=None, session=None):
			# The seam's contract: parsed body bytes on 200, None on failure
			# (the _http_get_bytes body, not the response object).
			return b"\xff\xd8jpeg-bytes"

		monkeypatch.setattr(abs_client, "_http_get_bytes", _fake)
		client = AudiobookshelfClient("http://abs.lan:13378", "s3cret")
		assert client.get_item_cover("abc") == b"\xff\xd8jpeg-bytes"

	def test_none_on_http_failure(self, monkeypatch) -> None:
		def _fake(url, *, timeout=15.0, headers=None, session=None):
			return None

		monkeypatch.setattr(abs_client, "_http_get_bytes", _fake)
		client = AudiobookshelfClient("http://abs.lan:13378", "s3cret")
		assert client.get_item_cover("abc") is None


# ---------------------------------------------------------------------------
# stale_series_items
# ---------------------------------------------------------------------------


def _manifest(folder: Path, series) -> Path:
	"""Write a metadata.json with the given series value (... = key absent)."""
	data = {"title": folder.name}
	if series is not ...:
		data["series"] = series
	(folder / "metadata.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
	return folder


class TestStaleSeriesItems:
	"""The series-deletion sweep: ABS rows holding series the disk dropped."""

	def _matched(self, root: Path, name: str, iid: str) -> tuple[Path, AbsItem]:
		folder = root / name
		folder.mkdir(parents=True)
		return folder, AbsItem(id=iid, path=f"/data/books/{name}", rel_path=name, title=name)

	def test_disk_dropped_series_abs_holds_it(self, tmp_path: Path) -> None:
		# The measured ABS gap: apply wrote series: [], the per-item rescan
		# ran, the junk series stayed in the ABS database.
		folder, item = self._matched(tmp_path, "Autor/Kniha (1)", "i1")
		_manifest(folder, [])
		stale = stale_series_items([(folder, item)], {"i1": ["Neznámý"]})
		assert [(s.item.id, s.series_names) for s in stale] == [("i1", ("Neznámý",))]

	def test_series_key_absent_counts_as_dropped(self, tmp_path: Path) -> None:
		folder, item = self._matched(tmp_path, "Autor/Kniha (1)", "i1")
		_manifest(folder, ...)
		assert len(stale_series_items([(folder, item)], {"i1": ["Autor"]})) == 1

	def test_disk_still_has_series_not_flagged(self, tmp_path: Path) -> None:
		# A non-empty difference (rename, sequence) is the SCAN's own job —
		# the sweep must not PATCH it.
		folder, item = self._matched(tmp_path, "Autor/Kniha (1)", "i1")
		_manifest(folder, ["Zaklínač #2"])
		assert stale_series_items([(folder, item)], {"i1": ["Povídky"]}) == []

	def test_dict_and_bare_string_series_shapes(self, tmp_path: Path) -> None:
		folder1, item1 = self._matched(tmp_path, "A/Kniha (1)", "i1")
		_manifest(folder1, [{"name": "Zaklínač", "index": 2}])
		folder2, item2 = self._matched(tmp_path, "A/Kniha (2)", "i2")
		_manifest(folder2, "Zaklínač #2")
		assert stale_series_items([(folder1, item1), (folder2, item2)], {"i1": ["Zaklínač"], "i2": ["Zaklínač"]}) == []

	def test_whitespaced_entries_count_as_empty(self, tmp_path: Path) -> None:
		folder, item = self._matched(tmp_path, "Autor/Kniha (1)", "i1")
		_manifest(folder, [{"name": "  ", "index": 1}])
		assert len(stale_series_items([(folder, item)], {"i1": ["Autor"]})) == 1

	def test_unreadable_manifest_never_cleared(self, tmp_path: Path) -> None:
		# No disk truth -> no verdict; clearing on a guess would be data loss.
		folder, item = self._matched(tmp_path, "Autor/Kniha (1)", "i1")
		(folder / "metadata.json").write_text("{not json", encoding="utf-8")
		assert stale_series_items([(folder, item)], {"i1": ["Autor"]}) == []

	def test_item_without_abs_series_skipped(self, tmp_path: Path) -> None:
		folder, item = self._matched(tmp_path, "Autor/Kniha (1)", "i1")
		_manifest(folder, [])
		assert stale_series_items([(folder, item)], {}) == []

	def test_progress_callback_reports_done_total(self, tmp_path: Path) -> None:
		seen: list[tuple[int, int]] = []
		folder, item = self._matched(tmp_path, "Autor/Kniha (1)", "i1")
		_manifest(folder, [])
		stale = stale_series_items([(folder, item)], {"i1": ["X"]}, progress_callback=lambda d, t: seen.append((d, t)))
		assert len(stale) == 1
		assert seen == [(1, 1)]
