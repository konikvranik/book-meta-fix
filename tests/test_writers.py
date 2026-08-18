"""Tests for the writers' field coverage (OPF mirror + json overlay).

The "everything we fetch gets written" contract: series (calibre:series /
calibre:series_index in the OPF, the series list in the manifest) and
genres/tags (dc:subject) must survive a write — in every shape the library
stores them in.
"""
from __future__ import annotations

import json

from book_meta_fix.models import BookMeta
from book_meta_fix.writers import _json_overlay, _render_opf, clear_verified


class TestVerifiedFlag:
	"""`verified` lives in metadata.json only: emitted when set (never as a
	constant false), never mirrored into the OPF, and clear_verified pops it."""

	def test_overlay_emits_verified_only_when_set(self):
		assert "verified" not in _json_overlay(BookMeta(title="T"))
		assert _json_overlay(BookMeta(title="T", verified=True))["verified"] is True

	def test_opf_never_contains_verified(self):
		opf = _render_opf(BookMeta(title="T", verified=True, uuid="u"))
		assert "verified" not in opf

	def test_clear_verified_pops_key_and_keeps_the_rest(self, tmp_path):
		folder = tmp_path / "book"
		folder.mkdir()
		(folder / "metadata.json").write_text(json.dumps(
			{"title": "Kniha", "verified": True, "narrators": ["X"]}), encoding="utf-8")
		assert clear_verified(folder) is True
		data = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
		assert "verified" not in data
		assert data["title"] == "Kniha"
		assert data["narrators"] == ["X"]
		# A .bak of the pre-clear state is kept (the writers' convention).
		assert (folder / "metadata.json.bak").is_file()

	def test_clear_verified_without_flag_is_noop(self, tmp_path):
		folder = tmp_path / "book"
		folder.mkdir()
		(folder / "metadata.json").write_text(json.dumps({"title": "Kniha"}), encoding="utf-8")
		assert clear_verified(folder) is False
		data = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
		assert data == {"title": "Kniha"}

	def test_clear_verified_missing_folder(self, tmp_path):
		assert clear_verified(tmp_path / "nope") is False


class TestSeriesPair:
	"""BookMeta.series_pair normalises the wild shapes of meta.series."""

	def test_dict_with_index(self):
		# The shape found in this library's metadata.json.
		meta = BookMeta(series=[{"name": "Star Trek - Voyager", "index": "1"}])
		assert meta.series_pair() == ("Star Trek - Voyager", "1")

	def test_dict_with_sequence_key(self):
		# Newer Audiobookshelf builds write "sequence" for the index.
		meta = BookMeta(series=[{"name": "Nadace", "sequence": 3}])
		assert meta.series_pair() == ("Nadace", "3")

	def test_plain_string(self):
		# Calibre-era manifests store bare strings with the index in the name.
		meta = BookMeta(series=["Zaklínač #8"])
		assert meta.series_pair() == ("Zaklínač #8", "")

	def test_dict_without_index(self):
		meta = BookMeta(series=[{"name": "Ren Dhark"}])
		assert meta.series_pair() == ("Ren Dhark", "")

	def test_empty(self):
		assert BookMeta().series_pair() == ("", "")
		assert BookMeta(series=[]).series_pair() == ("", "")


class TestRenderOpfSeries:
	def test_series_meta_tags_present(self, tmp_path):
		meta = BookMeta(title="X", path=str(tmp_path), series=[{"name": "Zaklínač", "index": "8"}])
		opf = _render_opf(meta)
		assert 'name="calibre:series"' in opf
		assert 'content="Zaklínač"' in opf
		assert 'name="calibre:series_index"' in opf
		assert 'content="8"' in opf

	def test_series_without_index_omits_index_tag(self, tmp_path):
		meta = BookMeta(title="X", path=str(tmp_path), series=["Bouřková sezóna"])
		opf = _render_opf(meta)
		assert 'name="calibre:series"' in opf
		assert 'content="Bouřková sezóna"' in opf
		assert "calibre:series_index" not in opf

	def test_no_series_tags_without_series(self, tmp_path):
		opf = _render_opf(BookMeta(title="X", path=str(tmp_path)))
		assert "calibre:series" not in opf


class TestRenderOpfSubjects:
	def test_subjects_from_genres_and_tags_deduped(self, tmp_path):
		meta = BookMeta(title="X", path=str(tmp_path), genres=["sci-fi", "detektivka"], tags=["sci-fi", "překlad"])
		opf = _render_opf(meta)
		assert opf.count("<dc:subject>") == 3
		for s in ("sci-fi", "detektivka", "překlad"):
			assert f">{s}</dc:subject>" in opf

	def test_no_subjects_without_genres_or_tags(self, tmp_path):
		opf = _render_opf(BookMeta(title="X", path=str(tmp_path)))
		assert "<dc:subject>" not in opf


class TestJsonOverlay:
	def test_series_round_trip(self):
		meta = BookMeta(series=[{"name": "Nadace", "index": "2"}])
		overlay = _json_overlay(meta)
		assert overlay["series"] == [{"name": "Nadace", "index": "2"}]
		# Serialisable as the manifest requires.
		assert json.loads(json.dumps(overlay["series"]))

	def test_series_plain_string_preserved_verbatim(self):
		meta = BookMeta(series=["Zaklínač #8"])
		assert _json_overlay(meta)["series"] == ["Zaklínač #8"]

	def test_non_list_series_becomes_empty(self):
		assert _json_overlay(BookMeta(series="Nadace"))["series"] == []
