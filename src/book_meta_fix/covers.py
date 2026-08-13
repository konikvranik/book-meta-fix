"""Cover image detection and replacement.

Detects auto-generated (Calibre placeholder) covers by pixel analysis and
downloads real covers from enricher-provided URLs.

Detection signals (any one reaching the 0.5 threshold classifies as generated):
  - Dimensions exactly 1200x1600 AND few colours (Calibre default template;
    size alone is unreliable — many photos share it)                +0.5
  - Few significant colours (<= 5 colour buckets each > 2% of pixels,
    after 16-colour quantization)                                        +0.5
  - Dominant colour covers > 60% of pixels (solid background)            +0.2

The "few significant colours" signal is the workhorse and is noise-tolerant:
JPEG artefacts fragment a solid background into many near-identical colours,
which saturates a raw unique-colour count and makes it useless. Quantizing
aggressively first collapses the background into one bucket — generated
placeholders (solid background + text) then have very few significant buckets
(<= 5), while real artwork / photos always spread across six or more. This
catches generated covers at ANY size, not just the 1200x1600 default.

A cover is classified "generated" at confidence >= 0.5.

No LLM is involved — all detection is deterministic pixel math via Pillow,
and the replacement URL comes from the existing enricher chain (preferably
databazeknih.cz).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass
class CoverInfo:
	"""Result of analyzing a cover image."""

	width: int = 0
	height: int = 0
	is_generated: bool = False
	confidence: float = 0.0
	signals: list[str] = field(default_factory=list)


def analyze_cover(path: str | Path) -> CoverInfo:
	"""Analyze a cover image and determine whether it looks auto-generated.

	Returns a CoverInfo with is_generated flag and the signals that fired.
	Never raises — on any error returns CoverInfo(is_generated=False).
	"""
	try:
		from PIL import Image
	except ImportError:
		log.debug("Pillow not available; skipping cover analysis")
		return CoverInfo()

	path = Path(path)
	info = CoverInfo()
	try:
		with Image.open(path) as img:
			info.width, info.height = img.size
			# Downscale for colour analysis (the full-res image is overkill
			# for counting dominant colours and is slow on 1200x1600).
			small = img.convert("RGB").resize((150, 200))
	except Exception as e:  # noqa: BLE001
		log.debug("cover analysis failed for %s: %s", path, e)
		return info

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
	if (info.width, info.height) == _CALIBRE_DEFAULT_SIZE and significant <= _FEW_SIGNIFICANT_COLOURS:
		info.confidence += 0.5
		info.signals.append(f"{info.width}x{info.height} (calibre default)")

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

	info.is_generated = info.confidence >= _GENERATED_THRESHOLD
	# Clamp confidence to [0, 1] for display.
	info.confidence = min(1.0, info.confidence)
	return info


def extract_cover_from_book(book_path: str | Path, dest: Path | None = None) -> Path | None:
	"""Extract the cover image from an ebook file via calibre's ebook-meta.

	For books without a sidecar cover.jpg but with a cover embedded in the
	EPUB/MOBI/PDB. Returns the path to the extracted image (a temp file unless
	*dest* is given), or None if extraction failed or calibre is unavailable.
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
		if proc.returncode != 0 or not dest.is_file():
			return None
		return dest
	except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
		return None


def recover_cover_from_book(book_path: str | Path, dest_path: str | Path) -> bool:
	"""Extract the book's embedded cover into *dest_path*, validating it first.

	The fallback when ``cover.jpg`` is missing or a Calibre placeholder and no
	web cover was found. Extracts via :func:`extract_cover_from_book` (calibre
	``ebook-meta --get-cover``) into a temp file, REJECTS the result if it is
	itself a generated placeholder, then atomically moves it into place with a
	``.bak`` backup — mirroring :func:`download_cover`. Never raises; returns
	True only when a real cover landed at *dest_path*.

	The generated-placeholder gate is mandatory and uses the SAME pixel math as
	the C11 detector (:func:`analyze_cover`): anything we would flag as a
	generated sidecar is rejected here too. Calibre embeds the covers it
	generates, so without this gate a C11 book would simply extract its own
	placeholder back out — and a generated extract can never become ``cover.jpg``
	(the book would just re-fire C11 on the next scan).
	"""
	dest_path = Path(dest_path)
	# Extract into a temp file in the DESTINATION's own directory, not the
	# system /tmp, so the final move stays on one filesystem. os.replace uses
	# rename(2), which fails with EXDEV ("Invalid cross-device link", Errno 18)
	# across mounts — e.g. /tmp on the local disk vs the library on an NFS
	# share. download_cover sidesteps the same trap by writing its .tmp beside
	# the cover. If the dest dir is unusable we fall back to /tmp and rely on
	# shutil.move's cross-device copy+unlink path below.
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
			return False  # calibre absent, or the file has no embedded cover
		# Generated-placeholder gate — see docstring.
		if analyze_cover(tmp_path).is_generated:
			log.info("extracted cover for %s looks generated; discarding", book_path)
			return False
		if dest_path.is_file():  # backup existing, mirroring download_cover
			bak = dest_path.with_suffix(dest_path.suffix + ".bak")
			shutil.copy2(dest_path, bak)
		# Atomic rename within one filesystem; on the /tmp fallback (or an odd
		# overlay mount) shutil.move transparently falls back to copy + unlink.
		shutil.move(str(tmp_path), str(dest_path))
		log.info("cover extracted from book: %s -> %s", Path(book_path).name, dest_path.name)
		return True
	finally:
		if tmp_path.exists():
			tmp_path.unlink(missing_ok=True)


def download_cover(url: str, dest_path: str | Path, *, timeout: float = 15.0) -> bool:
	"""Download a cover image from *url* to *dest_path* atomically.

	Mirrors the _http_get_html pattern from enrichers.py (browser UA, rate-
	limited, graceful failure). Writes via .tmp + os.replace with a .bak
	backup of any existing cover. Validates the response is a real image
	before writing. Returns True on success, False on any failure.
	"""
	import requests

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

	# Backup existing cover, then atomic write.
	if dest_path.is_file():
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
	log.info("cover downloaded: %s -> %s", url, dest_path.name)
	return True
