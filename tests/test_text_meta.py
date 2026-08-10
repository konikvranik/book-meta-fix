"""Tests for text_meta — deterministic metadata extraction from page text.

Fixtures are verbatim samples from a CZ/SK title-page inspection of the real
library (see scripts/llm_experiment.py and the text_meta module docstring).
These are text strings, not EPUB files — the extractor is pure-text.
"""
from __future__ import annotations

from book_meta_fix.text_meta import (
	extract_authors_from_text,
	extract_isbn_from_text,
	extract_metadata_from_text,
	extract_publisher_from_text,
	extract_title_from_text,
	extract_year_from_text,
)


class TestTitleExtraction:
	def test_allcaps_title_after_neznamy_placeholder(self):
		"""The dominant CZ/SK pattern: 'Neznámý' placeholder then ALL-CAPS title."""
		text = "Neznámý JÁDRO GALAXIE Gregory Benford Sérii tvoří šest románů"
		assert extract_title_from_text(text) == "Jádro Galaxie"

	def test_allcaps_title_with_series_number(self):
		text = "Neznámý Jason Dark JOHN SINCLAIR 132 NOČNÍ HOROR Mnoho staletí"
		# ALL-CAPS run including the series number — kept as the title.
		title = extract_title_from_text(text)
		assert title is not None
		assert "Sinclair" in title
		assert "Noční" in title or "Horor" in title

	def test_label_nazev_takes_priority_over_allcaps(self):
		"""When a copyright page carries an explicit 'Název:' label, it wins
		over ALL-CAPS runs elsewhere in the text."""
		text = (
			"Neznámý Název: Poslední cesty obou Quitzowů Autor: Karel May "
			"Nakladatelství: Návrat, 1999 RADOMÍR SUCHÁNEK NAKLADATELSTVÍ NÁVRAT BRNO 1999"
		)
		assert extract_title_from_text(text) == "Poslední Cesty Obou Quitzowů"

	def test_strips_css_leakage_prefix(self):
		"""_strip_html leaks 'Cover @page {...} body {...}' — must be dropped."""
		text = "Cover @page {padding: 0pt; margin:0pt} body {text-align: center;} Neznámý ZASTAVENÝ PŘÍVAL List z počátku"
		title = extract_title_from_text(text)
		assert title == "Zastavený Příval"

	def test_diacritics_preserved_in_title(self):
		text = "Neznámý ČAS PŘÍLIVU Agatha Christie"
		assert extract_title_from_text(text) == "Čas Přílivu"

	def test_returns_none_for_pure_noise(self):
		assert extract_title_from_text("Neznámý Neznámý Neznámý") is None
		assert extract_title_from_text("") is None
		assert extract_title_from_text(None) is None

	def test_returns_none_for_filename_garbage(self):
		"""Filename-derived titles (calibre used the .doc filename) are noise."""
		assert extract_title_from_text("Microsoft Word - A701-User's-CZ.doc") is None

	def test_skips_web_navigation(self):
		text = "Předchozí | Další | Obsah | Hlavní stránka"
		assert extract_title_from_text(text) is None


class TestAuthorExtraction:
	def test_label_autor(self):
		text = "Název: Kniha Autor: Karel May Nakladatelství: Návrat"
		authors = extract_authors_from_text(text)
		assert "Karel May" in authors

	def test_label_autor_with_role_in_parens(self):
		text = "Autor: Jan Novák (editor)"
		authors = extract_authors_from_text(text)
		assert authors == ["Jan Novák"]

	def test_no_author_when_only_placeholder(self):
		text = "Neznámý ZASTAVENÝ PŘÍVAL"
		assert extract_authors_from_text(text) == []

	def test_label_autor_strips_trailing_label(self):
		"""Glued labels must not leak into the author value."""
		text = "Autor: Božena Němcová Nakladatelství: Československý spisovatel"
		authors = extract_authors_from_text(text)
		assert authors == ["Božena Němcová"]


class TestIsbnExtraction:
	def test_isbn_13_in_text(self):
		# Real ISBN-13 from the library (valid check digit).
		text = "ISBN 978-80-720-7232-3 some more text"
		assert extract_isbn_from_text(text) == "9788072072323"

	def test_isbn_10_in_text(self):
		# Real ISBN-10 from the library (valid check digit).
		text = "ISBN: 8072072323"
		assert extract_isbn_from_text(text) == "9788072072323"

	def test_no_isbn_returns_none(self):
		assert extract_isbn_from_text("no isbn here") is None
		assert extract_isbn_from_text(None) is None


class TestPublisherAndYear:
	def test_label_nakladatelstvi(self):
		text = "Nakladatelství: Academia, 2014 nějaký další text"
		pub = extract_publisher_from_text(text)
		assert pub == "Academia"

	def test_label_vydalo(self):
		text = "Vydalo: Argo, Praha 1999"
		assert extract_publisher_from_text(text) == "Argo"

	def test_year_from_label(self):
		text = "Rok vydání: 2014"
		assert extract_year_from_text(text) == 2014

	def test_year_near_publisher(self):
		text = "Nakladatelství: Academia 2014 další text"
		assert extract_year_from_text(text) == 2014

	def test_year_none_when_absent(self):
		assert extract_year_from_text("no year here") is None


class TestExtractMetadataFromText:
	def test_full_structured_copyright_page(self):
		"""The 'gold standard' CZ copyright page with all labels."""
		text = (
			"Neznámý Název: Poslední cesty obou Quitzowů Autor: Karel May "
			"Nakladatelství: Návrat, 1999 RADOMÍR SUCHÁNEK NAKLADATELSTVÍ NÁVRAT BRNO 1999"
		)
		m = extract_metadata_from_text(text)
		assert m.title == "Poslední Cesty Obou Quitzowů"
		assert "Karel May" in m.authors
		assert m.year == 1999
		assert m.source == "content"

	def test_allcaps_only_title_known(self):
		"""When only ALL-CAPS title is present (no labels), title is found but
		author stays empty — offline heuristics cannot split author from title
		when both are ALL-CAPS and glued together."""
		text = "Neznámý JÁDRO GALAXIE Gregory Benford Sérii tvoří šest románů"
		m = extract_metadata_from_text(text)
		assert m.title == "Jádro Galaxie"
		# Author may or may not be found — offline cannot reliably split here.
		# Just assert it does not hallucinate a nonsense name.
		for a in m.authors:
			assert "Neznámý" not in a

	def test_has_any_false_for_empty(self):
		m = extract_metadata_from_text("Neznámý Neznámý")
		assert not m.has_any()

	def test_has_any_true_when_title_found(self):
		m = extract_metadata_from_text("Neznámý JÁDRO GALAXIE")
		assert m.has_any()
