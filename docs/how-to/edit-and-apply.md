# Edit + apply

```bash
$EDITOR review.yaml               # set action: accept|reject|swap|edit|keep per entry
bmf apply review.yaml             # dry-run preview
bmf apply --apply review.yaml     # actually write metadata.json + metadata.opf
```

Actions: `accept` (apply proposed), `reject` (leave), `swap` (author↔title for
C1), `edit` (apply only the fields under `edited:`), `keep` (apply proposed
but retain the entry — `analyze` skips it next time; set back to `pending` to
re-decide).

Every fetched field reaches the book: title/author(s), ISBN, year, publisher,
language, series + series index (stored as the ABS `[{"name", "index"}]` list,
mirrored to `metadata.opf` as `calibre:series`/`calibre:series_index`), genres
(and tags — both as OPF `<dc:subject>`), and the description/annotation from
databazeknih / Google Books / OpenLibrary. A proposal that carries only half
of the series keeps the current other half; `action: edit` with an emptied
series name clears it.

Instead of hand-editing the YAML you can use the [GUI editor](gui.md).
