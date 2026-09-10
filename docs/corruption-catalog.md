# Corruption Catalog (C1–C18)

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

During `analyze` the rule is also **pool-armed**: `run_pipeline` builds a
known-author pool from the whole library (the same clustering `bmf normalize`
uses), so a record whose TITLE string resolves to a known library author
fires C1 with HIGH confidence — either as a *variant pair* (title and author
are the same person in two spellings, e.g. title `Anatolij Dněprov` /
author `A. Dněprov` — the real title is lost from the record) or a *classic
swap* (the author field holds the real title; when it is itself another
known author the reason flags the ambiguity — biography territory). The
repair tier (`_try_known_author_swap`) then takes the author from the pool
canonical and the title from the book's OWN text, bound by `confirm_identity`;
a classic swap is only trusted when the content-mined title agrees with the
author field (a biography titled with its subject would survive a naive
swap self-test). Every failure stays for review with the raw-swap hint.

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

## C8 — Translator in the author's place

**POLICY (2026-09):** a translator sitting AMONG the comma-separated
authors is an ACCEPTED state, not corruption. Audiobookshelf models only
authors and narrators — there is no translator field — so the authors
entry is what keeps a book findable by its translator. The old signal
("CZ-looking name next to a foreign author → propose stripping the CZ
names") flagged every mixed list in the library (47 books, all with the
foreign author correctly first) and its proposal was dead weight: a
separate `translators` field has no model/writer support, so an applied
fix would have silently deleted the names.

What metadata alone can still PROVE — an author entry carrying an explicit
translator label (`přeložil František Jungwirth`, `Překlad: J. Novák`,
`translated by X`): strip the label, keep the bare name in the list.
Currently zero occurrences in the library; kept as a cheap,
false-positive-free guard for future Calibre re-imports.

NOT detected, deliberately: a translator-first ordering or a lone
translator with the real author lost. Name-order heuristics cannot tell
a leading translator from a Czech adaptor/editor legitimately first
(measured false positives: "Kate Wilhelmová" — an American author with a
feminized Czech surname; "Josef V. Pleva, Daniel Defoe" — a Czech
adaptor first by convention), and 0 of the library's books are in the
translator-first shape anyway. Those corruptions belong to the content
flows: the identity verifier catches a primary author the book's own
text contradicts, `text_meta` mines "přeložil X" credits from page text.

**Verdict:** NEEDS_REVIEW

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

## C10 — Long multi-author list *(retired 2026-09)*

**PAST:** 4+ authors — "verify anthology vs translator list", NEEDS_REVIEW.
Both hypotheses are accepted states under the C8 policy above (real
co-authors of an anthology, or translators riding along for searchability),
so there is nothing left for a human to verify. Measured on the library:
all 12 flagged entries were genuine multi-author works, mostly already
accepted by hand. No new C10 diagnoses are produced; the code stays in the
display order so historical review entries still render.

| id | title | n_authors |
|----|---|---|
| 197 | Soumrak světů | 13 (real CZ SF anthology) |
| 4411 | Kuchařka stařenky Oggové | 4 (Briggs, Pratchett, Kantůrek, Kidby) |

**Verdict:** — (not flagged anymore)

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

## C18 — Series-name variants (library-level)

The same series spelled several ways across books: "Zaklínač" /
"zaklínač" / "Zaklinac" (case/diacritics), "Perry Rodan" / "Perry Rhodan"
(real-word rename), or a suffixed sibling "Mark Stone" / "Mark Stone
(edice)". Like C15/C16, invisible to any per-book detector — the
inconsistency is a property of the whole library. The series ORDER is
never proposed, only the name (`_apply_fields` keeps each book's own
index half of the pair).

`bmf normalize --series` clusters the names with three evidence tiers:

1. **Fold (deterministic, pre-filled accept):** NFC + casefold +
   diacritics out + punctuation to spaces, whitespace collapsed. Word
   order is PRESERVED (unlike genres: "Legenda o Drizztovi" ≠ "Drizztova
   legenda" — close sub-series must not auto-merge). Canonical = the
   group's most frequent original spelling. A trailing "#N" glued into
   the name is stripped from the fold key, but a dict-glued name is
   otherwise SKIPPED — C14's split already owns those books.
2. **Suspects (pending):** token-prefix pairs ("Mark Stone" /
   "Mark Stone (edice)") and names fuzzy-close to a VERIFIED series.
   Evidence weighs volume NUMBERING — the two names' volume sets that
   interleave into one contiguous row (1,2,4 + 3 ⇒ merge) versus a
   collision (both claim volume 1 ⇒ two series or duplicate books, never
   merged) — and, with `--online`, the names' existence in the
   bibliographic DB (base exists and the suspect does not ⇒ merge; both
   exist ⇒ distinct). Online answers go through the enricher's
   persistent cache, so repeat runs cost nothing.
3. **Alias table (`SERIES_ALIASES` in `normalize.py`, pending):**
   hand-written rows for renames neither tier can see. Unlike
   `GENRE_ALIASES` (HIGH), a row lands as PENDING — it retitles a whole
   group at once and the one-time GUI confirm is the safety net against
   a typo in the row itself.

Books listing MULTIPLE series are skipped and reported (applying a
single-name proposal would drop the other series — edit those in
`bmf gui`). There is deliberately no free fuzzy tier, same trade as C16.

`bmf series` is the read-only companion: every series with book counts,
volume coverage ("1–3, 5 (missing: 4)"), duplicate-volume warnings and
the suspect pairs with their verdicts. Missing volumes are INFORMATION
(a personal library need not be complete); a volume number claimed by
two books is a WARNING (duplicate folder or a wrong index).

**Verdict:** AUTO_FIXABLE (fold) / NEEDS_REVIEW (alias rows, evidenced
suspect merges)

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
