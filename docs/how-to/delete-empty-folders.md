# Delete empty book folders (`bmf clean --empty`)

**English** | [Čeština](../cs/how-to/delete-empty-folders.md)

```bash
bmf clean --empty                        # dry-run: list dead records (nothing written)
bmf clean --empty --apply                # write `action: delete` proposals into review.yaml
bmf gui                                  # filter the list by the delete state, review the batch
bmf apply --apply                        # delete the approved folders (tar.gz snapshot)
```

## What is detected

A folder qualifies as a dead record (EMPTY_BOOK) when it holds ONLY
metadata sidecars — `metadata.json`, `metadata.opf`, `cover.jpg` and their
`.bak`/`.tmp` backups. No ebook file, no subdirectory, no other file: a
stray document or an unrecognized format disqualifies the folder (some
other rule sees it instead).

Detection is location-blind: dead records are found wherever they sit in
the tree — including `needfix/empty/` (parked there by an earlier apply)
and any folder an older placement routed into `needfix/`.

## Default: quarantine, not deletion

By default bmf never deletes a dead record. `bmf analyze` flags it
EMPTY_BOOK with a pre-filled `action: accept` and `bmf apply` parks the
folder under `needfix/empty/` (relative path preserved, metadata left
untouched for a possible later manual recovery). `clean --empty` is the
opt-in path to propose deletion instead — see
[corruption-catalog.md](../corruption-catalog.md#empty_book--dead-record-the-book-file-is-gone)
for the full rule description.

## The safety ladder

1. **Opt-in**: `--empty` is off by default; plain `bmf clean` never
   proposes.
2. **Dry-run by default**: the first run only reports; `--apply` just
   writes review.yaml proposals — no deletion happens in `clean`.
3. **Review-gated**: fresh entries arrive pre-filled `action: delete`; in
   the GUI, filter the list by the *delete* state (or by category
   EMPTY_BOOK) to see them as one batch, then
   - **Ctrl+Shift+D** on a selection mass-clears the decision back to
     pending (the veto for folders that should survive),
   - **Ctrl+Shift+R** removes selected books from review.yaml entirely.
4. **Re-check**: when `--apply` writes the proposals, the empty-folder
   fact is re-verified per book — a folder that gained a real file since
   the dry-run scan is left alone.
5. **Snapshot**: everything `bmf apply` deletes lands in
   `deletion_snapshot_<stamp>.tar.gz` next to the library.

## Ordering: run AFTER analyze

`bmf analyze` rebuilds a still-pending EMPTY_BOOK entry from scratch —
back to the pre-filled `accept`. Decide the order accordingly:

```bash
bmf clean --empty --apply    # write the delete proposals
bmf gui                      # review + veto (do NOT run analyze in between)
bmf apply --apply            # delete
```

A DECIDED entry survives a later analyze untouched — including the accept
analyze pre-fills for EMPTY_BOOK (those are skipped, never clobbered). If
you change your mind about a decided one, clear its decision in the GUI
(**Ctrl+Shift+D**) and re-run `clean --empty --apply`.

## Audiobookshelf

After `bmf apply` removed folders, run `bmf abs-rescan` so ABS notices the
missing items.
