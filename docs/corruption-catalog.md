# Corruption Catalog (C1–C17)

**English** | [Čeština](cs/corruption-catalog.md)

Empirically derived from a ~5,440-book CZ/SK Calibre library. Each category
has real examples (calibre_id, author_folder, title) found during the initial
study.

## C1 — Author/title swapped

The book's *title* became the *author* folder; real author names became titles.
Often a series where the source listed `<author>=series, <title>=contributor`.

| id | author_folder | title | note |
|----|---|---|---|
| 95 | `NŘm Barik da` | `Jan Drda` | real author = "Jan Drda" |
| 111 | `Schindler v Seznam` | `Thomas Keneally` | Keneally wrote Schindler's Ark |
| 4357 | `uzivatelska prirucka 31D30588` | `Peugeot 406 - uzivatelská příručka` | swap |

**Verdict:** NEEDS_REVIEW (LLM or manual fix)
**Fix action:** `accept` — the analyzer proposes the swap itself into
`proposed` (title ← author, author ← title) when no better source is found;
adjust the values before accepting if needed

## C2 — Filename used as title (diacritics stripped)

Book was imported from a file; the filename became both folder and title.
Diacritics replaced with `_`. **Most common category (~47% of library).**

| id | title | formats | shape |
|----|---|---|---|
| 1753 | `Kirill_Bulicov-Druha_cesta_k_pr` | epub,pdb | `Autor-Titul` with `_` |
| 3342 | `Buskov_A-Rytirka_Natal_n_` | epub,pdb | ends `_n_` |
| 1416 | `Microsoft Word - 4444.doc` | epub,pdf | Word temp filename |
| 3774 | `Bradbury` | doc | title is just author surname |
| 2497 | `Cas prilivu` | epub | should be "Čas přílivu" |

**Verdict:** NEEDS_REVIEW (need content/online to know the correct title)
**Fix action:** `accept` (edit the `proposed` values if the proposal needs a fix)

## C3 — Series/library/publisher used as author

| id | author | title | note |
|----|---|---|---|
| 155 | `abeles` | `Dr. Oldrich Elias: Golem – Historicka studie` | library tag, not author |

**Verdict:** NEEDS_REVIEW

## C4 — Encoding corruption (mojibake)

Two forms observed:
1. **Octal-escape** — Python `repr()` of UTF-16 bytes leaked into JSON value:
   `\\376\\377\\000K\\000u\\000l\\000h\\000\\341\\000n\\000e\\000k` = "Kulhánek"
2. **Mis-decoded** — cp1250 bytes read as cp1251/cp1252/iso-8859-1, re-encoded
   as UTF-8: `Jiшн Kosek` (cyrillic), `¬as pý¡livu` (latin-1), `Kamenáè` (iso-1)

| id | symptom | original |
|----|---|---|
| 5687 | `\\376\\377\\000K...` | Kulhánek Jiří-Stroncium |
| 2184 | `'Darko\uffbd je bytost'` | Darkon doma a na cestách |
| 1795 | `1. ZAĚTEK VELIKÝ CESTY` | Svandrlik Příliš tlustý dobrodruh |

**Verdict:** NEEDS_REVIEW (LLM for unrepairable cases)
**Note:** many are auto-repaired by the encoding module; only the unrepairable
ones reach review.

## C5 — Placeholder record

Literal empty placeholder with title="title", author="author".

| id | author | title |
|----|---|---|
| 5575 | `author` | `title` |

**Verdict:** AUTO_FIXABLE (delete)

## C6 — MS-Word lock-file duplicate

`~$` is the Word temp-lock-file prefix; these are duplicates.

| id | author_folder |
|----|---|
| 3690 | `~$N. Shearer` |

**Verdict:** AUTO_FIXABLE (delete)

## C7 — Glued authors

Author tokens glued together without spaces around connectives.

| id | author | should be |
|----|---|---|
| 317 | `byKathy SierraandBert Bates` | Kathy Sierra, Bert Bates |

**Verdict:** NEEDS_REVIEW

## C8 — Translator mislabeled as author

Translators are encoded as a second `<dc:creator opf:role="aut">` — the `trl`
role is never used in this library. Only reliable signal: CZ-looking name
alongside a foreign-looking author.

| id | title | authors | likely translator |
|----|---|---|---|
| 393 | Měsíční prach | Jarmila Emmerová, Arthur C. Clarke | Emmerová |
| 499 | Hrobky Atuánu | Karel Soukup, Petr Kotrle, Ursula K. Le Guin | Soukup, Kotrle |
| 2144 | Smrt lorda Edgwarea | Marek Roesel, Agatha Christie | Roesel |

**Verdict:** NEEDS_REVIEW
**Caveat:** with 4+ authors and 2+ foreign names, it's more likely a real
anthology (C10) than a translator team.

## C9 — Anonym (mostly fake)

**PAST:** 99.7% of `Neznamy` records in this library are corrupted, not
genuinely anonymous. Only ~5 are real (Bible). Detection whitelists religious
titles; everything else with anonym spelling is flagged.

| spelling | count in library |
|---|---|
| `Neznamy` | 1844 |
| `Unknown` | 73 |
| `Neznámý` | 19 |

| id | author | title | real? |
|----|---|---|---|
| 5485 | anonym | Nová Bible kralická (Knihy Mojžíšovy) | YES (whitelisted) |
| 2235 | Neznamy | `0 DEV_T PRINC_ AMBERU` | NO (corrupted) |

**Verdict:** NEEDS_REVIEW (unless whitelisted → OK)

## C10 — Long multi-author list

4+ authors — could be a real anthology OR a translator team. Grain is unclear.

| id | title | n_authors |
|----|---|---|
| 197 | Soumrak světů | 13 (real CZ SF anthology) |
| 4411 | Kuchařka stařenky Oggové | 4 (Briggs, Pratchett, Kantůrek, Kidby) |

**Verdict:** NEEDS_REVIEW

## C13 — Location mismatch (folder ≠ metadata)

The book's folder does not match the pattern-derived target path
(`{author}/{title} ({id})` by default; see `--pattern` / `BMF_PATTERN`).
Not corruption of the metadata itself — the record is fine, the book merely
sits in the wrong place (a calibre rename that never moved the folder, a
stint under `needfix/` that is now resolved, ...).

Pure path math: no content reads, no online lookups. Evaluated only when
analyze runs with location checking on (default); `report`/`epubgen` stay
location-blind. The `proposed.location` value is informational — `bmf apply`
recomputes the destination from the FINAL metadata (you may fix
author/title in the same pass). When C13 is the only real problem (with at
most benign extras — OK-verdict, MISSING_*, or a cover diagnosis C11/
MISSING_COVER, which apply's cover recovery retries in the same pass), the
review entry is pre-filled `action: accept`, so a misplaced-but-healthy
book is moved in bulk; a proposal that changes title/author keeps the entry
for individual review. A book under `needfix/` whose problems were resolved
moves back out to the root tree the same way.

**Verdict:** AUTO_FIXABLE (move)

## C14 — Series order glued into the series name

The series name carries the book's order — e.g. `Mark Stone #73` as the
name with an EMPTY index. ABS/Calibre importers occasionally store the
whole "Name #N" string in the name field; the GUI then shows a broken
series, series search groups by the polluted name, and a `{series}`
placement pattern would embed the "#N" into the folder name. A library
survey found 353 such books (Asterion, Agent JFK, Mark Stone, …).

Only the explicit `#N` suffix is split — a trailing bare number
("MR-362 Espace 4") can be part of a real name and is deliberately left
alone. A stored index that DIFFERS from the embedded number is a
deliberate state and the rule does not fire (an equal index does — only
the name is wrong then).

The fix is deterministic and lossless: bare name + `series_index`
("Mark Stone" + 73). The review entry is pre-filled `action: accept` and,
when the split is the book's only problem, also `verified: true` —
analyze + apply repair the whole library in bulk. The GUI's `+ library`
mode additionally pre-fills the same proposal for books its search pulls
in from outside review.

**Verdict:** AUTO_FIXABLE (split)

## C15 — Author-name variants (library-level)

The same person spelled several ways across books: "Robert A. Heinlein" vs
"Robert Anson Heinlein" (initials vs full middle name), "Jiří Kulhánek" vs
"Jiri Kulhanek" (diacritics), NFD-decomposed duplicates, ALL-CAPS or
mojibake copies ("JiĹ™Ă Kosek"), academic titles ("Ing. Václav Semerád"),
the anonym family ("Neznamy"/"Neznámý"/"Unknown" — 5 spellings, ~107
books), and swapped name order ("James, Peter" in the Calibre
*Surname, Given* convention, or plain "Podroužek Přemysl").

A per-book detector cannot see this — the pattern only emerges ACROSS
books. `bmf normalize` (the only emitter of C15) clusters the spellings.
Deterministic merges are pre-filled `action: accept`: fold-equal forms,
initials vs full given names, titles, ALL-CAPS, the anonym family, and a
swapped order attested by the cluster (majority spelling wins). Judgement
calls stay pending for the human: letter-level variants
("Frederik"/"Frederick", "Miloslav"/"Miroslav" — same-letter homonyms can
be two people) and unevidenced comma reorders ("Weis, Hickman" may be two
authors joined by a comma). The canonical form is the most frequent
NON-degraded spelling — "Neznamy" ×63 loses to "Neznámý" ×24. Multi-author
strings ("Wilhelm a Jacob Grimmové") are skipped; splitting them is C7/C8
territory.

**Verdict:** AUTO_FIXABLE (whole-list replace via `proposed.authors`),
NEEDS_REVIEW for the judgement calls

## C16 — Genre/tag name variants (library-level)

Genre strings that differ only by case ("Sci-fi"/"sci-fi" — 1,235 books in
the survey), diacritics, word order ("Literatura česká"/"česká
literatura"), language ("Science Fiction", "Comedy", "Thriller"), or
spelling ("mumour"). Genres and tags are canonicalized against ONE shared
vocabulary — both serialize into OPF `dc:subject` and must not drift to
different casings of the same name.

`bmf normalize` unifies them onto Czech names by two deterministic
mechanisms only: fold groups (case/diacritics/word-order duplicates; the
representative spelling is the library's own most frequent form) and the
curated `GENRE_ALIASES` table in `normalize.py` (English→Czech,
singular/plural, observed misspellings — every merge is an explicit,
reviewable row). There is deliberately NO fuzzy auto-matching: on the real
library's 1,621 distinct names, distance-2 "typos" were ~50% false pairs
(Afrika→Amerika, vlaky→války, etika→erotika). The long tail
("Hugo (literární cena)") is left alone unless a table row covers it.

**Verdict:** AUTO_FIXABLE (whole-list replace via `proposed.genres` /
`proposed.tags`)

## C17 — Invalid ebook file (unrecoverable content)

An ebook file whose CONTENT is recognizable as no book format at all:
0 bytes, binary noise matching no signature (no ZIP / `%PDF-` /
`BOOKMOBI` / `Rar!` / PalmDB structure, not readable text), a readable
ZIP holding no book content (no `META-INF/container.xml`, no images, no
readable text members), or a truncated ZIP whose central directory is
gone.

Emitted ONLY by `bmf clean --files` (opt-in; never by `bmf analyze` —
file validity deliberately stays out of the auto-correction flow). The
probes are content-based, not extension-based: a valid book saved under a
wrong extension (an EPUB named `.pdf`) is recognized and never proposed —
the extension only decides whether *unrecognized* content may be called
invalid at all (`_FULLY_PROBED_SUFFIXES` in `filecheck.py`; formats with
variants bmf cannot positively identify, like `.prc`/`.pdb`, are never
flagged). Deliberate misses, safety over recall: a truncated PDF still
starts `%PDF-`, text junk (an HTML error page saved as `.epub`) still
reads as recoverable text. When calibre's `ebook-meta` reads a file
cleanly (exit 0 and empty stderr — measured: it exits 0 with a traceback
on garbage, falling back to the filename), the file is NOT invalid.

New entries arrive pre-filled `action: delete` with
`proposed.delete_files`; the GUI filters by the delete state and
Ctrl+Shift+D mass-clears the decision (Ctrl+Shift+R drops entries from
review). `bmf apply` deletes only the named FILES (the folder and
healthy sibling formats stay), re-checks every file immediately before
deletion (a file that became valid — or disappeared — is skipped), and
snapshots everything into `deletion_snapshot_*.tar.gz`. Deleting the only
book file cascades: the next run reports EMPTY_BOOK and routes the folder
to `needfix/empty/`.

**Verdict:** NEEDS_REVIEW (delete proposal; review-gated, never auto-applied)

## EMPTY_BOOK — Dead record (the book file is gone)

The folder holds only metadata sidecars, their backups and/or a cover —
no ebook file and no subdirectory. The book itself was lost (a failed
calibre import, a deleted file); only the record survived. There is nothing
to extract or verify, so the record can never be confirmed against content.

The rule runs FIRST: with the book file gone, no other rule's opinion about
the metadata matters. `bmf apply` moves the folder to `needfix/empty/`
(preserving the relative path); the metadata is left untouched for a
possible later manual recovery. The entry is pre-filled `action: accept` —
the move is mechanical and reversible. A folder containing any other file
(an unrecognized format, a stray document) or a subdirectory is NOT empty.

**Verdict:** AUTO_FIXABLE (quarantine to `needfix/empty/`)

## MISSING_ISBN / MISSING_YEAR

Not corruption — just missing data that can be filled by online lookup
(databazeknih.cz for CZ/SK, plus Google Books / OpenLibrary as fallbacks).

**Verdict:** AUTO_FIXABLE (enrich)

## Detector calibration notes

- **C2 alone is holočný** — 47% of titles contain `_`. Requires a stronger
  signal (file extension in title, Word temp prefix, exact filename match, or
  3+ underscores) to fire.
- **C2 has priority over C1** — polluted titles produce false swap signals
  (the filename contains both author and title).
- **C9 default is NEEDS_REVIEW**, not OK — the whitelist is the only way to
  get OK for an anonym record.
