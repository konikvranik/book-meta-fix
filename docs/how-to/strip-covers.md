# Strip generated / invalid covers

**English** | [Čeština](../cs/how-to/strip-covers.md)

```bash
bmf strip-covers                                # dry-run: generated covers, both scopes (the original behaviour)
bmf strip-covers --apply                        # remove them (cover.jpg -> .bak, EPUB covers stripped)
bmf strip-covers --invalid                      # dry-run: INVALID covers, both scopes
bmf strip-covers --invalid external --apply     # remove invalid sidecar files only
bmf strip-covers --generated embedded --invalid both --apply   # both selectors, chosen scopes
```

Two selectors, each an optional-scope flag. Passing a flag bare means
`both`; each also accepts a value: `external` (loose files in the book
folder) or `embedded` (covers inside EPUBs). Without either flag the
command does what it always did — generated covers, both scopes.

## `--generated` — auto-generated placeholders (C11 pixel analysis)

- **`cover.jpg` sidecar** — renamed to `cover.jpg.bak` (reversible; overwrites
  an existing `.bak`), never hard-deleted.
- **Embedded EPUB cover** — probed via the OPF wiring and, when generated,
  stripped surgically (zip + OPF rewrite; the e-book file itself is kept).
- **Non-EPUB formats (MOBI/AZW3/PRC, PDF)** — deliberately untouched: their
  covers live in binary EXTH headers with no safe removal path.

After a write run the next `bmf analyze` sees `MISSING_COVER` (no `cover.jpg`)
and refetches a real cover — from an enricher URL or by extracting a genuine
embedded cover from the book file. That is the intended follow-up workflow:
strip the placeholders, let the pipeline recover the real ones.

## `--invalid` — files that are not readable images at all

These are the covers behind Audiobookshelf's `[FfmpegHelpers] Resize Image
Error … Invalid data found when processing input` log lines: ABS picks an
item cover by file EXTENSION only — it prefers `cover.*`, else takes the
first `png/jpg/jpeg/webp` in the folder — so a non-image file with an image
extension (an HTML error page saved as `.jpg`, a truncated download) or a
`cover.*` name (`cover.html`) becomes the cover, and ffmpeg then chokes on
it.

- **External**: every file in the folder with an image extension
  (`jpg/jpeg/png/webp` — the set ABS classifies as images) or named
  `cover.*` that no image decoder can read, is renamed to `<name>.bak`
  (same reversible convention). Valid images — including generated ones,
  which the C11 pass handles — are left to their own selector.
- **Embedded**: an EPUB whose OPF-wired cover bytes do not decode as an
  image is stripped with the same zip + OPF surgery as generated covers.

One ABS caveat this command deliberately does NOT touch: when ffmpeg is fed
`metadata.json` (as in the log above), that folder is a dead record
(EMPTY_BOOK — the e-book file is gone) and `metadata.json` is the manifest
bmf must keep; the stale cover path lives in ABS's own database and clears
on ABS's side (a full library re-scan in ABS), not by deleting files.
