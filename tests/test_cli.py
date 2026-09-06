"""Tests for CLI commands: completion installer, shims, strip-covers."""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
from test_covers import (
	_gradient_cover,
	_gradient_jpeg_bytes,
	_make_epub,
	_solid_cover,
	_solid_jpeg_bytes,
)

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
