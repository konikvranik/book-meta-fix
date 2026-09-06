# Edit via the GUI (optional)

**English** | [Čeština](../cs/how-to/gui.md)

Instead of hand-editing the YAML, use the keyboard-driven editor:

```bash
bmf gui --review review.yaml
```

It shows read-only current fields next to editable targets, a one-key
author↔title swap, the book's folder as a clickable "open in file manager"
link (double-clicking a list row does the same), cover previews (current /
`.bak` / recommended, plus the
cover embedded in each format file — `Ctrl+M` removes the checked embedded
covers out of the e-book files, which stay put; EPUB only), and a
per-format content view with
double-encoding repair (`Ctrl+G`; the codec boxes let you experiment
manually — "přečteno jako" is the wrong codec the text was once read
through, "skutečně je" the real one, and `⇄` swaps them; a failing pair is
explained in the hint, which offers the reversed direction as a click;
bytes lost to an earlier replace-decode (`�`) don't block the repair and
stay marked; two-layer chains are repaired automatically and named in the
hint; always rendered as UTF-8 — the toggle is never auto-checked, seeing
the repaired text is your decision: tick it or press `Ctrl+G`). Drag the
grip under the content preview to resize it vertically (double-click
resets). The
detail column scrolls; every action has a `Ctrl+letter` shortcut (`F1` lists
them); `PgUp`/`PgDn` move between books and `Tab` cycles only the editable
fields — author, title, ISBN, year, publisher, language, série, pořadí v
sérii, autoři, žánry (`Ctrl+A` selects all in a field). The list shows the
label on the left, the series + order at the right end of the author's
line (the current values; an `accept`/`keep` entry shows the proposed
author and series, i.e. the ones `apply` will write), and the cover
thumbnail flush
right on every row. Rows sort series-first: the books of one series form
a single block in reading order (the order compares numerically, so #2
comes before #10, and a glued `Mark Stone #73` counts as 73), the blocks
follow their series names, and books without a series come after them,
by author then title. Requires the Tk
bindings
(`sudo apt install python3-tk` on Debian/Ubuntu). Edits are written back to
`review.yaml` — commit them with `bmf apply` as in [edit + apply](edit-and-apply.md).

## Whole-library search (`+ library`)

The `Search:` box normally filters review entries only. Tick **`+ library`**
next to it and the same query also sweeps the whole library: matching books
that are **not** in review.yaml join the list (their header says "not in
review.yaml"; they sort into the same series-first order as everything
else, so the whole run lines up in one block). This is the workflow for a
series you
know is broken — you spot bad metadata for the "Mark Stone" books in
Audiobookshelf, search the series, tick `+ library`, and every Mark Stone
book shows up, in review or not, ready to edit.

A fresh book behaves exactly like a review entry (fields, covers, content
view, actions). The one difference: `Ctrl+S` writes it into review.yaml
only once you **decide or edit** it (an action, the verified mark, a
proposal that differs from the current values) — untouched books never
flood the file. The additions stay in the list for the rest of the
session: editing a book, saving or unticking the box does not remove
them, and re-searching is idempotent (a book is never listed twice).
`bmf apply` then processes the saved ones like any other entry.

A book whose series order is glued into the series name (`Mark Stone
#73`) arrives with the C14 split pre-filled in the target fields (bare
series + order) — the same proposal `bmf analyze` would emit — so the
fix is one `Ctrl+Enter` per book instead of retyping. The untouched
pre-fill alone does not write the book into review.yaml; the mass fix
is analyze's pre-filled accept.

A fulltext index of the library is built by a background sweep the
moment the editor opens (progress in the status line; well under a
minute for ~5k books on NFS, folder reads parallelized — the same sweep
feeds the author/series autocomplete). Every `+ library` search is then
an instant in-memory filter, no waiting; a query typed before the index
is ready is answered automatically the moment it lands. The index
matches the same fields as the search box — author, title, series, the
folder path — plus the manifest-only fields review entries never carry
(annotation, publisher, tags), so a book with broken metadata still
matches when only its folder name or its annotation mentions the series
(typical for house-pseudonym series like Mark Stone, where most books
live under the real writers' folders). A book that has no uuid yet gets
one minted during indexing (the same lazy identity augmentation a scan
performs), because the review workflow is uuid-keyed.

## Bulk edit (author / series)

The list supports a multi-selection: `Ctrl+click` toggles a row,
`Shift+click` selects a range (the focus row keeps the stronger highlight;
rows hidden by a filter leave the selection). `Ctrl+E` — or the
**Bulk edit** button under the list — opens a small dialog that sets one
field, author or series (with the same autocomplete as the field itself),
for every selected book at once; `∅` applies the field as EMPTY instead
(deletes it — the same mark the per-field `∅` button sets). The value
lands in each book's `proposed` block; a pending book becomes `accept`
(a proposal without a decision would be skipped by `bmf apply`), an
already decided book keeps its action. The series ORDER stays per-book —
only the name is set. `Ctrl+S` writes the results into review.yaml as
usual. This is the natural companion to the `+ library` search: find
every book of the broken series, select them, fix the series name in one
stroke.

## Verified (the OK mark)

Next to the action radios sits a **Verified** checkbox (`Ctrl+O` toggles
it; verified rows show a blue ✓ under the action glyph, and the action
filter gains a `verified` value). It marks the book OK for good: apply
stores `verified: true` in the book's `metadata.json`, later analyze runs
skip the book entirely, and apply places it on the target path even if
some problems remain. Analyze pre-fills it when its own proposal completes
the book. The read-only "Target folder" row under the fields previews the
C13 move proposal (`proposed.location`), if any.
