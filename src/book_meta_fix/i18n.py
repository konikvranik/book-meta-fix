"""Localization: thin wrapper over gettext with cs/en catalogs.

Conventions:
	- msgids are ENGLISH source strings; English is also the fallback when
	  no catalog exists or a translation is missing (no en catalog needed).
	- Default language is auto-detected from the user's locale (env
	  BMF_LANGUAGE > LC_ALL/LC_MESSAGES/LANG > locale.getlocale()); a
	  cs* locale selects Czech, everything else falls back to English.
	- Override order: CLI --lang > BMF_LANGUAGE (env/.env) > locale detect.
	  Use ``init_language(cfg.language or None)`` in a command, or the lazy
	  auto-init: the first ``_()`` call initializes from the environment,
	  so plain imports never crash and cli.py can translate its click
	  ``help=`` texts (built at import time) via the same mechanism.
	- Catalogs live in ``book_meta_fix/locales/<lang>/LC_MESSAGES/bmf.mo``
	  (compiled .mo files are committed so tests/installs don't need
	  pybabel; regenerate via ``make i18n-compile``).
"""
from __future__ import annotations

import gettext
import locale
import os
from pathlib import Path

DOMAIN = "bmf"
LOCALEDIR = Path(__file__).parent / "locales"
SUPPORTED_LANGUAGES = ("cs", "en")

_translation: gettext.NullTranslations | None = None
_current_language: str | None = None


def _normalize(code: str | None) -> str | None:
	"""Reduce a locale code ('cs_CZ.UTF-8', 'en-US') to a base language ('cs')."""
	if not code:
		return None
	base = code.replace("\\", "/").split("/")[-1]  # LANGUAGE may carry a list
	base = base.split("@")[0].split(".")[0].split("_")[0].split("-")[0]
	base = base.strip().lower()
	return base or None


def detect_language() -> str:
	"""Pick the language from the environment / user locale. Never raises."""
	for key in ("BMF_LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
		base = _normalize(os.environ.get(key))
		if base in SUPPORTED_LANGUAGES:
			return base  # type: ignore[return-value]
	try:
		base = _normalize(locale.getlocale()[0] or locale.getdefaultlocale()[0])
	except Exception:
		base = None
	if base in SUPPORTED_LANGUAGES:
		return base  # type: ignore[return-value]
	return "en"


def init_language(lang: str | None = None) -> str:
	"""(Re)initialize the translation catalog and return the active language.

	*lang* is normalized and validated; ``None``/"" triggers locale
	auto-detection. Unsupported codes fall back to English (the msgid
	language), so a typo can never blank out all messages.
	"""
	global _translation, _current_language
	base = _normalize(lang)
	if base not in SUPPORTED_LANGUAGES:
		base = detect_language()
	_translation = gettext.translation(DOMAIN, localedir=str(LOCALEDIR), languages=[base], fallback=True)
	_current_language = base
	return base


def get_language() -> str:
	"""The currently active language code ('cs' | 'en')."""
	if _current_language is None:
		return detect_language()
	return _current_language


def _(message: str) -> str:
	"""Translate *message* (an English msgid) into the active language.

	Lazily initializes the catalog on first use, so callers never need an
	explicit init — importing this module is enough.
	"""
	global _translation
	if _translation is None:
		init_language()
		assert _translation is not None
	return _translation.gettext(message)
