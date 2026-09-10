# Merging duplicate folders (`bmf merge`)

**English** | [Čeština](../cs/how-to/merge-duplicates.md)

```bash
bmf merge                        # dry-run: report duplicate clusters (nothing written)
bmf merge --apply                # write `action: merge` proposals (C19) into review.yaml
bmf gui                          # review the batch (filter by the merge state)
bmf apply                        # fold the approved duplicates into their survivors
bmf abs-rescan                   # the survivor folders changed on disk — push into ABS
```

## What is detected

The same work living in **two folders** — typically a double import: each
calibre import mints its own id, the default `{author}/{title} ({id})` pattern
puts each at its own path, and they never collide (so apply's own
collision-merge can never reach them). A cluster is:

- identical **folded first author + folded title** (diacritics, case and
  punctuation folded — *Babička* matches *babicka*), **or**
- the same **valid ISBN** on both sides (which also catches a duplicate whose
  title got corrupted in one folder).

Folders whose years **both exist and differ** are different editions — not
proposed, unless the ISBNs already match (a matching ISBN identifies the
same edition even when one record carries a wrong year). Dead records (no
book files) are skipped; EMPTY_BOOK owns them. There is deliberately no
fuzzy tier: a misspelled title is invisible to C19 (fix it via
`bmf normalize` first and the pair appears on the next run).

The **survivor** of each cluster is picked deterministically: a record with a
valid ISBN wins, otherwise the lowest calibre_id. Every other member gets a
review entry whose `proposed.merge_into` names the survivor's folder.

## The safety ladder

1. **Dry-run by default**: the first run only reports the clusters;
   `--apply` just fills review.yaml — no folder is touched by `merge`.
2. **Pre-fill is conservative**: only **ISBN-confirmed** duplicates (both
   sides carry the same valid ISBN) arrive pre-filled `action: merge`; an
   exact author+title match without ISBN proof stays pending for your
   decision.
3. **Decided entries are never touched**: re-running `bmf merge --apply`
   skips books you already decided about.
4. **Apply-time re-check**: `bmf apply` re-reads both folders fresh and
   re-runs `same_book` — if the metadata changed since the proposal, the
   merge is skipped and the entry stays in review (re-run `bmf merge`).
   A survivor with its own decided whole-folder `delete` entry is left
   alone.
5. **No file is ever overwritten**: same-name collisions inside the survivor
   rename with the loser's id (`Book (id123).epub`), identical bytes are
   skipped, the survivor's cover wins. Metadata is field-merged
   **survivor-first** (the duplicate only fills gaps).
6. **Snapshot**: the loser's `metadata.json`/`metadata.opf` + cover (the
   only things a merge destroys) ride the shared
   `deletion_snapshot_<stamp>.tar.gz`.

`analyze --merge` chains the same sweep onto the end of a run, over the
books that analyze already scanned — no second (NFS-slow) library walk.

## Audiobookshelf

Merging changes the survivor folders on disk (new files move in) and removes
the loser folders — run `bmf abs-rescan` afterwards so ABS re-scans the
touched survivors.
