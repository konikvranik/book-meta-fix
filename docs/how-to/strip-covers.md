# Strip generated covers

**English** | [Čeština](../cs/how-to/strip-covers.md)

```bash
bmf strip-covers                   # dry-run: list books with generated covers
bmf strip-covers --apply           # remove them (cover.jpg -> .bak, EPUB covers stripped)
```

Removes every cover the C11 pixel analysis classifies as auto-generated
(Calibre placeholder), per book:

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
