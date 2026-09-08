"""Tests for CLI commands: completion installer, shims, strip-covers, abs-rescan."""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
from test_abs_client import _DeleteRecorder, _GetRecorder, _PostRecorder, _Resp
from test_covers import (
	_HTML_BYTES,
	_gradient_cover,
	_gradient_jpeg_bytes,
	_make_epub,
	_solid_cover,
	_solid_jpeg_bytes,
)

from book_meta_fix import abs_client
from book_meta_fix.cli import main
from book_meta_fix.covers import epub_cover_image


class TestInstallCompletion:
	"""bmf install-completion <shell> emits a usable shell completion script."""

	def test_bash_outputs_completion_function(self) -> None:
		result = CliRunner().invoke(main, ["install-completion", "bash"])
		assert result.exit_code == 0
		# The bash installer defines a completion function and binds it to 'bmf'.
		assert "_bmf_completion()" in result.output
		assert "complete " in result.output
		assert "bmf" in result.output
		# The script must reference Click's magic env var so the runtime
		# completion query fires at tab-time.
		assert "_BMF_COMPLETE=bash_complete" in result.output

	def test_zsh_outputs_compdef(self) -> None:
		result = CliRunner().invoke(main, ["install-completion", "zsh"])
		assert result.exit_code == 0
		assert "#compdef bmf" in result.output
		assert "_BMF_COMPLETE=zsh_complete" in result.output

	def test_fish_outputs_function(self) -> None:
		result = CliRunner().invoke(main, ["install-completion", "fish"])
		assert result.exit_code == 0
		assert "function _bmf_completion" in result.output
		assert "_BMF_COMPLETE=fish_complete" in result.output

	def test_output_flag_writes_file(self, tmp_path) -> None:  # noqa: ANN001
		out = tmp_path / "bmf.sh"
		result = CliRunner().invoke(main, ["install-completion", "bash", "-o", str(out)])
		assert result.exit_code == 0
		assert out.is_file()
		assert "_bmf_completion()" in out.read_text()
		# Confirmation message goes to stdout (not the script body).
		assert "written to" in result.output

	def test_invalid_shell_rejected(self) -> None:
		result = CliRunner().invoke(main, ["install-completion", "powershell"])
		assert result.exit_code != 0


class TestOrganizeShim:
	"""`bmf organize` was merged into apply — the command is now a signpost."""

	def test_prints_migration_message(self) -> None:
		result = CliRunner().invoke(main, ["organize"])
		assert result.exit_code == 0
		assert "merged into `bmf apply`" in result.output
		assert "bmf analyze" in result.output


class TestStripCovers:
	"""`bmf strip-covers` removes generated covers (sidecar + embedded EPUB)."""

	_MINI_OPF = (
		'<?xml version="1.0" encoding="utf-8"?>'
		'<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="BookId">'
		'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
		"<dc:title>Kniha</dc:title><dc:creator>Autor</dc:creator>"
		'<dc:identifier id="BookId">x</dc:identifier><dc:language>ces</dc:language>'
		"</metadata></package>"
	)

	def _make_library(self, root: Path) -> tuple[Path, Path]:
		"""Two books: one with generated covers everywhere, one clean."""
		bad = root / "Jan Autor - Kniha"
		bad.mkdir(parents=True)
		(bad / "metadata.opf").write_text(self._MINI_OPF)
		_solid_cover(bad / "cover.jpg")
		_make_epub(bad / "b.epub", cover_bytes=_solid_jpeg_bytes())
		good = root / "Jana Autorka - Kniha"
		good.mkdir(parents=True)
		(good / "metadata.opf").write_text(self._MINI_OPF)
		_gradient_cover(good / "cover.jpg")
		_make_epub(good / "g.epub", cover_bytes=_gradient_jpeg_bytes())
		return bad, good

	def test_dry_run_reports_and_touches_nothing(self, tmp_path: Path) -> None:
		bad, good = self._make_library(tmp_path)
		result = CliRunner().invoke(main, ["strip-covers", "--library", str(tmp_path), "--no-cache"])
		assert result.exit_code == 0
		assert "DRY-RUN" in result.output
		assert "Cover strip summary" in result.output
		assert (bad / "cover.jpg").is_file()
		assert not (bad / "cover.jpg.bak").exists()
		assert epub_cover_image(bad / "b.epub") is not None
		assert (good / "cover.jpg").is_file()
		assert epub_cover_image(good / "g.epub") is not None

	def test_apply_removes_generated_only(self, tmp_path: Path) -> None:
		bad, good = self._make_library(tmp_path)
		result = CliRunner().invoke(main, ["strip-covers", "--library", str(tmp_path), "--no-cache", "--apply"])
		assert result.exit_code == 0
		assert "WRITE" in result.output
		assert not (bad / "cover.jpg").exists()
		assert (bad / "cover.jpg.bak").is_file()
		assert epub_cover_image(bad / "b.epub") is None
		# the clean book is untouched
		assert (good / "cover.jpg").is_file()
		assert epub_cover_image(good / "g.epub") is not None

	def _add_invalid_book(self, root: Path) -> Path:
		"""A book whose cover files are not images at all (the ABS ffmpeg case)."""
		ugly = root / "Jan Nevalidni - Kniha"
		ugly.mkdir(parents=True)
		(ugly / "metadata.opf").write_text(self._MINI_OPF)
		(ugly / "cover.html").write_bytes(_HTML_BYTES)
		_make_epub(ugly / "i.epub", cover_bytes=_HTML_BYTES)
		return ugly

	def test_invalid_selector_renames_invalid_and_strips_epub(self, tmp_path: Path) -> None:
		bad, good = self._make_library(tmp_path)
		ugly = self._add_invalid_book(tmp_path)
		result = CliRunner().invoke(main, [
			"strip-covers", "--library", str(tmp_path), "--no-cache", "--apply", "--invalid",
		])
		assert result.exit_code == 0
		assert not (ugly / "cover.html").exists()
		assert (ugly / "cover.html.bak").is_file()
		assert epub_cover_image(ugly / "i.epub") is None
		# generated covers stay out of the --invalid run
		assert (bad / "cover.jpg").is_file()
		assert epub_cover_image(bad / "b.epub") is not None
		assert result.output.count("invalid cover files renamed to .bak") == 1

	def test_invalid_external_scope_leaves_embedded(self, tmp_path: Path) -> None:
		ugly = self._add_invalid_book(tmp_path)
		result = CliRunner().invoke(main, [
			"strip-covers", "--library", str(tmp_path), "--no-cache", "--apply", "--invalid", "external",
		])
		assert result.exit_code == 0
		assert (ugly / "cover.html.bak").is_file()
		assert epub_cover_image(ugly / "i.epub") is not None

	def test_generated_scope_value_leaves_sidecar(self, tmp_path: Path) -> None:
		bad, _good = self._make_library(tmp_path)
		result = CliRunner().invoke(main, [
			"strip-covers", "--library", str(tmp_path), "--no-cache", "--apply", "--generated", "embedded",
		])
		assert result.exit_code == 0
		assert (bad / "cover.jpg").is_file()
		assert epub_cover_image(bad / "b.epub") is None


class TestAbsRescan:
	"""`bmf abs-rescan` maps changed folders to ABS items and triggers rescans.

	All HTTP is monkeypatched at the abs_client module level (no network).
	"""

	_MINI_OPF = TestStripCovers._MINI_OPF
	_ENV = {"BMF_ABS_URL": "http://abs.lan:13378", "BMF_ABS_TOKEN": "s3cret", "BMF_ABS_LIBRARY": ""}

	def _make_library(self, root: Path) -> tuple[Path, Path]:
		a = root / "Autor A/Kniha (1)"
		a.mkdir(parents=True)
		(a / "metadata.opf").write_text(self._MINI_OPF)
		b = root / "Autor B/Serie 2 - Druha (2)"
		b.mkdir(parents=True)
		(b / "metadata.opf").write_text(self._MINI_OPF)
		return a, b

	def _fake_abs(self, monkeypatch, books: list[Path], covers: dict[int, str] | None = None, post: _PostRecorder | None = None) -> _PostRecorder:  # noqa: ANN001
		"""Serve libraries + items covering exactly *books*; record every POST.

		*covers* maps an item index to its stored media.coverPath row (the
		value --fix-covers audits); *post* overrides the POST recorder (to
		make selected item scans fail).
		"""
		items = []
		for i, book in enumerate(books):
			media: dict = {"metadata": {"title": book.name}}
			cover = (covers or {}).get(i, "")
			if cover:
				media["coverPath"] = cover
			items.append({
				"id": f"item-{i}",
				"path": str(book),
				"relPath": f"{book.parent.name}/{book.name}",
				"media": media,
			})

		def _get(url, *, params=None, timeout=15.0, headers=None, session=None):  # noqa: ANN001, ARG001
			if url.endswith("/api/libraries"):
				return {
					"libraries": [
						{"id": "lib1", "name": "Books", "mediaType": "book", "folders": [{"fullPath": "/data/books"}]}
					]
				}
			if "/items" in url:
				return {"results": items}
			return None

		monkeypatch.setattr(abs_client, "_http_get_json", _get)
		if post is None:
			post = _PostRecorder()
		monkeypatch.setattr(abs_client, "_http_post", post)
		return post

	def test_dry_run_reports_without_posting(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
		books = self._make_library(tmp_path)
		post = self._fake_abs(monkeypatch, list(books))
		result = CliRunner().invoke(main, ["abs-rescan", "--library", str(tmp_path)], env=self._ENV)
		assert result.exit_code == 0
		assert "DRY-RUN" in result.output
		assert "ABS rescan summary" in result.output
		assert post.calls == []

	def test_apply_posts_per_item_scans(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
		books = self._make_library(tmp_path)
		post = self._fake_abs(monkeypatch, list(books))
		monkeypatch.setattr(abs_client.time, "sleep", lambda s: None)
		result = CliRunner().invoke(main, ["abs-rescan", "--library", str(tmp_path), "--abs-workers", "1", "--apply"], env=self._ENV)
		assert result.exit_code == 0
		assert "WRITE" in result.output
		# The per-item loop drives a progress bar whose final state stays in
		# the output (N/N + the ETA column) — the run must not look hung.
		assert "Rescanning ABS items" in result.output
		assert "2/2" in result.output
		assert [c["url"] for c in post.calls] == [
			"http://abs.lan:13378/api/items/item-0/scan",
			"http://abs.lan:13378/api/items/item-1/scan",
		]

	def test_apply_partial_scan_failure_warns_with_counts(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
		books = self._make_library(tmp_path)
		post = _PostRecorder({"http://abs.lan:13378/api/items/item-1/scan": _Resp(403)})
		self._fake_abs(monkeypatch, list(books), post=post)
		monkeypatch.setattr(abs_client.time, "sleep", lambda s: None)
		result = CliRunner().invoke(main, ["abs-rescan", "--library", str(tmp_path), "--apply"], env=self._ENV)
		# Most items delivered, one rejected: a warning with counts, not a
		# hard failure (the majority of the rescan did land in ABS).
		assert result.exit_code == 0
		assert "Failed to request the rescan of 1 of 2 ABS items" in result.output

	def test_missing_config_exits_with_hint(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
		# chdir away from the repo so a developer's .env cannot satisfy the check
		monkeypatch.chdir(tmp_path)
		for var in ("BMF_ABS_URL", "BMF_ABS_TOKEN"):
			monkeypatch.delenv(var, raising=False)
		result = CliRunner().invoke(main, ["abs-rescan", "--library", str(tmp_path)])
		assert result.exit_code != 0
		assert "Audiobookshelf is not configured" in result.output
		assert "Traceback" not in result.output

	def test_unreachable_server_exits_with_hint(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
		self._make_library(tmp_path)
		monkeypatch.setattr(abs_client, "_http_get_json", _GetRecorder(None))
		monkeypatch.setattr(abs_client, "_http_post", _PostRecorder())
		result = CliRunner().invoke(main, ["abs-rescan", "--library", str(tmp_path)], env=self._ENV)
		assert result.exit_code != 0
		assert "Cannot reach Audiobookshelf" in result.output
		assert "Traceback" not in result.output

	def test_unmatched_books_reported_not_posted(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
		books = self._make_library(tmp_path)
		post = self._fake_abs(monkeypatch, books[:1])  # ABS knows only the first book
		monkeypatch.setattr(abs_client.time, "sleep", lambda s: None)
		result = CliRunner().invoke(main, ["abs-rescan", "--library", str(tmp_path), "--apply"], env=self._ENV)
		assert result.exit_code == 0
		assert "not found in ABS" in result.output
		# The unmatched book has no item id to scan; instead a PLAIN library
		# scan (no force param) is requested so ABS learns the new paths.
		assert [c["url"] for c in post.calls] == [
			"http://abs.lan:13378/api/items/item-0/scan",
			"http://abs.lan:13378/api/libraries/lib1/scan",
		]
		assert post.calls[-1]["params"] is None
		assert "Plain ABS library scan requested in the background" in result.output

	def test_unmatched_dry_run_promises_library_scan_without_posting(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
		books = self._make_library(tmp_path)
		post = self._fake_abs(monkeypatch, books[:1])
		result = CliRunner().invoke(main, ["abs-rescan", "--library", str(tmp_path)], env=self._ENV)
		assert result.exit_code == 0
		assert "Dry-run: would trigger a plain ABS library scan" in result.output
		assert post.calls == []

	def test_matched_only_apply_fires_no_library_scan(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
		books = self._make_library(tmp_path)
		post = self._fake_abs(monkeypatch, list(books))
		monkeypatch.setattr(abs_client.time, "sleep", lambda s: None)
		result = CliRunner().invoke(main, [
			"abs-rescan", "--library", str(tmp_path), "--abs-workers", "1", "--apply",
		], env=self._ENV)
		assert result.exit_code == 0
		assert not any("/api/libraries/lib1/scan" in c["url"] for c in post.calls)

	def test_library_scan_failure_warns_without_failing_run(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
		books = self._make_library(tmp_path)
		post = _PostRecorder({"http://abs.lan:13378/api/libraries/lib1/scan": _Resp(403)})
		self._fake_abs(monkeypatch, books[:1], post=post)
		monkeypatch.setattr(abs_client.time, "sleep", lambda s: None)
		result = CliRunner().invoke(main, ["abs-rescan", "--library", str(tmp_path), "--apply"], env=self._ENV)
		# The matched rescan was delivered; the follow-up library scan only warns.
		assert result.exit_code == 0
		assert "Failed to request the library scan" in result.output

	def test_nothing_changed_is_green_noop(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
		import os
		import time as _time

		self._make_library(tmp_path)
		old = _time.time() - 7 * 86400
		for opf in tmp_path.rglob("metadata.opf"):
			os.utime(opf, (old, old))
		post = self._fake_abs(monkeypatch, [])
		result = CliRunner().invoke(main, ["abs-rescan", "--library", str(tmp_path), "--since", "1h"], env=self._ENV)
		assert result.exit_code == 0
		assert "nothing to rescan" in result.output
		assert post.calls == []

	def test_invalid_since_rejected(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
		self._make_library(tmp_path)
		self._fake_abs(monkeypatch, [])
		result = CliRunner().invoke(main, ["abs-rescan", "--library", str(tmp_path), "--since", "2x"], env=self._ENV)
		assert result.exit_code != 0
		assert "Invalid --since value" in result.output
		assert "Traceback" not in result.output

	def test_force_all_dry_run_vs_apply(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
		self._make_library(tmp_path)
		post = self._fake_abs(monkeypatch, [])
		runner = CliRunner()
		dry = runner.invoke(main, ["abs-rescan", "--library", str(tmp_path), "--force-all"], env=self._ENV)
		assert dry.exit_code == 0
		assert "would force-rescan" in dry.output
		assert post.calls == []
		wet = runner.invoke(main, ["abs-rescan", "--library", str(tmp_path), "--force-all", "--apply"], env=self._ENV)
		assert wet.exit_code == 0
		assert [c["url"] for c in post.calls] == ["http://abs.lan:13378/api/libraries/lib1/scan"]
		assert post.calls[0]["params"] == {"force": 1}

	def _fake_delete(self, monkeypatch) -> _DeleteRecorder:  # noqa: ANN001
		rec = _DeleteRecorder()
		monkeypatch.setattr(abs_client, "_http_delete", rec)
		return rec

	@staticmethod
	def _abs_cover(book: Path, name: str) -> str:
		"""A stored coverPath the way ABS sees it (its own /data/books mount)."""
		return f"/data/books/{book.parent.name}/{book.name}/{name}"

	def _age_library(self, tmp_path: Path) -> None:
		import os
		import time as _time

		old = _time.time() - 7 * 86400
		for opf in tmp_path.rglob("metadata.opf"):
			os.utime(opf, (old, old))

	def test_fix_covers_dry_run_lists_without_calls(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
		books = self._make_library(tmp_path)
		self._age_library(tmp_path)
		# Item 0 stores metadata.json as its cover — the ffmpeg "Invalid
		# data found" row; item 1 has a real, existing cover.jpg.
		(books[1] / "cover.jpg").write_bytes(b"\xff\xd8jpg")
		self._fake_abs(monkeypatch, list(books), covers={
			0: self._abs_cover(books[0], "metadata.json"),
			1: self._abs_cover(books[1], "cover.jpg"),
		})
		delete = self._fake_delete(monkeypatch)
		result = CliRunner().invoke(main, [
			"abs-rescan", "--library", str(tmp_path), "--since", "1h", "--fix-covers",
		], env=self._ENV)
		assert result.exit_code == 0
		assert "Broken covers in the ABS database" in result.output
		assert "not an image file" in result.output
		assert "covers stay as they are" in result.output
		assert delete.calls == []  # dry-run clears nothing

	def test_fix_covers_apply_clears_then_rescans(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
		books = self._make_library(tmp_path)
		self._age_library(tmp_path)
		self._fake_abs(monkeypatch, list(books), covers={0: self._abs_cover(books[0], "metadata.json")})
		delete = self._fake_delete(monkeypatch)
		monkeypatch.setattr(abs_client.time, "sleep", lambda s: None)
		result = CliRunner().invoke(main, [
			"abs-rescan", "--library", str(tmp_path), "--since", "1h", "--fix-covers", "--apply",
		], env=self._ENV)
		assert result.exit_code == 0
		assert "Clearing broken covers" in result.output
		assert "1/1" in result.output
		assert "Cleared 1 stored item cover" in result.output
		# The cleared item joins the scan set even though nothing changed
		# within --since — the rescan is what lets ABS pick a new cover.
		assert delete.urls == ["http://abs.lan:13378/api/items/item-0/cover"]
		assert [c["url"] for c in abs_client._http_post.calls] == [
			"http://abs.lan:13378/api/items/item-0/scan",
		]

	def test_fix_covers_nothing_broken_is_green_noop(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
		books = self._make_library(tmp_path)
		self._age_library(tmp_path)
		for book in books:
			(book / "cover.jpg").write_bytes(b"\xff\xd8jpg")
		self._fake_abs(monkeypatch, list(books), covers={
			0: self._abs_cover(books[0], "cover.jpg"),
			1: self._abs_cover(books[1], "cover.jpg"),
		})
		delete = self._fake_delete(monkeypatch)
		result = CliRunner().invoke(main, [
			"abs-rescan", "--library", str(tmp_path), "--since", "1h", "--fix-covers",
		], env=self._ENV)
		assert result.exit_code == 0
		assert "No broken item covers found" in result.output
		assert delete.calls == []

	def test_fix_covers_union_with_changed_books(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
		import os
		import time as _time

		books = self._make_library(tmp_path)
		# Book 0 changed within --since (matched); book 1 is unchanged but
		# its stored cover row is junk (cleared) -> both end up in the scan set.
		old = _time.time() - 7 * 86400
		for opf in books[1].rglob("metadata.opf"):
			os.utime(opf, (old, old))
		self._fake_abs(monkeypatch, list(books), covers={1: self._abs_cover(books[1], "cover.html")})
		delete = self._fake_delete(monkeypatch)
		monkeypatch.setattr(abs_client.time, "sleep", lambda s: None)
		result = CliRunner().invoke(main, [
			"abs-rescan", "--library", str(tmp_path), "--since", "1h", "--fix-covers", "--abs-workers", "1", "--apply",
		], env=self._ENV)
		assert result.exit_code == 0
		assert delete.urls == ["http://abs.lan:13378/api/items/item-1/cover"]
		assert [c["url"] for c in abs_client._http_post.calls] == [
			"http://abs.lan:13378/api/items/item-0/scan",
			"http://abs.lan:13378/api/items/item-1/scan",
		]

	def test_fix_covers_missing_file_reported(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
		books = self._make_library(tmp_path)
		self._age_library(tmp_path)
		# Image extension but the mapped file does not exist locally.
		self._fake_abs(monkeypatch, list(books), covers={0: self._abs_cover(books[0], "cover.jpg")})
		self._fake_delete(monkeypatch)
		result = CliRunner().invoke(main, [
			"abs-rescan", "--library", str(tmp_path), "--since", "1h", "--fix-covers",
		], env=self._ENV)
		assert result.exit_code == 0
		assert "file missing" in result.output


class TestPathValidation:
	"""CLI commands gracefully report missing library or unopenable cache without traceback."""

	def test_nonexistent_library_exits_with_error(self, tmp_path: Path) -> None:
		nonexistent = tmp_path / "does_not_exist"
		runner = CliRunner()
		for cmd in ["scan", "report", "analyze"]:
			result = runner.invoke(main, [cmd, "--library", str(nonexistent)])
			assert result.exit_code != 0
			assert "Library directory does not exist or is not accessible" in result.output
			assert "Traceback" not in result.output

	def test_unopenable_cache_exits_with_error(self, tmp_path: Path) -> None:
		# Create a file where a directory would need to be created
		blocker = tmp_path / "blocker"
		blocker.write_text("not a dir", encoding="utf-8")
		unopenable_cache = blocker / "sub" / "cache.db"

		lib = tmp_path / "lib"
		lib.mkdir()

		runner = CliRunner()
		result = runner.invoke(main, ["scan", "--library", str(lib)], env={"BMF_CACHE": str(unopenable_cache)})
		assert result.exit_code != 0
		assert "Cannot open cache database" in result.output
		assert "Traceback" not in result.output


class TestAnalyzeEndToEnd:
	"""analyze runs the full two-phase flow (scan progress + per-book
	progress under a non-interactive console) and streams review.yaml."""

	def test_analyze_writes_review(self, tmp_path: Path, monkeypatch) -> None:
		import json as _json

		lib = tmp_path / "lib"
		for i in (1, 2):
			folder = lib / f"Autor{i}" / f"Kniha{i} ({i})"
			folder.mkdir(parents=True)
			(folder / "metadata.json").write_text(_json.dumps({
				"title": f"Kniha{i}",
				"authors": [f"Autor{i}"],
				"isbn": "9788020403117",
				"publishedYear": "2001",
			}), encoding="utf-8")
			(folder / "book.epub").write_text("x", encoding="utf-8")
		review = tmp_path / "review.yaml"
		# Isolate from the host environment: the developer shell may export
		# BMF_REVIEW/BMF_CACHE/BMF_DATABAZEKNIH pointing at the REAL library
		# (CliRunner inherits os.environ) — the test must never touch those.
		monkeypatch.setenv("BMF_REVIEW", str(review))
		monkeypatch.setenv("BMF_CACHE", str(tmp_path / "cache.db"))
		monkeypatch.setenv("BMF_DATABAZEKNIH", "0")
		monkeypatch.setenv("BMF_LIBRARY", str(lib))

		result = CliRunner().invoke(main, [
			"analyze", "--library", str(lib), "--skip-enrich", "--no-check-location",
		])
		assert result.exit_code == 0, result.output
		assert "Running pipeline" in result.output
		text = review.read_text(encoding="utf-8")
		assert "Autor1/Kniha1 (1)" in text
		assert "Autor2/Kniha2 (2)" in text
