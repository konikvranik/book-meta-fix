#!/usr/bin/env python3
"""Calibrate the C11 text-page signal against the real library.

Walks every book folder's cover.jpg, computes the raw text-page stats
(near-white fraction, coloured fraction, ink-band count) with the CURRENT
thresholds from book_meta_fix.covers, and reports:

  - how many covers the signal would flag today (-> is_generated),
  - near misses per gate (so a threshold tweak can be judged by count),
  - the flagged files' dimensions (1240x1752 = calibre page render) and
    a path sample for manual eyeballing.

Read-only: nothing is renamed or written. Run from the repo root:

	.venv/bin/python scripts/calibrate_text_cover.py [library_root]
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from book_meta_fix.covers import (
	_TEXT_MAX_COLOUR_FRAC,
	_TEXT_MIN_INK_BANDS,
	_TEXT_MIN_WHITE_FRAC,
	_text_page_stats,
)


def main() -> None:
	root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/mnt/share_nfs/Shared eBooks")
	hits: list[tuple[Path, int, int]] = []
	near: Counter[str] = Counter()
	total = 0
	for cover in sorted(root.rglob("cover.jpg")):
		if not cover.is_file():
			continue
		total += 1
		try:
			from PIL import Image

			with Image.open(cover) as img:
				w, h = img.size
				small = img.convert("RGB").resize((150, 200))
		except Exception:
			continue
		white, colour, bands = _text_page_stats(small)
		ok_white = white >= _TEXT_MIN_WHITE_FRAC
		ok_colour = colour <= _TEXT_MAX_COLOUR_FRAC
		ok_bands = bands >= _TEXT_MIN_INK_BANDS
		if ok_white and ok_colour and ok_bands:
			hits.append((cover, w, h))
		elif ok_white:
			# White-dominated but not flagged — the interesting near-misses:
			# real white covers live here; check which gate saved them.
			if not ok_colour:
				near["white_but_coloured"] += 1
			elif not ok_bands:
				near["white_but_few_bands"] += 1
				if bands >= 4:
					near[f"white_bands_{bands}"] += 1
	print(f"covers scanned: {total}")
	print(f"TEXT-PAGE FLAGGED: {len(hits)}")
	for p, w, h in hits[:60]:
		print(f"   {w}x{h}  {p}")
	print("\nnear misses (white-dominated, not flagged):", dict(near))
	print("flagged sizes:", Counter(f"{w}x{h}" for _, w, h in hits).most_common())


if __name__ == "__main__":
	main()
