# Edit + apply

**English** | [Čeština](../cs/how-to/edit-and-apply.md)

```bash
$EDITOR review.yaml               # set action: accept|delete|keep per entry
bmf apply review.yaml             # dry-run preview (incl. planned moves)
bmf apply --apply review.yaml     # write metadata + PLACE each applied book
```

Actions: `accept` (apply `proposed`), `delete` (remove the book folder,
tar.gz-backed), `keep` (apply `proposed` but retain the entry in
review.yaml — nothing more; it does not freeze the book).

`proposed` is the edit surface: adjust the values directly to override the
analyzer, and set a value to `null` to DELETE that field at apply time (a
wrong value with the correct one unknown). A decided entry's `proposed`
(including your adjustments) is carried verbatim through the next `analyze`;
undecided entries get a fresh proposal.

## The `verified` flag

An entry may also carry `verified: true` (the GUI checkbox / `Ctrl+O`;
orthogonal to the action, so "accept + verified" fixes AND closes the book
in one pass). Apply persists it into the book's `metadata.json` (never the
OPF mirror — Calibre never sees it); later `analyze` runs skip the book
entirely, and apply routes it to the target path even if some problems
remain unrecoverable. Analyze pre-fills it when its own proposal completes
the book (the projected post-apply state is detector-clean) — a fixed book
never re-enters review. Undo a too-hasty OK with
`bmf analyze --recheck-ok`.

## Placement (the former `bmf organize`)

After writing an entry's metadata, apply also decides where the folder
belongs — clean/`verified` books move to the pattern path, unresolved ones
to `needfix/`, dead records (no ebook file) to `needfix/empty/`. See
[Organize the library](organize.md) for the pattern fields and collision
handling. `--no-place` skips moving entirely; `--pattern` /
`--needfix-dir` (or `BMF_PATTERN` / `BMF_NEEDFIX_DIR`) override the
targets.

Every fetched field reaches the book: title/author(s), ISBN, year, publisher,
language, series + series index (stored as the ABS `[{"name", "index"}]` list,
mirrored to `metadata.opf` as `calibre:series`/`calibre:series_index`), genres
(and tags — both as OPF `<dc:subject>`), and the description/annotation from
databazeknih / Google Books / OpenLibrary. A proposal that carries only half
of the series keeps the current other half; a `null`/emptied series name
clears it. For C1 (author/title swapped) the analyzer proposes the swap
itself — accept it or adjust the values.

Old review.yaml files (an `edited:` block, `action: edit|reject|swap`) are
migrated on load: `edited` is merged over `proposed`, `edit` becomes
`accept`, and `reject`/`swap` reset to pending.

Instead of hand-editing the YAML you can use the [GUI editor](gui.md).
