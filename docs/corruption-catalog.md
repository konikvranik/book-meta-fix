# Corruption Catalog (C1–C10)

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
