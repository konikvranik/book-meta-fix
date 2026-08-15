"""Shared test fixtures.

Output-language pinning: user-facing strings (review.yaml header, CLI
messages) are localized via book_meta_fix.i18n and would follow the test
machine's locale. Tests assert the English msgids, so pin the language to
English for every test; tests/test_i18n.py manages the catalog explicitly
(its own autouse fixture runs after this one and resets the module state).
"""
from __future__ import annotations

import pytest

from book_meta_fix import i18n


@pytest.fixture(autouse=True)
def _pin_language_en(monkeypatch):
	monkeypatch.setenv("BMF_LANGUAGE", "en")
	i18n.init_language("en")
	yield
