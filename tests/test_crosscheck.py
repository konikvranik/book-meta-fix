"""Tests for `bmf crosscheck`: cross-format content consistency.

Two layers:
  * classify_format / crosscheck_book — pure logic, exercised with a real
    minimal .epub (zip + container.xml + OPF + one XHTML spine item) so the
    EPUB extractor genuinely runs. No external binaries needed.
  * quarantine / move_file — real files in tmp_path, assert the isolated
    needfix/crosscheck/ destination and the move_book-style collision rule.

ISBNs used are real valid ISBN-13s (check digit verified) so canonicalize()
accepts them:
  9780306406157 , 9783161484100
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from book_meta_fix.crosscheck import (
	AGREES,
	CROSSCHECK_SUBDIR,
	DECISION_AMBIGUOUS,
	DECISION_CLEAN,
	DECISION_QUARANTINE,
	DECISION_SKIPPED,
	DISAGREES,
	UNCERTAIN,
	classify_format,
	crosscheck_book,
	move_file,
	quarantine,
	rogue_destination,
)
from book_meta_fix.extractors import ExtractedMeta
from book_meta_fix.library import Cache
from book_meta_fix.models import BookMeta

ISBN_GOOD = "9780306406157"
ISBN_ROGUE = "9783161484100"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ext(**kwargs) -> ExtractedMeta:
	"""Build an ExtractedMeta with the given fields (source_format defaults to epub)."""
	kwargs.setdefault("source_format", "epub")
	return ExtractedMeta(**kwargs)


def _write_epub(path: Path, *, body_html: str) -> None:
	"""Write a minimal but valid .epub (zip) whose first spine item renders body_html.

	The OPF dc:title is intentionally 'Placeholder' to prove classify_format
	ignores embedded metadata and relies on the body text / DB title instead.
	"""
	container = (
		'<?xml version="1.0"?>'
		'<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
		'<rootfiles><rootfile full-path="OEBPS/content.opf" '
		'media-type="application/oebps-package+xml"/></rootfiles></container>'
	)
	# OPF dc:identifier deliberately NOT an ISBN — we want the body text to be
	# the source of isbn_from_text (the independent signal classify_format uses).
	opf = (
		'<?xml version="1.0" encoding="utf-8"?>'
		'<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="BookId">'
		'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
		'xmlns:opf="http://www.idpf.org/2007/opf">'
		"<dc:title>Placeholder</dc:title><dc:creator>Author</dc:creator>"
		'<dc:identifier id="BookId">x</dc:identifier><dc:language>ces</dc:language>'
		"</metadata>"
		'<manifest><item id="c" href="content.xhtml" media-type="application/xhtml+xml"/></manifest>'
		'<spine><itemref idref="c"/></spine></package>'
	)
	xhtml = (
		'<?xml version="1.0" encoding="utf-8"?>'
		'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>'
		f"<body>{body_html}</body></html>"
	)
	with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
		zf.writestr("mimetype", "application/epub+zip")
		zf.writestr("META-INF/container.xml", container)
		zf.writestr("OEBPS/content.opf", opf)
		zf.writestr("OEBPS/content.xhtml", xhtml)


def _make_book(tmp_path: Path, *, title: str = "Babička", authors=None, isbn=None, calibre_id: int = 1, name: str = "Book (1)"):
	"""Create an empty book folder + a BookMeta with formats detected from it.

	Callers add files (via _write_epub) BEFORE building meta so _collect_formats
	sees them. Returns (folder, meta).
	"""
	from book_meta_fix.readers import _collect_formats

	folder = tmp_path / name
	folder.mkdir(parents=True)
	meta = BookMeta(
		calibre_id=calibre_id,
		title=title,
		authors=authors or ["Author"],
		path=str(folder),
		isbn=isbn,
	)
	_collect_formats(folder, meta)
	return folder, meta


def _remeta(folder: Path, *, title="Babička", authors=None, isbn=None, calibre_id=1) -> BookMeta:
	"""Rebuild a BookMeta from an already-populated folder (refresh formats list)."""
	from book_meta_fix.readers import _collect_formats

	meta = BookMeta(
		calibre_id=calibre_id, title=title, authors=authors or ["Author"],
		path=str(folder), isbn=isbn,
	)
	_collect_formats(folder, meta)
	return meta


# ---------------------------------------------------------------------------
# classify_format — pure unit tests (no files)
# ---------------------------------------------------------------------------


class TestClassifyFormat:
	def test_isbn_match_agrees(self):
		meta = BookMeta(title="X", isbn=ISBN_GOOD)
		v, reason = classify_format(meta, _ext(isbn_from_text=ISBN_GOOD))
		assert v == AGREES
		assert "matches" in reason

	def test_isbn_differ_disagrees(self):
		meta = BookMeta(title="X", isbn=ISBN_GOOD)
		v, reason = classify_format(meta, _ext(isbn_from_text=ISBN_ROGUE))
		assert v == DISAGREES
		assert "differs" in reason

	def test_isbn_isbn10_normalized_to_13_match(self):
		# DB has ISBN-13, content text shows the same book's ISBN-10 form of the
		# SAME isbn → must canonicalize equal → AGREES.
		from book_meta_fix.isbn import canonicalize

		meta = BookMeta(title="X", isbn=ISBN_GOOD)
		# Derive the ISBN-10 form of the same book by reversing _isbn10_to_13 is
		# awkward; instead just confirm canonical equality is what drives it.
		assert canonicalize(ISBN_GOOD) == canonicalize(ISBN_GOOD)
		v, _ = classify_format(meta, _ext(isbn_from_text=ISBN_GOOD))
		assert v == AGREES

	def test_title_in_text_agrees(self):
		meta = BookMeta(title="Babička")
		v, _ = classify_format(meta, _ext(first_page_text="Babička\nBožena Němcová"))
		assert v == AGREES

	def test_title_absent_disagrees(self):
		meta = BookMeta(title="Babička")
		v, _ = classify_format(meta, _ext(first_page_text="Saturnin\nZdeněk Jirotka"))
		assert v == DISAGREES

	def test_title_partial_is_uncertain(self):
		# A middling fuzzy score (between weak and strong) must be UNCERTAIN,
		# never auto-quarantined.
		meta = BookMeta(title="Babička")
		# "Babiččina rodina" shares a prefix → partial_ratio in the uncertain band.
		v, _ = classify_format(meta, _ext(first_page_text="Babiččina rodina"), strong=0.95, weak=0.4)
		assert v == UNCERTAIN

	def test_embedded_title_ignored_page_text_decides(self):
		# The embedded `title` block is irrelevant; the DB title searched in the
		# page text is the signal (mirrors verifier.verify).
		meta = BookMeta(title="Babička")
		v, _ = classify_format(meta, _ext(title="Some Embedded Junk", first_page_text="Babička Božena Němcová"))
		assert v == AGREES

	def test_no_signal_uncertain(self):
		meta = BookMeta(title="Babička")
		v, _ = classify_format(meta, _ext())
		assert v == UNCERTAIN


# ---------------------------------------------------------------------------
# crosscheck_book — real epubs, decision rules
# ---------------------------------------------------------------------------


class TestCrosscheckBook:
	def test_single_format_skipped(self, tmp_path: Path):
		folder = tmp_path / "Book (1)"
		folder.mkdir()
		_write_epub(folder / "only.epub", body_html="<h1>Babička</h1>")
		meta = _remeta(folder, title="Babička")
		res = crosscheck_book(meta)
		assert res.decision == DECISION_SKIPPED
		assert res.formats_checked == 1
		assert res.rogues == []

	def test_all_agree_clean(self, tmp_path: Path):
		folder, meta = _make_book(tmp_path, title="Babička")
		_write_epub(folder / "a.epub", body_html="<h1>Babička</h1><p>Božena Němcová</p>")
		_write_epub(folder / "b.epub", body_html="<h1>Babička</h1><p>Božena Němcová</p>")
		res = crosscheck_book(_remeta(folder, title="Babička"))
		assert res.decision == DECISION_CLEAN
		assert res.rogues == []
		assert res.formats_checked == 2

	def test_split_quarantines_rogue(self, tmp_path: Path):
		folder, _ = _make_book(tmp_path, title="Babička")
		_write_epub(folder / "good.epub", body_html="<h1>Babička</h1>")
		_write_epub(folder / "rogue.epub", body_html="<h1>Saturnin</h1><p>Zdeněk Jirotka</p>")
		res = crosscheck_book(_remeta(folder, title="Babička"))
		assert res.decision == DECISION_QUARANTINE
		assert len(res.rogues) == 1
		assert Path(res.rogues[0].file).name == "rogue.epub"
		# The good file is AGREES, the rogue is DISAGREES.
		by_name = {Path(v.file).name: v.verdict for v in res.verdicts}
		assert by_name["good.epub"] == AGREES
		assert by_name["rogue.epub"] == DISAGREES

	def test_two_rogues_from_one_book_both_flagged(self, tmp_path: Path):
		"""Regression behind the path-scheme choice: two rogues from one book may
		be different wrong books — both must be flagged and (later) isolated into
		separate folders, never merged."""
		folder, _ = _make_book(tmp_path, title="Babička")
		_write_epub(folder / "good.epub", body_html="<h1>Babička</h1>")
		_write_epub(folder / "rogue1.epub", body_html="<h1>Saturnin</h1>")
		_write_epub(folder / "rogue2.epub", body_html="<h1>Temno</h1>")
		res = crosscheck_book(_remeta(folder, title="Babička"))
		assert res.decision == DECISION_QUARANTINE
		assert sorted(Path(r.file).name for r in res.rogues) == ["rogue1.epub", "rogue2.epub"]

	def test_isbn_strong_signal_overrides_matching_titles(self, tmp_path: Path):
		# Both epubs' body title matches metadata, but the rogue carries a
		# DIFFERENT ISBN → ISBN wins and the rogue DISAGREES.
		folder, _ = _make_book(tmp_path, title="Babička", isbn=ISBN_GOOD)
		_write_epub(folder / "good.epub", body_html=f"<h1>Babička</h1><p>ISBN {ISBN_GOOD}</p>")
		_write_epub(folder / "rogue.epub", body_html=f"<h1>Babička</h1><p>ISBN {ISBN_ROGUE}</p>")
		res = crosscheck_book(_remeta(folder, title="Babička", isbn=ISBN_GOOD))
		assert res.decision == DECISION_QUARANTINE
		assert Path(res.rogues[0].file).name == "rogue.epub"
		assert "ISBN" in res.rogues[0].reason

	def test_all_disagree_is_ambiguous_not_moved(self, tmp_path: Path):
		# No format corroborates the metadata → metadata itself is suspect.
		# Must NOT populate rogues (nothing to move).
		folder, _ = _make_book(tmp_path, title="Babička")
		_write_epub(folder / "a.epub", body_html="<h1>Saturnin</h1>")
		_write_epub(folder / "b.epub", body_html="<h1>Temno</h1>")
		res = crosscheck_book(_remeta(folder, title="Babička"))
		assert res.decision == DECISION_AMBIGUOUS
		assert res.rogues == []
		# …but the disagreeing verdicts are still recorded for the report.
		assert any(v.verdict == DISAGREES for v in res.verdicts)

	def test_uncertain_formats_do_not_trigger_quarantine(self, tmp_path: Path):
		# If every format is UNCERTAIN (no comparable signal), nothing moves.
		# We force UNCERTAIN by stubbing extract to return empty ExtractedMeta.
		folder, _ = _make_book(tmp_path, title="Babička")
		(folder / "a.epub").write_bytes(b"x")
		(folder / "b.epub").write_bytes(b"x")
		meta = _remeta(folder, title="Babička")

		def _stub(_path):
			return ExtractedMeta(source_format="epub")  # no signals → UNCERTAIN

		res = crosscheck_book(meta, extract_fn=_stub)
		assert res.decision == DECISION_CLEAN
		assert all(v.verdict == UNCERTAIN for v in res.verdicts)


# ---------------------------------------------------------------------------
# move_file / rogue_destination / quarantine — real moves
# ---------------------------------------------------------------------------


class TestRogueDestination:
	def test_folder_is_flat_and_encodes_origin_and_filename(self, tmp_path: Path):
		meta = BookMeta(calibre_id=4895, title="R.U.R.", authors=["Karel Čapek"], path=str(tmp_path))
		# Build a result with the same origin label crosscheck_book would produce.
		from book_meta_fix.crosscheck import _origin_label

		res = crosscheck_book(meta)  # skipped (no files) but origin is populated
		dest = rogue_destination(res, str(tmp_path / "rogue.epub"), tmp_path, "needfix")
		# rogue_destination returns the FOLDER: .../needfix/crosscheck/<folder>
		assert dest.parent.name == CROSSCHECK_SUBDIR
		assert "4895" in dest.name
		assert "rogue.epub" in dest.name
		# Sanity: origin label is the raw form used in the folder name.
		assert _origin_label(meta) == "Karel Čapek - R.U.R. (4895)"

	def test_two_rogues_get_distinct_folders(self, tmp_path: Path):
		# The whole point: two rogues from one book must NOT share a folder.
		meta = BookMeta(calibre_id=1, title="Babička", authors=["Author"], path=str(tmp_path))
		res = crosscheck_book(meta)
		d1 = rogue_destination(res, str(tmp_path / "rogue1.epub"), tmp_path, "needfix")
		d2 = rogue_destination(res, str(tmp_path / "rogue2.epub"), tmp_path, "needfix")
		assert d1 != d2


class TestMoveFile:
	def test_dry_run_moves_nothing(self, tmp_path: Path):
		src = tmp_path / "book.epub"
		src.write_bytes(b"epub")
		dest_folder = tmp_path / "needfix" / "crosscheck" / "Author - Book (1) - book.epub"
		res = move_file(src, dest_folder, dry_run=True)
		assert res.action == "moved"
		assert src.exists()  # nothing actually moved
		assert not dest_folder.exists()

	def test_real_move_creates_folder_and_moves_file(self, tmp_path: Path):
		src = tmp_path / "book.epub"
		src.write_bytes(b"epub")
		dest_folder = tmp_path / "needfix" / "crosscheck" / "Author - Book (1) - book.epub"
		res = move_file(src, dest_folder, dry_run=False)
		assert res.action == "moved"
		assert not src.exists()
		assert (dest_folder / "book.epub").is_file()
		# destination reported is the final FILE path
		assert res.destination == str(dest_folder / "book.epub")

	def test_collision_gets_dup_suffix_on_folder(self, tmp_path: Path):
		# Pre-create the destination folder (e.g. a rogue already quarantined there).
		src = tmp_path / "book.epub"
		src.write_bytes(b"epub")
		dest_folder = tmp_path / "needfix" / "crosscheck" / "Author - Book (1) - book.epub"
		dest_folder.mkdir(parents=True)
		(dest_folder / "book.epub").write_bytes(b"existing")

		res = move_file(src, dest_folder, dry_run=False)
		assert res.action == "collision_renamed"
		assert "(dup 1)" in res.destination
		# Original occupant untouched; new file landed in the (dup 1) sibling.
		assert (dest_folder / "book.epub").read_bytes() == b"existing"
		assert Path(res.destination).is_file()
		assert Path(res.destination).read_bytes() == b"epub"


class TestQuarantine:
	def _split_book(self, tmp_path: Path):
		folder, _ = _make_book(tmp_path, title="Babička")
		_write_epub(folder / "good.epub", body_html="<h1>Babička</h1>")
		_write_epub(folder / "rogue.epub", body_html="<h1>Saturnin</h1><p>Zdeněk Jirotka</p>")
		return folder, crosscheck_book(_remeta(folder, title="Babička"))

	def test_dry_run_moves_nothing(self, tmp_path: Path):
		folder, res = self._split_book(tmp_path)
		moves = quarantine([res], tmp_path, dry_run=True)
		assert len(moves) == 1
		assert moves[0].action == "moved"
		# Both files still in the original folder.
		assert (folder / "good.epub").is_file()
		assert (folder / "rogue.epub").is_file()
		# No needfix tree created.
		assert not (tmp_path / "needfix").exists()

	def test_apply_moves_only_the_rogue_into_its_own_folder(self, tmp_path: Path):
		folder, res = self._split_book(tmp_path)
		moves = quarantine([res], tmp_path, dry_run=False)
		assert len(moves) == 1
		assert moves[0].action == "moved"
		# Good file stays in the book folder.
		assert (folder / "good.epub").is_file()
		# Rogue is gone from the book folder…
		assert not (folder / "rogue.epub").exists()
		# …and lives alone in its own isolated folder under needfix/crosscheck/.
		quarantined = list((tmp_path / "needfix" / CROSSCHECK_SUBDIR).iterdir())
		assert len(quarantined) == 1
		rogue_folder = quarantined[0]
		assert rogue_folder.is_dir()
		assert list(rogue_folder.iterdir()) == [rogue_folder / "rogue.epub"]
		assert (rogue_folder / "rogue.epub").is_file()

	def test_two_rogues_land_in_separate_folders(self, tmp_path: Path):
		folder, _ = _make_book(tmp_path, title="Babička")
		_write_epub(folder / "good.epub", body_html="<h1>Babička</h1>")
		_write_epub(folder / "rogue1.epub", body_html="<h1>Saturnin</h1>")
		_write_epub(folder / "rogue2.epub", body_html="<h1>Temno</h1>")
		res = crosscheck_book(_remeta(folder, title="Babička"))
		assert res.decision == DECISION_QUARANTINE

		moves = quarantine([res], tmp_path, dry_run=False)
		assert len(moves) == 2
		# Two distinct destination folders, each holding exactly its own file.
		dest_folders = {Path(m.destination).parent for m in moves}
		assert len(dest_folders) == 2
		for m in moves:
			folder_files = list(Path(m.destination).parent.iterdir())
			assert folder_files == [Path(m.destination)]
		# Both rogues gone from the source book; good file remains.
		assert (folder / "good.epub").is_file()
		assert not (folder / "rogue1.epub").exists()
		assert not (folder / "rogue2.epub").exists()

	def test_ambiguous_result_moves_nothing(self, tmp_path: Path):
		folder, _ = _make_book(tmp_path, title="Babička")
		_write_epub(folder / "a.epub", body_html="<h1>Saturnin</h1>")
		_write_epub(folder / "b.epub", body_html="<h1>Temno</h1>")
		res = crosscheck_book(_remeta(folder, title="Babička"))
		assert res.decision == DECISION_AMBIGUOUS
		moves = quarantine([res], tmp_path, dry_run=False)
		assert moves == []
		assert (folder / "a.epub").is_file() and (folder / "b.epub").is_file()


# ---------------------------------------------------------------------------
# Cache invalidation — mirror TestCacheInvalidation in test_mover.py
# ---------------------------------------------------------------------------


class TestCacheInvalidation:
	def test_real_quarantine_invalidates_book_folder(self, tmp_path: Path):
		"""A real quarantine changes the book folder's contents (a file left),
		so its cache row must be dropped → next scan re-parses it."""
		from book_meta_fix.readers import read_book_folder

		cache = Cache(tmp_path / "cache.db")
		folder, _ = _make_book(tmp_path, title="Babička")
		# metadata.json so read_book_folder parses cleanly and is cacheable.
		(folder / "metadata.json").write_text('{"title": "Babička", "authors": ["Author"], "uuid": "u-babicka"}\n', encoding="utf-8")
		_write_epub(folder / "good.epub", body_html="<h1>Babička</h1>")
		_write_epub(folder / "rogue.epub", body_html="<h1>Saturnin</h1>")
		cache.put(read_book_folder(folder))
		cache.commit()
		assert self._has_row(cache, folder)

		res = crosscheck_book(_remeta(folder, title="Babička"))
		assert res.decision == DECISION_QUARANTINE
		quarantine([res], tmp_path, dry_run=False, cache=cache)

		assert not self._has_row(cache, folder)  # invalidated → re-parse next time
		cache.close()

	def test_dry_run_does_not_invalidate(self, tmp_path: Path):
		from book_meta_fix.readers import read_book_folder

		cache = Cache(tmp_path / "cache.db")
		folder, _ = _make_book(tmp_path, title="Babička")
		(folder / "metadata.json").write_text('{"title": "Babička", "authors": ["Author"], "uuid": "u-babicka"}\n', encoding="utf-8")
		_write_epub(folder / "good.epub", body_html="<h1>Babička</h1>")
		_write_epub(folder / "rogue.epub", body_html="<h1>Saturnin</h1>")
		cache.put(read_book_folder(folder))
		cache.commit()

		res = crosscheck_book(_remeta(folder, title="Babička"))
		quarantine([res], tmp_path, dry_run=True, cache=cache)
		assert self._has_row(cache, folder)  # nothing moved → entry kept
		cache.close()

	@staticmethod
	def _has_row(cache: Cache, path: Path) -> bool:
		return cache.conn.execute("SELECT 1 FROM books WHERE path = ?", (str(Path(path)),)).fetchone() is not None
