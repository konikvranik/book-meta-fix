# Edit via the GUI (optional)

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
sérii, autoři, žánry (`Ctrl+A` selects all in a field). The list shows the label on the
left and the cover thumbnail flush right on every row. Requires the Tk
bindings
(`sudo apt install python3-tk` on Debian/Ubuntu). Edits are written back to
`review.yaml` — commit them with `bmf apply` as in [edit + apply](edit-and-apply.md).
