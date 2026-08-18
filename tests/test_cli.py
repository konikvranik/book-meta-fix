"""Tests for the install-completion CLI command."""
from __future__ import annotations

from click.testing import CliRunner

from book_meta_fix.cli import main


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
