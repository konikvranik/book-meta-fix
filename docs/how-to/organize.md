# Organize the library (placement)

**English** | [Čeština](../cs/how-to/organize.md)

Placement is part of `bmf apply` — the former `bmf organize` command (which
re-classified the whole library on every run, detectors + content reads for
the identity gate) is a deprecation stub.

```bash
bmf analyze                       # flags misplaced books (C13) with a
                                  # pre-filled action: accept
bmf gui                           # review if needed (verified checkbox, edits)
bmf apply                         # dry-run: shows the planned moves
bmf apply --apply                 # writes metadata AND places every book

bmf apply --apply --pattern "{author_sort}/{title} ({id})"
bmf apply --apply --needfix-dir "_problems"
bmf apply --apply --no-place      # metadata only, no moves
```

Routing is decided from the FINAL metadata (pure path math — no content
reads, that work belongs to analyze). The entry's decision outranks
residual detector complaints:

| book state after apply | destination |
|---|---|
| decided — accept or keep (regardless of leftover detector complaints) | the pattern path, default `{author}/{title} ({id})` |
| dead record — no ebook file at all (EMPTY_BOOK) | `<library>/<needfix-dir>/empty/<original relative path>` |

An accepted book therefore always moves back OUT of `needfix/` to its
pattern target — a leftover complaint that is real simply re-fires on the
next analyze and re-enters review; only the `verified` flag closes a book
for good. A dead record stays under `needfix/empty/` — without the book
file there is nothing to place.

Pattern fields: `{author}`, `{author_sort}`, `{title}`, `{title_sort}`,
`{id}`, `{isbn}`, `{year}`, `{language}`, `{series}`, `{series_index}`.
Set the defaults once via `BMF_PATTERN` / `BMF_NEEDFIX_DIR` in `.env`.

## Collision handling (duplicate-book merge)

When two books resolve to the same target path (common with an `{id}`-less
pattern, or duplicate `calibre_id`s), apply does not blindly append
` (dup N)`. It detects whether they are the **same book** and acts
accordingly:

- **Same book** (ISBN agrees, **or** title + author fuzzy-match and the year
  doesn't disagree) ⇒ **merged** into one folder: all format files combined,
  metadata field-merged (the approved book's record is the base; the
  occupant fills gaps, authors/tags unioned). The loser folder is removed.
- **Different books** at the same path ⇒ each is **disambiguated** rather
  than merged: by **year** (`Title (2026)/`) when their years differ,
  otherwise by **id** (`Title (id123)/`).
- ` (dup N)` survives only as a last-resort fallback.

Dry-run by default; merges and moves only run with `--apply`.
