"""Cover image detection and replacement.

Detects auto-generated (Calibre placeholder) covers by pixel analysis and
downloads real covers from enricher-provided URLs.

Detection signals (combined, each adds confidence):
  - Dimensions exactly 1200x1600  (Calibre default template signature)  +0.5
  - Few unique colours after quantization (< 5000 at 64 colours)        +0.3
  - Dominant colour covers > 60% of pixels (solid background + text)    +0.2

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

# Below this many unique colours (after quantizing to 64), the cover is
# almost certainly text-on-solid-background (a generated placeholder).
_LOW_COLOR_COUNT = 5000


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

	# Signal 1: exact Calibre default dimensions.
	if (info.width, info.height) == _CALIBRE_DEFAULT_SIZE:
		info.confidence += 0.5
		info.signals.append(f"{info.width}x{info.height} (calibre default)")

	# Signal 2: few unique colours (quantize to 64-colour palette, count).
	try:
		quantized = small.quantize(colors=64)
		colours = quantized.getcolors(maxcolors=64) or []
		unique = len(colours)
		if unique < _LOW_COLOR_COUNT // 100:  # ~50 unique colours at 64-colour quant
			info.confidence += 0.3
			info.signals.append(f"low_colour_count ({unique} unique)")
	except Exception:  # noqa: BLE001
		pass

	# Signal 3: dominant colour covers > 60% of pixels.
	try:
		colours = small.getcolors(maxcolors=65536)  # [(count, (r,g,b)), ...]
		if colours:
			total = sum(c for c, _ in colours)
			colours_sorted = sorted(colours, key=lambda x: x[0], reverse=True)
			dominant_frac = colours_sorted[0][0] / total if total else 0
			if dominant_frac > 0.60:
				info.confidence += 0.2
				info.signals.append(f"dominant_bg ({dominant_frac:.0%})")
	except Exception:  # noqa: BLE001
		pass

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

	# Validate the bytes are a real image before writing.
	try:
		from PIL import Image

		import io
		Image.open(io.BytesIO(content)).verify()
	except Exception as e:  # noqa: BLE001
		log.warning("downloaded cover from %s is not a valid image: %s", url, e)
		return False
	except ImportError:
		pass  # Pillow unavailable — accept bytes as-is

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
