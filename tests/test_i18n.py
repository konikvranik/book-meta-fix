"""Tests for the i18n wrapper (gettext cs/en catalogs, locale detection)."""
from __future__ import annotations

import pytest

from book_meta_fix import i18n
from book_meta_fix.i18n import _, detect_language, get_language, init_language


@pytest.fixture(autouse=True)
def _reset_state():
	"""Isolate the module-global catalog between tests."""
	i18n._translation = None
	i18n._current_language = None
	yield
	i18n._translation = None
	i18n._current_language = None


class TestDetect:
	def test_env_override_wins(self, monkeypatch):
		monkeypatch.setenv("BMF_LANGUAGE", "cs")
		monkeypatch.setenv("LANG", "en_US.UTF-8")
		assert detect_language() == "cs"

	def test_lang_env(self, monkeypatch):
		monkeypatch.delenv("BMF_LANGUAGE", raising=False)
		monkeypatch.setenv("LANG", "cs_CZ.UTF-8")
		assert detect_language() == "cs"

	def test_unsupported_falls_back_to_en(self, monkeypatch):
		monkeypatch.delenv("BMF_LANGUAGE", raising=False)
		monkeypatch.delenv("LC_ALL", raising=False)
		monkeypatch.delenv("LC_MESSAGES", raising=False)
		monkeypatch.setenv("LANG", "de_DE.UTF-8")
		assert detect_language() == "en"

	def test_language_list_takes_first(self, monkeypatch):
		# LANGUAGE-style value "cs_CZ.UTF-8:en_US" — only the first entry counts
		monkeypatch.delenv("BMF_LANGUAGE", raising=False)
		monkeypatch.setenv("LANG", "cs_CZ.UTF-8:en_US")
		assert detect_language() == "cs"


class TestInit:
	def test_init_en_returns_msgid(self):
		assert init_language("en") == "en"
		assert _("No books found.") == "No books found."

	def test_init_cs_translates_known_msgid(self):
		init_language("cs")
		# The cs catalog must translate the canonical header msgid.
		assert _("No books found.") == "Nenalezeny žádné knihy."

	def test_unsupported_code_falls_back(self, monkeypatch):
		monkeypatch.delenv("BMF_LANGUAGE", raising=False)
		monkeypatch.delenv("LC_ALL", raising=False)
		monkeypatch.delenv("LC_MESSAGES", raising=False)
		monkeypatch.setenv("LANG", "de_DE.UTF-8")
		assert init_language("es") == "en"

	def test_none_autodetects(self, monkeypatch):
		monkeypatch.setenv("BMF_LANGUAGE", "en")
		assert init_language(None) == "en"

	def test_lazy_init_on_first_call(self, monkeypatch):
		monkeypatch.setenv("BMF_LANGUAGE", "cs")
		# No explicit init_language call — _() initializes lazily.
		assert get_language() == "cs"


class TestFallback:
	def test_missing_translation_returns_msgid(self):
		init_language("cs")
		assert _("definitely-not-translated-msgid-xyzzy") == "definitely-not-translated-msgid-xyzzy"
