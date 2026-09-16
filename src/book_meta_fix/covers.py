"""Cover image detection and replacement.

Detects auto-generated (Calibre placeholder) covers by pixel analysis and
downloads real covers from enricher-provided URLs.

Detection signals (any one reaching the 0.5 threshold classifies as generated):
  - Dimensions exactly 1200x1600 AND few colours (Calibre default template;
    size alone is unreliable — many photos share it)                +0.5
  - Few significant colours (<= 5 colour buckets each > 2% of pixels,
    after 16-colour quantization)                                        +0.5
  - Dominant colour covers > 60% of pixels (solid background)            +0.2
  - Text-page render: a near-white, colourless sheet with many separated
    ink bands (calibre's ``ebook-meta --get-cover`` RENDERS page 1 for a
    coverless book — white background + black text lines; the colour
    signals above miss it because antialiased ink spreads over 6+ grey
    buckets)                                                        +0.5
  - Vendor no-cover placeholder: the bytes are identical to a registry
    entry (databazeknih serves its light-gray "D"-logo branding image as
    the JSON-LD ``image`` of coverless books — deterministic hash, not
    pixel math; the registry self-refreshes from the live URL, see
    refresh_placeholder_registry)                       generated, conf 1.0
  - Calibre's own marker: the JPEG COM comment ``Generated cover: calibre
    <version>`` written by calibre's cover generator. The parchment
    default template (beige vignette + ornamental border + title text)
    survives every pixel signal — its gradient spreads over 6+ quantized
    buckets and it is neither near-white nor colourless — yet the comment
    is unforgeable self-confession                        generated, conf 1.0

The "few significant colours" signal is the workhorse and is noise-tolerant:
JPEG artefacts fragment a solid background into many near-identical colours,
which saturates a raw unique-colour count and makes it useless. Quantizing
aggressively first collapses the background into one bucket — generated
placeholders (solid background + text) then have very few significant buckets
(<= 5), while real artwork / photos always spread across six or more. This
catches generated covers at ANY size, not just the 1200x1600 default.

A cover is classified "generated" at confidence >= 0.5.

Besides generated placeholders, a cover file can be outright INVALID — not
decodable as an image at all (an HTML page saved as .jpg, a truncated
download, a cover.html masquerading by name). Audiobookshelf picks item
covers by file EXTENSION only (prefers cover.*, else the first png/jpg/
jpeg/webp in the folder — BookScanner.js + globals.SupportedImageTypes), so
such a file becomes the item cover and its ffmpeg resize fails with
"Invalid data found when processing input". image_is_readable() is that
validity check; the strip engine removes invalid covers on demand.

No LLM is involved — all detection is deterministic pixel math via Pillow,
and the replacement URL comes from the existing enricher chain (preferably
databazeknih.cz).
"""
from __future__ import annotations

import logging
import os
import posixpath
import shutil
import subprocess
import tempfile
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from lxml import etree

log = logging.getLogger(__name__)

# Dimensions of Calibre's default "Generate cover" template. Sampling 200
# covers in the library found 119 at exactly this size, all generated.
_CALIBRE_DEFAULT_SIZE = (1200, 1600)

# Confidence threshold for classifying a cover as generated.
_GENERATED_THRESHOLD = 0.5

# Aggressive quantization so JPEG noise collapses a solid background into one
# bucket. Counting raw unique colours is useless — JPEG artefacts fragment the
# background into many near-identical colours and the count saturates at the
# palette maximum (measured on the real library: 3733/4968 covers hit exactly
# 64/64 on a 64-colour quantize). 16 is coarse enough to merge the noise, fine
# enough to still distinguish a multi-band placeholder from a photo.
_QUANTIZE_COLOURS = 16

# A quantized colour bucket is "significant" if it holds more than this share
# of pixels. Counting significant buckets is the cleanest generated-vs-photo
# separator measured on the real library: generated covers have <= 5, photos
# always >= 6 (0 false positives across 2773 photo-like covers).
_SIGNIFICANT_FRAC = 0.02
_FEW_SIGNIFICANT_COLOURS = 5

# Text-page render thresholds (signal 4). A page-1 render is a white sheet of
# black text: near-white dominates, there is essentially no colour anywhere
# (the render is greyscale), and the text shows up as many distinct ink BANDS
# — maximal runs of consecutive ink rows separated by white gaps. Bands, not
# raw ink-row count: a white MINIMALIST cover's title block is 1-3 bands even
# when it is tall, while a page of text has dozens of separated lines.
# Artwork fails the colourlessness gate; a title-only cover fails the band
# count.
_TEXT_WHITE_LUM = 230  # luminance above this counts as "white paper"
_TEXT_MIN_WHITE_FRAC = 0.45
_TEXT_COLOUR_SPREAD = 12  # max-min RGB channel spread still counts as grey
_TEXT_MAX_COLOUR_FRAC = 0.02
_TEXT_INK_LUM = 128  # a pixel darker than this is ink
_TEXT_INK_ROW_PIXELS = 3  # an "ink row" carries at least this many dark px
# Calibrated on the real library (2026-09-15, 4136 covers): real white
# minimalist covers sit at 1-9 bands (a title block + author line; measured
# FP: the Susanna Clarke "Jonathan Strange" cream cover = 9), text-page
# scans start at 11 (Xenograffiti 128x204) and run to 27+. 11 keeps every
# junk scan out of the near misses while leaving a 2-band margin below the
# lowest observed false positive.
_TEXT_MIN_INK_BANDS = 11

# Document-scan signal (signal 6): the SAME page-of-text shape as signal 4,
# but measured RELATIVE to the page's own paper tone. Two junk shapes the
# absolute gates cannot see (both measured in the wild 2026-09-16 on covers
# surviving clean+abs-rescan): a FAINT photocopy (light-grey text above the
# absolute ink threshold, white paper) and text on AGED paper (beige tone:
# under the near-white gate, over the colourless gate). Ink = darker than
# the paper MEDIAN by this much; a text LINE at the 150x200 downscale is a
# short band (<= _DOC_SCAN_MAX_LINE_HEIGHT rows) — tall dark runs are
# illustration blocks, not lines, and keep line-art covers real.
_DOC_SCAN_MIN_PAPER_LUM = 150  # paper must be light (a dark poster's light text never counts)
_DOC_SCAN_PAPER_TOL = 18  # |lum - median| within this = paper pixel
_DOC_SCAN_INK_DROP = 25  # darker than the paper median by this = ink
_DOC_SCAN_MIN_PAPER_FRAC = 0.55
_DOC_SCAN_MAX_LINE_HEIGHT = 3

# Vendor no-cover placeholders: databazeknih serves a shared branding image
# (a light-gray sheet with its "D" logo) as the JSON-LD `image` of coverless
# books. The URL name is stable; the BYTES are not — two generations measured
# in the library (2026-09-15): the current 3625-byte JPEG (3 copies, pixel
# math catches it anyway) and a former 38050-byte one (7 copies, INVISIBLE to
# the pixel math — JPEG noise spreads it over 6+ quantized buckets), each
# byte-identical across books. So the defence is layered:
#   1. is_placeholder_cover_url() refuses the URL where it enters (detail
#      parse, stale cache payloads, download_cover) — catches every NEW
#      download whatever the bytes.
#   2. The md5 registry flags already-downloaded copies (C11 + clean
#      --covers). It is NOT just the seeds below: refresh_placeholder_
#      registry() re-fetches the placeholder URL once per run and learns the
#      CURRENT bytes into the persistent store, so a future generation is
#      captured the first run that sees it. The seeds exist for generations
#      the site no longer serves (unfetchable, but copies sit in the
#      library) and for an offline first run.
_PLACEHOLDER_COVER_URLS = (
	"https://www.databazeknih.cz/img/books/empty_bmid.jpg",
)
# Substring form of the URLs above for matching (case-insensitive) — keep in
# sync with _PLACEHOLDER_COVER_URLS; the scheme is trimmed so http/https
# mirrors match too.
_PLACEHOLDER_COVER_URL_PARTS = ("databazeknih.cz/img/books/empty_bmid",)
_PLACEHOLDER_COVER_MD5 = {
	"bbc424ed9a848cd409f5294c45000d9e",  # current generation, 3625 B
	"a5b62142ebda74fa3f96e9064ff072d5",  # former generation, 38050 B
}

# Version of the cover-analysis heuristics, stored in every persisted verdict.
# Bumping it invalidates ALL cached verdicts in one step: the persistent
# `covers` table is keyed (path, mtime_ns, size) with no heuristic identity,
# so a detector change would otherwise leave every pre-change "not generated"
# verdict frozen forever (an unchanged file never re-analyzes). A stored
# payload with a different/missing version is treated as a cache miss and
# recomputed — old rows self-heal lazily as they are read. v4 added the
# calibre JPEG-COM marker: parchment-template covers measured as "real"
# under v3 carry the marker and must recompute. v5 added the doc_scan
# signal (paper-relative text lines): faint photocopies and aged-paper
# text scans measured as "real" under v4 and must recompute.
_COVER_ANALYSIS_VERSION = 5


@dataclass
class CoverInfo:
	"""Result of analyzing a cover image."""

	width: int = 0
	height: int = 0
	is_generated: bool = False
	confidence: float = 0.0
	signals: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Cover-verdict caching (in-run memo + optional persistent store)
# ---------------------------------------------------------------------------
#
# Decoding a JPEG with Pillow (~72 ms CPU per cover) is by far the most
# expensive thing the C11 detector does, and ONE analyze run asks for the
# same cover up to 4 times: the serial OK-filter, the worker's own detect(),
# and the review writer's projected-clean / identity-verified passes. So the
# verdict is memoized in-process keyed by (path, mtime_ns, size) and — when a
# library.Cache is attached via set_cover_cache() — persisted in the cache
# DB's `covers` table, so the NEXT run skips the decode for unchanged covers
# too. Same invalidation contract as the books cache: mtime/size change
# recomputes. Failed decodes (width == 0) are never cached — a transient NFS
# read error must not freeze a "not an image" verdict for the whole run.

_cover_memo: dict[tuple[str, int, int], CoverInfo] = {}
_cover_memo_lock = threading.Lock()
# Persistent store: any object with get_cover(path, mtime_ns, size) -> dict|None
# and put_cover(path, mtime_ns, size, payload) (library.Cache implements it).
_cover_store: object | None = None
_cover_store_lock = threading.Lock()


def set_cover_cache(store: object | None) -> None:
	"""Attach/detach the persistent cover-verdict store (library.Cache).

	Call with None to fall back to the in-run memo only (e.g. --no-cache).
	Does not clear the in-run memo — stat-keyed entries stay valid.
	"""
	global _cover_store
	with _cover_store_lock:
		_cover_store = store


def clear_cover_cache() -> None:
	"""Drop the in-run memo (test isolation)."""
	with _cover_memo_lock:
		_cover_memo.clear()
	global _placeholder_md5s, _placeholder_registry_refreshed
	_placeholder_md5s = None
	_placeholder_registry_refreshed = False


# ---------------------------------------------------------------------------
# Vendor no-cover placeholder registry (see _PLACEHOLDER_COVER_URLS above)
# ---------------------------------------------------------------------------

# Lazily built seeds ∪ learned set; None = not loaded yet this run.
_placeholder_md5s: set[str] | None = None
_placeholder_registry_refreshed = False


def is_placeholder_cover_url(url: str) -> bool:
	"""True for known vendor no-cover placeholder URLs.

	Checked where a cover URL enters the system (the databazeknih detail
	parse, stale enrich-cache payloads) and again in download_cover before
	any network I/O.
	"""
	lowered = url.lower()
	return any(part in lowered for part in _PLACEHOLDER_COVER_URL_PARTS)


def _known_placeholder_md5s() -> set[str]:
	"""Seed hashes ∪ hashes learned into the persistent cover store."""
	global _placeholder_md5s
	if _placeholder_md5s is None:
		learned: set[str] = set()
		with _cover_store_lock:
			store = _cover_store
		if store is not None and hasattr(store, "get_placeholder_md5s"):
			try:
				learned = {str(h) for h in store.get_placeholder_md5s()}
			except Exception:  # noqa: BLE001 - the registry must never break analysis
				log.debug("placeholder registry load failed", exc_info=True)
		_placeholder_md5s = set(_PLACEHOLDER_COVER_MD5) | learned
	return _placeholder_md5s


# Never learn an image larger than this under the placeholder URL — the two
# real generations are ~4-38 KB; something orders of magnitude bigger is not
# the branding sheet (guard against a future redirect to a full page).
_PLACEHOLDER_FETCH_LIMIT = 512 * 1024


def refresh_placeholder_registry(*, timeout: float = 6.0) -> None:
	"""Re-learn the CURRENT bytes behind the known placeholder URLs.

	databazeknih swaps its no-cover branding image from time to time while
	the URL stays; a stale hash registry would then miss copies of the new
	generation already sitting in the library. Once per process (before any
	detection work) fetch each placeholder URL, hash the bytes and persist
	anything new into the attached cover store. Best-effort by design:
	offline or HTTP failure keeps the seeds — the URL guard still refuses
	new downloads either way. Never raises.
	"""
	global _placeholder_registry_refreshed
	if _placeholder_registry_refreshed:
		return
	_placeholder_registry_refreshed = True
	import hashlib

	import requests

	with _cover_store_lock:
		store = _cover_store
	known = _known_placeholder_md5s()
	for url in _PLACEHOLDER_COVER_URLS:
		try:
			resp = requests.get(
				url,
				timeout=timeout,
				headers={
					"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
					"Accept": "image/*,*/*;q=0.8",
				},
			)
			if resp.status_code != 200 or not resp.content:
				continue
			if len(resp.content) > _PLACEHOLDER_FETCH_LIMIT:
				log.debug("placeholder URL %s serves %d bytes; not the branding sheet", url, len(resp.content))
				continue
			content = resp.content
			md5 = hashlib.md5(content).hexdigest()
		except requests.RequestException as e:
			log.debug("placeholder registry refresh failed for %s: %s", url, e)
			continue
		if md5 in known:
			continue
		# Only a decodable image may enter the registry — an error page or a
		# redirect body under the placeholder URL must not flag real covers.
		try:
			import io

			from PIL import Image

			with Image.open(io.BytesIO(content)) as img:
				img.load()
		except Exception:  # noqa: BLE001
			log.debug("placeholder bytes from %s are not an image; not learned", url)
			continue
		known.add(md5)
		if store is not None and hasattr(store, "add_placeholder_md5"):
			try:
				store.add_placeholder_md5(md5)
			except Exception:  # noqa: BLE001
				log.debug("placeholder registry persist failed", exc_info=True)
		log.info("learned vendor placeholder hash %s from %s", md5, url)


def _vendor_placeholder_signal(data: bytes) -> str | None:
	"""Signal name when *data* is byte-identical to a known vendor no-cover
    placeholder, else None."""
	import hashlib

	if hashlib.md5(data).hexdigest() in _known_placeholder_md5s():
		return "vendor_placeholder (databazeknih)"
	return None


def _calibre_comment_signal(img) -> str | None:
	"""Signal name when the image carries calibre's cover-generator marker.

	Calibre writes ``Generated cover: calibre <version>`` into the JPEG COM
	comment of every cover its generator produces — a deterministic
	self-confession the pixel math cannot replace: the parchment default
	template (beige vignette, ornamental border, title text) spreads over
	6+ quantized colour buckets and is neither near-white nor colourless, so
	every colour/text signal misses it (measured 2026-09-16 on three covers
	surviving `clean --covers --generated --apply` in the real library).
	A false positive is impossible by construction — the comment is written
	by the generator only, never by a scanner or a publisher. Pillow exposes
	JPEG COM segments in ``img.info["comment"]`` (>= 9.4) and in
	``img.applist`` ("Comment" entries); both are checked, only the header
	is read (no pixel decode needed).
	"""
	candidates: list[bytes] = []
	comment = img.info.get("comment")
	if isinstance(comment, bytes):
		candidates.append(comment)
	elif isinstance(comment, str):
		candidates.append(comment.encode("utf-8", "replace"))
	for _marker, payload in getattr(img, "applist", ()) or ():
		if isinstance(payload, bytes):
			candidates.append(payload)
	for raw in candidates:
		text = raw.decode("utf-8", "replace").strip()
		if "generated cover" in text.lower():
			shown = text if len(text) <= 60 else text[:57] + "..."
			return f"calibre_comment ({shown})"
	return None


def is_calibre_generated_cover(data: bytes) -> bool:
	"""True when the image bytes carry calibre's "Generated cover" marker.

	Marker-only by design — no pixel math. The ABS-database cover audit
	(abs_client.broken_cover_items) runs this on cover bytes fetched from
	the server's own cache, where a false positive would delete a cover a
	user uploaded through the ABS UI; the deterministic marker keeps that
	impossible. Header read only; never raises.
	"""
	try:
		import io

		from PIL import Image
	except ImportError:
		return False
	try:
		with Image.open(io.BytesIO(data)) as img:
			return _calibre_comment_signal(img) is not None
	except Exception:  # noqa: BLE001
		return False


def _cover_key(path: Path) -> tuple[str, int, int] | None:
	try:
		st = path.stat()
	except OSError:
		return None
	return (str(path), st.st_mtime_ns, st.st_size)


def _cover_from_payload(d: dict) -> CoverInfo:
	return CoverInfo(
		width=int(d.get("width") or 0),
		height=int(d.get("height") or 0),
		is_generated=bool(d.get("is_generated")),
		confidence=float(d.get("confidence") or 0.0),
		signals=list(d.get("signals") or []),
	)


def _copy_info(info: CoverInfo) -> CoverInfo:
	"""Defensive copy — callers get their own CoverInfo so a mutated result
	can never poison the memoized/persisted instance."""
	return CoverInfo(
		width=info.width, height=info.height, is_generated=info.is_generated,
		confidence=info.confidence, signals=list(info.signals),
	)


def analyze_cover(path: str | Path) -> CoverInfo:
	"""Analyze a cover image and determine whether it looks auto-generated.

	Returns a CoverInfo with is_generated flag and the signals that fired.
	Never raises — on any error returns CoverInfo(is_generated=False).
	Results are cached (see the module comment above): unchanged files are
	never decoded twice in one run, and not at all across runs when a
	persistent store is attached.
	"""
	path = Path(path)
	key = _cover_key(path)
	if key is not None:
		with _cover_memo_lock:
			hit = _cover_memo.get(key)
		if hit is not None:
			return _copy_info(hit)
		with _cover_store_lock:
			store = _cover_store
		if store is not None:
			try:
				payload = store.get_cover(key[0], key[1], key[2])
			except Exception:  # noqa: BLE001 - cache must never break analysis
				payload = None
			if payload is not None and payload.get("v") == _COVER_ANALYSIS_VERSION:
				info = _cover_from_payload(payload)
				with _cover_memo_lock:
					_cover_memo[key] = info
				return _copy_info(info)
			# A stored payload with a different heuristic version predates the
			# current pixel math — fall through and recompute (the fresh verdict
			# overwrites the stale row below).
	info = _analyze_cover_uncached(path)
	# width == 0 means the decode failed — never cache (see module comment).
	if key is not None and info.width:
		with _cover_memo_lock:
			_cover_memo[key] = info
		with _cover_store_lock:
			store = _cover_store
		if store is not None:
			try:
				store.put_cover(key[0], key[1], key[2], {
					"v": _COVER_ANALYSIS_VERSION,
					"width": info.width, "height": info.height,
					"is_generated": info.is_generated,
					"confidence": info.confidence, "signals": info.signals,
				})
			except Exception:  # noqa: BLE001
				log.debug("cover cache store failed for %s", path, exc_info=True)
	return _copy_info(info)


def _text_page_stats(small) -> tuple[float, float, int]:
	"""(near-white fraction, coloured fraction, ink-band count) of the 150x200
	RGB downscale — the raw material of the text-page signal.

	Near-white = luminance >= _TEXT_WHITE_LUM; coloured = any pixel whose
	max-min RGB channel spread exceeds _TEXT_COLOUR_SPREAD (a page render is a
	greyscale render — any real artwork has colour), computed channel-wise via
	ImageChops (no per-pixel Python loop). An ink row carries at least
	_TEXT_INK_ROW_PIXELS pixels darker than _TEXT_INK_LUM; an ink BAND is a
	maximal run of consecutive ink rows (one line of text at this resolution).
	"""
	from PIL import ImageChops

	grey = small.convert("L")
	hist = grey.histogram()
	total = sum(hist) or 1
	white_frac = sum(hist[_TEXT_WHITE_LUM:]) / total
	r, g, b = small.split()
	spread = ImageChops.subtract(
		ImageChops.lighter(ImageChops.lighter(r, g), b),
		ImageChops.darker(ImageChops.darker(r, g), b),
	)
	spread_hist = spread.histogram()
	colour_frac = sum(spread_hist[_TEXT_COLOUR_SPREAD + 1:]) / total
	w, h = small.size
	gp = grey.load()
	bands = 0
	in_band = False
	for y in range(h):
		is_ink = sum(1 for x in range(w) if gp[x, y] < _TEXT_INK_LUM) >= _TEXT_INK_ROW_PIXELS
		if is_ink and not in_band:
			bands += 1
		in_band = is_ink
	return white_frac, colour_frac, bands


def _doc_scan_stats(small) -> tuple[int, float, int]:
	"""(paper median luminance, paper fraction, text-line count) of the
	150x200 RGB downscale — the raw material of the document-scan signal.

	The paper tone is the MEDIAN luminance (a text page is mostly paper, so
	the median IS the paper; artwork has no single dominant tone). Paper
	pixels sit within _DOC_SCAN_PAPER_TOL of the median; ink is RELATIVE —
	darker than the paper by _DOC_SCAN_INK_DROP — which catches the two
	shapes the absolute signal-4 gates cannot see: faint photocopies (ink
	above lum 128) and aged-paper scans (beige tone under the near-white
	gate, over the colourless gate). A text LINE at this resolution is a
	short band (<= _DOC_SCAN_MAX_LINE_HEIGHT rows tall) — tall dark runs are
	illustration blocks and are not counted, keeping line-art covers real.
	"""
	grey = small.convert("L")
	hist = grey.histogram()
	total = sum(hist) or 1
	cum = 0
	med_lum = 255
	for lum, count in enumerate(hist):
		cum += count
		if cum * 2 >= total:
			med_lum = lum
			break
	paper_frac = sum(hist[max(0, med_lum - _DOC_SCAN_PAPER_TOL):med_lum + _DOC_SCAN_PAPER_TOL + 1]) / total
	ink_threshold = max(0, med_lum - _DOC_SCAN_INK_DROP)
	w, h = small.size
	gp = grey.load()
	lines = 0
	band_top: int | None = None
	for y in range(h):
		is_ink = sum(1 for x in range(w) if gp[x, y] <= ink_threshold) >= _TEXT_INK_ROW_PIXELS
		if is_ink and band_top is None:
			band_top = y
		elif not is_ink and band_top is not None:
			if y - band_top <= _DOC_SCAN_MAX_LINE_HEIGHT:
				lines += 1
			band_top = None
	if band_top is not None and h - band_top <= _DOC_SCAN_MAX_LINE_HEIGHT:
		lines += 1
	return med_lum, paper_frac, lines


def _classify_pixels(small, width: int, height: int) -> CoverInfo:
	"""Pixel math shared by the path-based and bytes-based analyzers.

	*small* is the 150x200 RGB downscale of the cover; *width*/*height* are
	the FULL-RESOLUTION dimensions (the Calibre-default size signal compares
	against those).
	"""
	info = CoverInfo(width=width, height=height)

	# Noise-tolerant colour concentration. JPEG artefacts fragment a solid
	# background into many near-identical colours, so counting exact unique
	# colours saturates the palette and is useless (measured: 75% of covers
	# hit exactly 64/64 on a 64-colour quantize). Quantize aggressively first
	# so the background merges into one bucket, then measure concentration.
	significant = 99
	dominant_frac = 0.0
	try:
		quantized = small.quantize(colors=_QUANTIZE_COLOURS)
		counts = sorted(quantized.getcolors(maxcolors=_QUANTIZE_COLOURS) or [], reverse=True)
		total = sum(c for c, _ in counts) or 1
		dominant_frac = counts[0][0] / total if counts else 0.0
		significant = sum(1 for c, _ in counts if c / total > _SIGNIFICANT_FRAC)
	except Exception:  # noqa: BLE001
		pass

	# Signal 1: exact Calibre default dimensions — but only when the cover also
	# looks generated (few colours). Size alone is unreliable: ~1/3 of 1200x1600
	# covers in the library are photos that merely happen to share the default
	# size. Gating on few_colours drops those false positives. (When the gate
	# passes, few_colours below fires too, so this mainly annotates "calibre
	# default template" and lifts confidence for that case.)
	if (width, height) == _CALIBRE_DEFAULT_SIZE and significant <= _FEW_SIGNIFICANT_COLOURS:
		info.confidence += 0.5
		info.signals.append(f"{width}x{height} (calibre default)")

	# Signal 2: very few significant colours. The workhorse — fires on generated
	# placeholders at ANY size (not just 1200x1600) and never on photos.
	if significant <= _FEW_SIGNIFICANT_COLOURS:
		info.confidence += 0.5
		info.signals.append(f"few_colours ({significant} significant)")

	# Signal 3: one colour dominates (solid background). Boosts confidence and
	# documents the signal; alone it is not quite enough, since some real
	# covers also have a large solid area.
	if dominant_frac > 0.60:
		info.confidence += 0.2
		info.signals.append(f"dominant_bg ({dominant_frac:.0%})")

	# Signal 4: text-page render (calibre's page-1 "default cover"). The colour
	# signals above are blind to it — antialiased ink smears over 6+ grey
	# buckets after quantization — so the page SHAPE is measured directly:
	# mostly white, essentially colourless, many separated ink bands (lines).
	try:
		white_frac, colour_frac, bands = _text_page_stats(small)
	except Exception:  # noqa: BLE001
		white_frac, colour_frac, bands = 0.0, 1.0, 0
	if (
		white_frac >= _TEXT_MIN_WHITE_FRAC
		and colour_frac <= _TEXT_MAX_COLOUR_FRAC
		and bands >= _TEXT_MIN_INK_BANDS
	):
		info.confidence += 0.5
		info.signals.append(f"text_page ({bands} ink bands, {white_frac:.0%} white)")

	# Signal 6: document scan — the same page-of-text shape, measured
	# RELATIVE to the page's own paper tone. Catches the two shapes signal 4
	# is blind to (see _doc_scan_stats): light paper (not near-white enough)
	# or light ink (above the absolute threshold). Short line bands only —
	# tall dark runs are illustrations.
	try:
		doc_med, paper_frac, lines = _doc_scan_stats(small)
	except Exception:  # noqa: BLE001
		doc_med, paper_frac, lines = 0, 0.0, 0
	if (
		doc_med >= _DOC_SCAN_MIN_PAPER_LUM
		and paper_frac >= _DOC_SCAN_MIN_PAPER_FRAC
		and lines >= _TEXT_MIN_INK_BANDS
	):
		info.confidence += 0.5
		info.signals.append(f"doc_scan ({lines} lines, {paper_frac:.0%} paper)")

	info.is_generated = info.confidence >= _GENERATED_THRESHOLD
	# Clamp confidence to [0, 1] for display.
	info.confidence = min(1.0, info.confidence)
	return info


def _analyze_cover_uncached(path: Path) -> CoverInfo:
	"""Pixel analysis of *path* — the pre-cache body of analyze_cover."""
	try:
		from PIL import Image
	except ImportError:
		log.debug("Pillow not available; skipping cover analysis")
		return CoverInfo()

	import io

	try:
		# Read the bytes once: the vendor-placeholder hash check needs them
		# and Pillow decodes a BytesIO exactly like a path.
		data = path.read_bytes()
		with Image.open(io.BytesIO(data)) as img:
			width, height = img.size
			# The COM comment rides in the header — grab it while the image
			# is open (info/applist are gone after close).
			calibre_sig = _calibre_comment_signal(img)
			# Downscale for colour analysis (the full-res image is overkill
			# for counting dominant colours and is slow on 1200x1600).
			small = img.convert("RGB").resize((150, 200))
	except Exception as e:  # noqa: BLE001
		log.debug("cover analysis failed for %s: %s", path, e)
		return CoverInfo()
	info = _classify_pixels(small, width, height)
	for sig in (_vendor_placeholder_signal(data), calibre_sig):
		if sig:
			info.signals.append(sig)
			info.is_generated = True
			info.confidence = 1.0
	return info


def analyze_cover_bytes(data: bytes) -> CoverInfo:
	"""Classify in-memory image bytes with the same math as analyze_cover.

	No caching (there is no path/mtime identity) — used for one-shot gates:
	the download-cover validity check and the EPUB-wired recovery path. Never
	raises; an undecodable blob returns CoverInfo(width=0).
	"""
	try:
		from PIL import Image
	except ImportError:
		log.debug("Pillow not available; skipping cover analysis")
		return CoverInfo()

	import io

	try:
		with Image.open(io.BytesIO(data)) as img:
			width, height = img.size
			calibre_sig = _calibre_comment_signal(img)
			small = img.convert("RGB").resize((150, 200))
	except Exception as e:  # noqa: BLE001
		log.debug("cover byte analysis failed: %s", e)
		return CoverInfo()
	info = _classify_pixels(small, width, height)
	for sig in (_vendor_placeholder_signal(data), calibre_sig):
		if sig:
			info.signals.append(sig)
			info.is_generated = True
			info.confidence = 1.0
	return info


def sidecar_cover_usable(path: str | Path) -> bool:
	"""True when the sidecar cover exists AND decodes (width > 0).

	The guard behind "cover already replaced/filled — skip": a 0-byte or
	corrupt cover.jpg must not count as a cover. Measured on the real
	library: 746 zero-byte cover.jpg files (a silent calibre extract
	failure moved into place) masked MISSING_COVER forever, because a bare
	``is_file()`` says yes. Rides the analyze_cover cache, so the decode
	is usually free (the C11 rule has just asked for the same file).
	"""
	try:
		return analyze_cover(Path(path)).width > 0
	except Exception:  # noqa: BLE001
		return False


def extract_cover_from_book(book_path: str | Path, dest: Path | None = None) -> Path | None:
	"""Extract the cover image from an ebook file via calibre's ebook-meta.

	For books without a sidecar cover.jpg but with a cover embedded in the
	EPUB/MOBI/PDB. Returns the path to the extracted image (a temp file unless
	*dest* is given), or None if extraction failed or calibre is unavailable.

	Caveat the caller must know about: for a COVERLESS book calibre may exit 0
	while writing NOTHING — and when *dest* was pre-created (tempfile.mkstemp
	always does), the "file exists" check alone cannot tell a real extract
	from that silent failure. The size > 0 check below closes that hole; the
	recovery gate additionally refuses undecodable output.
	"""
	ebook_meta = shutil.which("ebook-meta")
	if not ebook_meta:
		return None
	if dest is None:
		tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False, prefix="bmf-cover-")
		dest = Path(tmp.name)
		tmp.close()
	else:
		dest = Path(dest)
	try:
		proc = subprocess.run(
			[ebook_meta, str(book_path), f"--get-cover={dest}"],
			capture_output=True, text=True, timeout=15,
		)
		if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
			return None
		return dest
	except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
		return None


def _install_cover_bytes(dest_path: Path, content: bytes) -> bool:
	"""Backup any existing cover, then atomically write *content* to *dest_path*.

	Shared by the download and the EPUB-bytes recovery paths (both arrive with
	already-validated bytes). The .tmp sibling keeps os.replace on one
	filesystem — see recover_cover_from_book for why that matters on NFS.
	"""
	if dest_path.is_file():  # backup existing, mirroring download_cover
		bak = dest_path.with_suffix(dest_path.suffix + ".bak")
		shutil.copy2(dest_path, bak)
	tmp = dest_path.with_suffix(dest_path.suffix + ".tmp")
	try:
		tmp.write_bytes(content)
		os.replace(tmp, dest_path)
	except OSError as e:
		log.warning("failed to write cover to %s: %s", dest_path, e)
		if tmp.exists():
			tmp.unlink(missing_ok=True)
		return False
	return True


def recover_cover_from_book(book_path: str | Path, dest_path: str | Path) -> bool:
	"""Extract the book's embedded cover into *dest_path*, validating it first.

	The fallback when ``cover.jpg`` is missing or a Calibre placeholder and no
	web cover was found. Two extraction paths, strictest first:

	- EPUB: the OPF-wired cover BYTES (:func:`epub_cover_image`) — no calibre
	  subprocess, and crucially no page-render fallback: ``ebook-meta
	  --get-cover`` RENDERS page 1 for a coverless book and hands the render
	  out as a "default cover", which is exactly the junk this recovery exists
	  to avoid (white sheet of text — measured in the library as screenshot
	  covers). A wired cover is the truth; an EPUB without one has no cover.
	- other formats: :func:`extract_cover_from_book` (calibre), whose render
	  output is caught by the pixel gate below.

	Validation (both paths): the result must DECODE (a 0-byte or corrupt
	extract must never become cover.jpg — a size-less file then masks
	MISSING_COVER forever, measured: 746 zero-byte cover.jpg files in the
	library from calibre exiting 0 without writing anything) and must not be
	classified generated by the SAME pixel math as the C11 detector — anything
	we would flag as a generated sidecar is rejected here too, or a C11 book
	would simply extract its own placeholder back out.

	Never raises; returns True only when a real cover landed at *dest_path*.
	"""
	dest_path = Path(dest_path)
	book_path = Path(book_path)

	if book_path.suffix.lower() == ".epub":
		data = epub_cover_image(book_path)
		if data:
			if not image_is_readable(data) or analyze_cover_bytes(data).is_generated:
				log.info("wired cover for %s is unusable; discarding", book_path.name)
				return False
			if _install_cover_bytes(dest_path, data):
				log.info("cover extracted from EPUB: %s -> %s", book_path.name, dest_path.name)
				return True
			return False
		# No wired cover: an EPUB has nothing to extract — do NOT fall through
		# to ebook-meta, whose page-1 render is the screenshot-cover producer.
		return False

	# Non-EPUB: calibre extraction, temp file in the DESTINATION's own
	# directory, not the system /tmp, so the final move stays on one
	# filesystem. os.replace uses rename(2), which fails with EXDEV ("Invalid
	# cross-device link", Errno 18) across mounts — e.g. /tmp on the local
	# disk vs the library on an NFS share. download_cover sidesteps the same
	# trap by writing its .tmp beside the cover. If the dest dir is unusable
	# we fall back to /tmp and rely on shutil.move's cross-device copy+unlink
	# path below.
	try:
		fd, name = tempfile.mkstemp(suffix=".jpg", prefix="bmf-cover-", dir=str(dest_path.parent))
		os.close(fd)
		tmp_path = Path(name)
	except OSError:
		tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False, prefix="bmf-cover-")
		tmp.close()
		tmp_path = Path(tmp.name)
	try:
		if extract_cover_from_book(book_path, dest=tmp_path) is None:
			return False  # calibre absent, wrote nothing, or the file has no embedded cover
		# Decode + generated gates — see docstring. A 0-byte or corrupt output
		# decodes to width 0 and is rejected with the placeholders.
		if not image_is_readable(tmp_path):
			log.info("extracted cover for %s does not decode; discarding", book_path)
			return False
		if analyze_cover(tmp_path).is_generated:
			log.info("extracted cover for %s looks generated; discarding", book_path)
			return False
		if dest_path.is_file():  # backup existing, mirroring download_cover
			bak = dest_path.with_suffix(dest_path.suffix + ".bak")
			shutil.copy2(dest_path, bak)
		# Atomic rename within one filesystem; on the /tmp fallback (or an odd
		# overlay mount) shutil.move transparently falls back to copy + unlink.
		shutil.move(str(tmp_path), str(dest_path))
		log.info("cover extracted from book: %s -> %s", book_path.name, dest_path.name)
		return True
	finally:
		if tmp_path.exists():
			tmp_path.unlink(missing_ok=True)


# EPUB container / package namespaces (container.xml points at the OPF).
_OCF_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
_OPF_NS = "http://www.idpf.org/2007/opf"


def _opf_cover_parts(opf_root):
	"""Locate every cover element in a parsed OPF; single detection source.

	Returns ``(manifest, metadata, spine, guide, img_items, page_items,
	guide_refs)`` — the three core sections plus the cover IMAGE manifest
	items, the cover PAGE items and the guide references. Both the strip
	surgery (:func:`_epub_strip_cover_opf`) and the read-only probe
	(:func:`epub_cover_image`) go through here, so "what counts as the
	embedded cover" is defined exactly once. Recognises the idioms found in
	the wild (calibre writes the first two):

	- EPUB2 ``<meta name="cover" content="item-id">`` + a guide
	  ``<reference type="cover">`` page,
	- EPUB3 ``<item properties="cover-image">``,
	- a spine entry whose idref is the manifest item "cover"/"coverpage".

	EPUB2 meta content ids may point at the image OR (non-standard but
	common) at the cover page itself — told apart by media-type.
	"""
	ns = {"o": _OPF_NS}
	manifest = opf_root.find("o:manifest", ns)
	metadata = opf_root.find("o:metadata", ns)
	spine = opf_root.find("o:spine", ns)
	guide = opf_root.find("o:guide", ns)
	img_items: list = []
	page_items: list = []
	guide_refs: list = []
	if manifest is None:
		return manifest, metadata, spine, guide, img_items, page_items, guide_refs

	items = {i.get("id"): i for i in manifest.findall("o:item", ns)}
	for item in items.values():
		if "cover-image" in (item.get("properties") or "").split():
			img_items.append(item)
	if metadata is not None:
		for meta in metadata.findall("o:meta", ns):
			if meta.get("name") != "cover":
				continue
			item = items.get(meta.get("content") or "")
			if item is None:
				continue
			if (item.get("media-type") or "").startswith("image/"):
				if item not in img_items:  # one item, one removal pass
					img_items.append(item)
			elif item not in page_items:
				page_items.append(item)
	if spine is not None:
		for iref in spine.findall("o:itemref", ns):
			item = items.get(iref.get("idref") or "")
			if item is not None and item.get("id") in ("cover", "coverpage") and item not in page_items:
				page_items.append(item)
	if guide is not None:
		guide_refs = [r for r in guide.findall("o:reference", ns) if r.get("type") == "cover"]
		for ref in guide_refs:
			href = (ref.get("href") or "").split("#", 1)[0]
			for item in items.values():
				if (item.get("href") or "").split("#", 1)[0] == href and item not in page_items:
					page_items.append(item)
	return manifest, metadata, spine, guide, img_items, page_items, guide_refs


def epub_cover_image(book_path: str | Path) -> bytes | None:
	"""Raw bytes of the cover image wired into an EPUB's OPF, or None.

	Unlike calibre's ``ebook-meta --get-cover`` this never falls back to
	RENDERING the first page (calibre calls that a "default cover" — on a
	coverless EPUB it still hands out a 1240x1752 page render), so it is the
	truth for "does this file carry an embedded cover": the GUI preview
	correctly shows nothing after :func:`strip_cover_from_book`. Also saves
	one subprocess per EPUB. Falls back gracefully (None) on any parse
	doubt; callers that prefer the rendered fallback (cover RECOVERY) should
	still use :func:`extract_cover_from_book`.
	"""
	book_path = Path(book_path)
	if book_path.suffix.lower() != ".epub" or not book_path.is_file():
		return None
	try:
		with zipfile.ZipFile(book_path, "r") as zf:
			names = set(zf.namelist())
			if "META-INF/container.xml" not in names:
				return None
			container = etree.fromstring(zf.read("META-INF/container.xml"))
			rootfile = container.find(f".//{{{_OCF_NS}}}rootfile")
			if rootfile is None or not rootfile.get("full-path"):
				return None
			opf_path = rootfile.get("full-path")
			opf_root = etree.fromstring(zf.read(opf_path))
			_manifest, _m, _s, _g, img_items, _p, _r = _opf_cover_parts(opf_root)
			for item in img_items:
				href = (item.get("href") or "").split("#", 1)[0]
				if not href:
					continue
				img = posixpath.normpath(posixpath.join(
					posixpath.dirname(opf_path), unquote(href),
				))
				if img in names:
					return zf.read(img)
		return None
	except (etree.XMLSyntaxError, zipfile.BadZipFile, KeyError, RuntimeError, OSError):
		return None


def _epub_strip_cover_opf(opf_root, opf_dir: str, docs: dict[str, bytes]) -> set[str]:
	"""Cut all cover wiring out of a parsed OPF tree; return zip files to drop.

	Mutates *opf_root* in place. Cover detection comes from
	:func:`_opf_cover_parts` (shared with :func:`epub_cover_image`). The
	cover STATUS markers (meta / guide reference / cover-image property) are
	always removed — that is what makes readers render the title page
	instead of the placeholder. The image and page FILES are dropped too,
	unless a surviving document still references the image inline: deleting
	it then would leave a dangling ``<img src>`` and a broken book, so the
	manifest item stays and only the status goes. *docs* maps every zip
	entry to its bytes for that guard.
	"""
	ns = {"o": _OPF_NS}
	manifest, metadata, spine, guide, img_items, page_items, guide_refs = _opf_cover_parts(opf_root)
	if manifest is None:
		return set()

	def zip_path(href: str) -> str:
		href = unquote((href or "").split("#", 1)[0])
		return posixpath.normpath(posixpath.join(opf_dir, href))

	drop: set[str] = set()
	dropped_pages = {zip_path(i.get("href")) for i in page_items}
	for item in img_items:
		img = zip_path(item.get("href"))
		# Safety guard: referenced from a surviving document → the file (and
		# its manifest item) must stay; only the cover status is removed.
		referenced = any(
			posixpath.basename(img).encode() in data
			for name, data in docs.items()
			if name.endswith((".xhtml", ".html", ".htm")) and name not in dropped_pages
		)
		if referenced:
			props = (item.get("properties") or "").split()
			if "cover-image" in props:
				props.remove("cover-image")
				item.set("properties", " ".join(props))
			continue
		drop.add(img)
		manifest.remove(item)
	page_ids = set()
	for item in page_items:
		drop.add(zip_path(item.get("href")))
		page_ids.add(item.get("id"))
		if item.getparent() is manifest:  # id/media-type splits make dupes impossible; cheap guard
			manifest.remove(item)
	for itemref in list(spine.findall("o:itemref", ns)) if spine is not None else []:
		if (itemref.get("idref") or "") in page_ids:
			spine.remove(itemref)
	for ref in guide_refs:
		guide.remove(ref)
	if metadata is not None:
		for meta in list(metadata.findall("o:meta", ns)):
			if meta.get("name") == "cover":
				metadata.remove(meta)
	return drop


def strip_cover_from_book(book_path: str | Path) -> bool:
	"""Remove the cover EMBEDDED in an ebook file, keeping the book itself.

	The "clean out the invalid calibre cover" counterpart to
	:func:`extract_cover_from_book`: the embedded (typically calibre-written
	placeholder) cover is cut out of the file instead of the whole format
	file being deleted. Only EPUB is supported — calibre's CLI can set/get a
	cover but not remove one, and an EPUB is plain zip+XML so the surgery is
	deterministic; MOBI/AZW3/PRC keep their covers in binary EXTH headers
	(no safe removal without calibre's Python API) and a PDF "cover" is just
	page 1 of the content.

	The zip is rewritten atomically (temp file + ``os.replace``): the cover
	image and cover page entries are dropped, the OPF loses its cover wiring
	(see :func:`_epub_strip_cover_opf`), everything else is byte-identical.
	On ANY doubt (not an EPUB, corrupt zip, unparseable OPF) the file is
	left untouched and False is returned.
	"""
	book_path = Path(book_path)
	if book_path.suffix.lower() != ".epub" or not book_path.is_file():
		return False
	tmp_path: Path | None = None
	try:
		with zipfile.ZipFile(book_path, "r") as zin:
			infos = zin.infolist()
			names = {i.filename for i in infos}
			if "META-INF/container.xml" not in names:
				return False
			docs = {i.filename: zin.read(i.filename) for i in infos}
		container = etree.fromstring(docs["META-INF/container.xml"])
		rootfile = container.find(f".//{{{_OCF_NS}}}rootfile")
		if rootfile is None or not rootfile.get("full-path"):
			return False
		opf_path = rootfile.get("full-path")
		opf_root = etree.fromstring(docs[opf_path])
		drop = _epub_strip_cover_opf(opf_root, posixpath.dirname(opf_path), docs)
		if not drop:
			return False  # no cover wiring found — nothing to strip
		fd, name = tempfile.mkstemp(
			suffix=".epub", prefix="bmf-strip-", dir=str(book_path.parent)
		)
		os.close(fd)
		tmp_path = Path(name)
		with zipfile.ZipFile(tmp_path, "w") as zout:
			for info in infos:
				if info.filename == opf_path or info.filename in drop:
					continue
				# writestr(ZipInfo, ...) keeps each entry's original
				# compress_type/date_time — the leading STORED "mimetype"
				# entry the EPUB spec requires survives untouched.
				zout.writestr(info, docs[info.filename])
			zout.writestr(
				opf_path, etree.tostring(opf_root, xml_declaration=True, encoding="UTF-8")
			)
		os.replace(tmp_path, book_path)
		tmp_path = None
		log.info("embedded cover stripped from %s", book_path.name)
		return True
	except (etree.XMLSyntaxError, zipfile.BadZipFile, KeyError, RuntimeError, OSError) as exc:
		log.warning("cover strip failed for %s: %s", book_path, exc)
		return False
	finally:
		if tmp_path is not None:
			tmp_path.unlink(missing_ok=True)


@dataclass
class CoverStripResult:
	"""Outcome of one book folder's pass of :func:`strip_generated_covers`."""

	path: str
	# ANY generated cover candidate (cover.* or image-extension file — the
	# set ABS picks from) was (or would be, under dry-run) renamed to .bak.
	cover_bak: bool = False
	# The FILE NAMES of those generated sidecars (cover.jpg, cover.png, …);
	# cover_bak is just bool(cover_baks) kept for the summary counters.
	cover_baks: list[str] = field(default_factory=list)
	# EPUB file names whose embedded cover was (or would be) stripped.
	stripped_epubs: list[str] = field(default_factory=list)
	# EPUBs whose cover probed as generated but the strip failed — surfaced in
	# the summary so the failure is visible, not silent.
	failed_epubs: list[str] = field(default_factory=list)
	# Cover files that no image decoder can read (image extension or cover.*
	# name — see strip_generated_covers) renamed to <name>.bak.
	invalid_baks: list[str] = field(default_factory=list)
	# EPUB file names whose embedded cover does not decode as an image and
	# was (or would be) stripped for that reason.
	invalid_epubs: list[str] = field(default_factory=list)
	# Cover files SMALLER than the requested minimum (min_size selector)
	# renamed to <name>.bak, with their pixel size in small_sizes so the
	# report can show what was found.
	small_baks: list[str] = field(default_factory=list)
	small_sizes: dict[str, tuple[int, int]] = field(default_factory=dict)

	@property
	def touched(self) -> bool:
		return (
			self.cover_bak
			or bool(self.stripped_epubs)
			or bool(self.failed_epubs)
			or bool(self.invalid_baks)
			or bool(self.invalid_epubs)
			or bool(self.small_baks)
		)


def probe_embedded_cover(book_path: str | Path, *, data: bytes | None = None) -> CoverInfo:
	"""Analyze the cover EMBEDDED in an ebook file, without touching the file.

	EPUB-only and calibre-free: reads the OPF-wired cover bytes via
	:func:`epub_cover_image` (deliberately no page-render fallback — calibre's
	``--get-cover`` fabricates a "default cover" even for a coverless EPUB),
	spills them to a temp file and runs the same pixel math as the C11
	detector. Returns an empty CoverInfo (is_generated=False) for non-EPUB
	files, EPUBs without an embedded cover and anything unreadable. Never
	raises. Pass *data* to reuse bytes already fetched by the caller instead
	of re-opening the zip.
	"""
	if data is None:
		data = epub_cover_image(book_path)
	if not data:
		return CoverInfo()
	tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False, prefix="bmf-probe-")
	try:
		tmp.write(data)
		tmp.close()
		return analyze_cover(tmp.name)
	finally:
		Path(tmp.name).unlink(missing_ok=True)


def image_is_readable(src: bytes | str | Path) -> bool:
	"""True when Pillow can fully decode *src* as an image.

	The INVALID-cover test shared by the sidecar scan (path) and the embedded
	EPUB probe (bytes): a file that carries an image extension or a cover.*
	name but does not decode is junk a scanner will still pick up as the item
	cover (ABS chooses by extension only), and the consumer then chokes on it
	(ffmpeg: "Invalid data found when processing input"). ``img.load()``
	forces a full pixel decode, so truncated files fail too, not just wrong
	magic bytes. Returns True (nothing can be judged invalid) when Pillow is
	unavailable — the callers then delete nothing, which is the safe default.
	"""
	try:
		from PIL import Image
	except ImportError:
		return True
	try:
		if isinstance(src, bytes):
			import io

			src = io.BytesIO(src)
		with Image.open(src) as img:
			img.load()
		return True
	except Exception:  # noqa: BLE001
		return False


# File extensions Audiobookshelf classifies as images (scanner fallback: with
# no cover.* present it takes the FIRST such file in the folder as the cover).
# Anything undecodable with one of these extensions — or a cover.* name with
# any extension — is an invalid-cover candidate. Also the extension whitelist
# for the ABS-database cover audit (abs_client.broken_cover_items): a stored
# coverPath with any other extension can only be a stale/broken row.
ABS_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp"})


def _invalid_cover_candidate(p: Path) -> bool:
	"""Could a scanner mistake *p* for the book's cover file?"""
	if not p.is_file() or not p.suffix:
		return False
	name = p.name.lower()
	if name.endswith((".bak", ".tmp")):
		return False
	return p.suffix.lower() in ABS_IMAGE_EXTS or p.stem.lower() == "cover"


def strip_generated_covers(
	folder: str | Path, *, dry_run: bool = True,
	generated: str | None = "both", invalid: str | None = None,
	min_size: int | None = None,
) -> CoverStripResult:
	"""Remove GENERATED, INVALID and/or SMALL covers from one book folder.

	Per-folder engine of ``bmf clean --covers``. Two selectors with a scope
	(each ``"external"`` = loose files in the folder, ``"embedded"`` =
	covers inside EPUBs, ``"both"``) plus one external-only threshold:

	- *generated* (default ``"both"``; ``None`` disables) — the C11
	  classification: every cover candidate a scanner may pick (an
	  image-extension file or a ``cover.*`` name — the same set ABS chooses
	  item covers from) classified generated → renamed ``<name>.bak``
	  (reversible; overwrites any existing .bak), an EPUB whose embedded
	  cover probes generated → :func:`strip_cover_from_book` surgery. Bulk
	  cleanup before the pipeline refetches real covers: once the sidecars
	  are gone the book re-fires MISSING_COVER, whose recovery path exists.
	- *invalid* (default ``None`` = off) — covers no image decoder can read
	  (:func:`image_is_readable`): an HTML page saved as .jpg, a truncated
	  download, ``cover.html``. Externals (any image-extension file or
	  ``cover.*`` — the set ABS picks item covers from) are renamed to
	  ``<name>.bak``; an EPUB whose OPF-wired cover bytes do not decode is
	  stripped with the same surgery. These are the files behind ABS's ffmpeg
	  "Invalid data found when processing input" resize errors.
	- *min_size* (default ``None`` = off; pixels on the SHORTER side) —
	  real, decodable covers that are simply too small (the databazeknih
	  thumbnails) are renamed to ``<name>.bak`` over the same external
	  candidate set, so the book re-fires MISSING_COVER and the pipeline
	  refetches a bigger cover (``Enricher.upgrade_cover`` cross-compares
	  the CZ sources by image size and keeps the strictly larger one).
	  External only on purpose: the embedded cover is the recovery FALLBACK
	  when no source serves anything bigger — a small real fallback beats
	  none — and stripping an EPUB is file surgery too invasive for a
	  quality nit. A file whose header cannot be read has no judgeable size
	  and stays for the *invalid* selector.

	Non-EPUB format files are deliberately untouched: their covers live in
	binary EXTH headers with no safe removal path (see
	:func:`strip_cover_from_book`). With *dry_run* nothing is modified — the
	result reports what WOULD happen. Never raises.
	"""
	folder = Path(folder)
	result = CoverStripResult(path=str(folder))

	def _ext(scope: str | None) -> bool:
		return scope in ("external", "both")

	def _emb(scope: str | None) -> bool:
		return scope in ("embedded", "both")

	def _bak_away(path: Path, report: list[str]) -> bool:
		if dry_run:
			report.append(path.name)
			return True
		try:
			os.replace(path, path.with_suffix(path.suffix + ".bak"))
			report.append(path.name)
			return True
		except OSError as exc:
			log.warning("cover strip failed for %s: %s", path, exc)
			return False

	# GENERATED sidecars: EVERY candidate a scanner may pick as the item
	# cover — an image-extension file or a cover.* name
	# (_invalid_cover_candidate, the same set ABS chooses from) — not just
	# cover.jpg. Measured 2026-09-16: 66 of 68 covers still shown as
	# screenshots in ABS after clean+abs-rescan were generated cover.png /
	# cover.gif siblings the cover.jpg-only pass never saw, re-picked by
	# every rescan after the ABS row clear.
	claimed: set[str] = set()
	if _ext(generated):
		for p in sorted(folder.iterdir()):
			if not _invalid_cover_candidate(p):
				continue
			if not analyze_cover(p).is_generated:
				continue
			if _bak_away(p, result.cover_baks):
				claimed.add(p.name)
	result.cover_bak = bool(result.cover_baks)

	if _ext(invalid):
		for p in sorted(folder.iterdir()):
			# A generated cover cannot also be invalid (it decoded into
			# pixels for the C11 math) — the two passes never collide.
			if _invalid_cover_candidate(p) and not image_is_readable(p):
				_bak_away(p, result.invalid_baks)

	if min_size:
		# SMALL covers: external candidates only (see the docstring for why
		# the embedded fallback is kept). analyze_cover rides the persistent
		# cover-verdict cache, so the size of an unchanged cover is free.
		for p in sorted(folder.iterdir()):
			if not _invalid_cover_candidate(p):
				continue
			# The generated pass already claimed this file — under dry-run it
			# is still on disk and must not be reported a second time.
			if p.name in claimed:
				continue
			info = analyze_cover(p)
			# width == 0: undecodable or no Pillow — the invalid pass owns
			# those; without a header there is no size to judge.
			if not info.width:
				continue
			if min(info.width, info.height) < min_size and _bak_away(p, result.small_baks):
				result.small_sizes[p.name] = (info.width, info.height)

	for epub in sorted(folder.glob("*.epub")):
		data = epub_cover_image(epub)
		if data is None:
			continue
		invalid_hit = _emb(invalid) and not image_is_readable(data)
		generated_hit = (
			not invalid_hit and _emb(generated)
			and probe_embedded_cover(epub, data=data).is_generated
		)
		if not (invalid_hit or generated_hit):
			continue
		if dry_run or strip_cover_from_book(epub):
			(result.invalid_epubs if invalid_hit else result.stripped_epubs).append(epub.name)
		else:
			result.failed_epubs.append(epub.name)
	return result


def download_cover(url: str, dest_path: str | Path, *, timeout: float = 15.0) -> bool:
	"""Download a cover image from *url* to *dest_path* atomically.

	Mirrors the _http_get_html pattern from enrichers.py (browser UA, rate-
	limited, graceful failure). Writes via .tmp + os.replace with a .bak
	backup of any existing cover. Validates the response is a real image
	before writing. Returns True on success, False on any failure.
	"""
	import requests

	if is_placeholder_cover_url(url):
		log.warning("refusing known vendor placeholder cover: %s", url)
		return False

	dest_path = Path(dest_path)
	try:
		resp = requests.get(
			url,
			timeout=timeout,
			headers={
				"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
				"Accept": "image/*,*/*;q=0.8",
			},
			stream=True,
		)
		if resp.status_code != 200:
			log.debug("cover download %s -> %s", url, resp.status_code)
			return False
		content = resp.content
	except requests.RequestException as e:
		log.debug("cover download failed for %s: %s", url, e)
		return False

	# Validate the bytes are a real image before writing. Pillow is optional;
	# if it's unavailable we accept the bytes as-is (the URL came from a trusted
	# enricher and is overwhelmingly unlikely to be non-image). The import is
	# hoisted out of the verify try-block so a missing Pillow is not mistaken
	# for an invalid image — that ordering bug previously turned "no PIL" into
	# a hard download failure.
	try:
		from PIL import Image
	except ImportError:
		Image = None  # type: ignore[assignment]

	if Image is not None:
		import io

		try:
			Image.open(io.BytesIO(content)).verify()
		except Exception as e:  # noqa: BLE001
			log.warning("downloaded cover from %s is not a valid image: %s", url, e)
			return False
		# Pixel gate: the URL came from a book-matching source, so a minimalist
		# real cover (few colours) is accepted — the book's own cover beats
		# none. Refused is only the junk: the TEXT-PAGE RENDER (screenshot of
		# page 1) and a KNOWN VENDOR PLACEHOLDER arriving under an URL the
		# registry did not recognize (a mirror/proxy path). "Only real
		# covers." The same math as the C11 detector's signals.
		info = analyze_cover_bytes(content)
		if info.is_generated and any(s.startswith("vendor_placeholder") for s in info.signals):
			log.warning("downloaded cover from %s is a known vendor placeholder; discarding", url)
			return False
		if info.is_generated and any(s.startswith("text_page") for s in info.signals):
			log.warning("downloaded cover from %s looks like a text-page render; discarding", url)
			return False

	# Backup existing cover, then atomic write.
	if not _install_cover_bytes(dest_path, content):
		return False
	log.info("cover downloaded: %s -> %s", url, dest_path.name)
	return True
