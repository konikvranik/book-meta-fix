# Invalid ebook files (`bmf clean --files`)

**English** | [Čeština](../cs/how-to/invalid-files.md)

```bash
bmf clean --files                        # dry-run: report unrecoverable files (nothing written)
bmf clean --files --apply                # write `action: delete` proposals (C17) into review.yaml
bmf gui                                  # filter the list by the delete state, review the batch
bmf apply                                # delete the approved files (re-checked, tar.gz snapshot)
```

## What is detected

A file is proposed for deletion only when its **content** is recognizable
as no book format at all: 0 bytes, binary noise matching no signature
(ZIP / `%PDF-` / `BOOKMOBI` / `Rar!` / PalmDB structure / readable text),
a readable ZIP holding no book content, or a truncated ZIP whose central
directory is gone.

The detection is deliberately **content-based, not extension-based**: a
valid book saved under a wrong extension (an EPUB named `.pdf`) is
recognized by its content and never proposed — at most it shows up in the
informational *wrong-extension notes* (no action attached). Formats whose
valid variants bmf cannot positively identify (`.prc`, `.pdb`, `.lit`, …)
are never flagged either. And when calibre's `ebook-meta` reads a file
cleanly, the file is not invalid regardless of what the probes say.

Misses are accepted on purpose (safety over recall): a truncated PDF
still starts with `%PDF-`, text junk (an HTML error page saved as
`.epub`) still reads as recoverable text — nothing that could be salvaged
is proposed.

## The safety ladder

1. **Opt-in**: `--files` is off by default; plain `bmf clean` never probes.
2. **Dry-run by default**: the first run only reports; `--apply` just
   writes review.yaml proposals — no deletion happens in `clean`.
3. **Review-gated**: fresh entries arrive pre-filled `action: delete`
   (with `proposed.delete_files`); in the GUI, filter the list by the
   *delete* state to see them as one batch, then
   - **Ctrl+Shift+D** on a selection mass-clears the decision back to
     pending (the veto for books that should survive),
   - **Ctrl+Shift+R** removes selected books from review.yaml entirely.
4. **Apply-time re-check**: `bmf apply` re-probes every file right before
   deleting it — a file that has become valid (or disappeared) since the
   proposal is skipped and recorded. Only the named FILES go; the folder
   and healthy sibling formats stay.
5. **Snapshot**: everything actually deleted lands in
   `deletion_snapshot_<stamp>.tar.gz` next to the library.

Deleting the only book file of a folder cascades: the next run reports
EMPTY_BOOK and routes the folder to `needfix/empty/`.

## Audiobookshelf

After `bmf apply` deleted files, run `bmf abs-rescan` so ABS re-scans the
touched folders (the per-item scan reflects the changed file set).
