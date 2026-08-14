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
import posixpath
import shutil
import subprocess
import tempfile
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
