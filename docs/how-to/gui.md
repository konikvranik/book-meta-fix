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
per-format content view that loads the **whole book text, progressively** —
chunks appear as they are extracted, so a slow NFS read or a converter
render shows its head immediately instead of a bare "loading…" (extremely
large books are capped at 2,000,000 characters, with a visible truncation
note) — with double-encoding
repair (`Ctrl+G`; the codec boxes let you experiment
manually — "přečteno jako" is the wrong codec the text was once read
through, "skutečně je" the real one, and `⇄` swaps them; a failing pair is
explained in the hint, which offers the reversed direction as a click;
bytes lost to an earlier replace-decode (`�`) don't block the repair and
stay marked; two-layer chains are repaired automatically and named in the
hint; always rendered as UTF-8 — the toggle is never auto-checked, seeing
the repaired text is your decision: tick it or press `Ctrl+G`). The preview is
placed responsively: while the detail pane is wide it sits to the right of
the form, and when the pane gets narrow (a small window, or the book list
widened with its sash) it moves below the form; the pane's sash resizes the
preview along its axis, and in that stacked layout one big scrollbar on the
detail's right edge scrolls the whole flow — the form first, then the whole
book text (the mouse wheel chains the same way; the per-widget scrollbars
return in the side-by-side layout). The
**Found problems** section above the fields lists every diagnosis
(severity-sorted): the lines are mouse-selectable and copyable (`Ctrl+C`,
or right-click for *copy selection / copy all*), and each underlined
**code** (`C1`…`C20`, `MISSING_*`, …) is clickable — it opens a small
window with the detailed description of that corruption type (from
[the catalog](../corruption-catalog.md)); the popup's text is selectable
and copyable the same way, `Esc` closes it. The
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

## Bulk actions (accept / verified / covers)

The same multi-selection drives three more bulk commands — the `Shift`
variant of a single-book shortcut means "do it to all selected":

- **`Ctrl+Shift+A`** accepts every selected book. Unlike the bulk field
  edit (which only decides pending entries), an explicit selection
  **overrides** any previous decision.
- **`Ctrl+Shift+O`** toggles the verified mark on all selected books —
  and clears it when every selected book already carries it.
- **`Ctrl+Shift+M`** deletes their covers. The dialog mirrors the per-book
  checkboxes: `cover.jpg` (default on), its `.bak`, and the covers
  embedded in the EPUB files (the strip rewrites the e-books, so it asks
  once with the file list first). A proposed `cover_url` is dropped from
  the touched entries — apply re-downloads a proposed cover for a
  C11/MISSING_COVER book, so keeping the URL would undo the deletion on
  the next run. `Ctrl+S` writes the entry changes as usual.

## Merge selected books (`Ctrl+J`)

When one work lives in several folders (a duplicate, a split import),
select them and press `Ctrl+J`. The dialog picks the **survivor**
(default: the focused row — the detail pane shows it) and warns when
`same_book` does not consider the selection the same work (a hint, not a
block — here you decide, unlike the automatic placement merge).

Below the survivor radios sits a **field-by-field grid**: one row per
metadata field (title, authors, ISBN, year, publisher, language, series,
genres, description), one column per selected book, one radio per cell.
Each cell shows that book's effective value — a decided proposal
(`accept`/`keep`) counts as the book's value, since that is what `apply`
would write; undecided and library books show the stored value. Pick which
side the merged book keeps; `∅` keeps the field empty. Defaults follow the
survivor, and a field the survivor lacks takes the first value found — so
confirming the dialog untouched reproduces the automatic gap-filling merge
and loses nothing.

On confirm, every other book's files move into the survivor's folder
(collisions rename with the loser's calibre id), the picked values are
written to the merged book's metadata, and the losers' folders are removed
and their review entries dropped. A picked value also REBASES a
conflicting proposal key on the survivor (a stale analyzer suggestion must
not undo an explicit pick at the next apply); fields without a proposal
stay clean. review.yaml is saved **right after** the merge — the losers'
folders are gone, and the file must agree with the disk (a stale entry
would fail the next apply with "folder not found"). The survivor keeps its
review decision; `bmf apply` finishes it later as usual.

## Split a wrongly merged folder (`Ctrl+Shift+J`)

The undo of the merge: two UNRELATED books sometimes end up sharing one
folder — a `Ctrl+J` or an automatic merge (C19, a placement collision)
that should not have happened. Focus the entry and press `Ctrl+Shift+J`
(the folder must hold at least two ebook files). The dialog lists every
ebook file with its embedded title/author (loaded in the background —
extraction can take a moment); check the files that belong to the OTHER
work and they move out into a **new book**.

The new book's title and author prefill from the first checked file's
embedded metadata (text-mined when the format carries none) — editable,
and required. The target folder follows the same path pattern apply uses
(previewed live as you type); an occupied name gets the `(dup N)` suffix.
On confirm the new folder is created, the checked files move, a fresh uuid
is minted, both metadata sidecars are written, and the cover is recovered
best-effort from the moved file (generated placeholders are rejected —
otherwise MISSING_COVER re-fires and the enrichers fetch a real one). A
`same_book` warning flags the probably-pointless split (two copies of ONE
book) without blocking it — here you decide, like in the merge dialog.

The source entry keeps its folder, identity and review decision — only its
file set shrinks. The new book joins the list as a library-served entry
(`+ library` semantics): editable right away, written into review.yaml
only once you decide or edit it. review.yaml is saved right after the
split, and both folders' cache rows are invalidated.

## Verified (the OK mark)

Next to the action radios sits a **Verified** checkbox (`Ctrl+O` toggles
it; verified rows show a blue ✓ under the action glyph, and the action
filter gains a `verified` value). It marks the book OK for good: apply
stores `verified: true` in the book's `metadata.json`, later analyze runs
skip the book entirely, and apply places it on the target path even if
some problems remain. Analyze pre-fills it when its own proposal completes
the book. The read-only "Target folder" row under the fields previews the
C13 move proposal (`proposed.location`), if any.
