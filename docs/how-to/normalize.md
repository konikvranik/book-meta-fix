# Normalize author spellings, genre names and series names

**English** | [Čeština](../cs/how-to/normalize.md)

```bash
bmf series                       # read-only overview: all series, volume
                                 # coverage, variants, suspect pairs
bmf normalize                    # dry-run: show the author/genre/series clusters
bmf normalize --apply            # fill review.yaml with C15/C16/C18 proposals
bmf normalize --series --apply   # series names only
bmf normalize --series --online --apply   # + weigh suspects against databazeknih
bmf normalize --genres --apply   # genres+tags only (skips authors)
bmf normalize --authors          # authors only
bmf analyze --normalize          # analyze, then the normalize pass over the
                                 # same scan (no second library walk)
bmf gui                          # review; bulk-accept with Ctrl+Shift+A
bmf apply --apply review.yaml    # write metadata + move folders
bmf abs-rescan --apply           # push the changes into Audiobookshelf
```

Three messes are invisible to the per-book detectors because they only show
up ACROSS books: the same person spelled several ways (C15), genre names
that differ only by case, word order, language or spelling (C16), and the
same series spelled several ways (C18). `bmf normalize` scans the whole
library once, clusters the variants and turns them into ordinary
review.yaml proposals — `proposed.authors` / `proposed.genres` /
`proposed.tags` whole-list replacements and `proposed.series` (the NAME
only — each book keeps its own series index) that `bmf apply` writes like
any other fix.

## What gets merged automatically (pre-filled `accept`)

- **Author variants, deterministic:** the same name modulo case,
  diacritics ("Jiří"/"Jiri"), NFD decomposition, dotted-initial spacing
  ("G. J."/"G.J."/"G.-J."), initials vs full middle names ("Robert A." /
  "Robert Anson"), academic titles ("Ing. Václav Semerád"), ALL-CAPS
  copies, the anonym family ("Neznamy"/"Neznámý"/"Unknown"/"Anonym" →
  "Neznámý"), and swapped name order when the cluster attests the person
  ("James, Peter" → "Peter James" because "Peter James" exists elsewhere;
  "Podroužek Přemysl" loses to the majority "Přemysl Podroužek").
- **Genre/tag variants:** case/diacritics/word-order duplicates
  ("Sci-fi"/"sci-fi", "Literatura česká"/"česká literatura") and the
  curated `GENRE_ALIASES` table in `src/book_meta_fix/normalize.py`
  (English→Czech: "Science Fiction"→"Sci-fi", "Comedy"→"Humor";
  singular/plural: "román"→"Romány"; observed misspellings: "mumour").
  The representative spelling is always the library's own most frequent
  form, and genres and tags share one vocabulary (both become OPF
  `dc:subject`).
- **Series fold variants:** "Zaklínač"/"zaklínač"/"Zaklinac" — the same
  letters after NFC + casefold + diacritics-out + punctuation-to-spaces.
  The representative is the group's most frequent original spelling.
  Word order is NOT folded ("Legenda o Drizztovi" ≠ "Drizztova legenda" —
  close sub-series stay apart) and the series INDEX is never part of the
  proposal: only the name changes, each book keeps its own volume number.

## What stays pending (your judgement)

- **Letter-level author variants** — "Frederik"/"Frederick" Pohl,
  "Miloslav"/"Miroslav" Švandrlík, mojibake copies ("JiĹ™Ă Kosek").
  Same-letter name pairs can be two different people, so these arrive
  with no pre-filled action.
- **Unevidenced comma reorders** — an isolated "Weis, Hickman" may be two
  authors joined by a comma rather than one swapped name.
- **Series alias rows and suspected series merges** — a row in
  `SERIES_ALIASES` retitles a whole group at once, and a prefix/fuzzy
  suspect ("Mark Stone" / "Mark Stone (edice)") is a judgement call, so
  both arrive with no pre-filled action. A suspect becomes a proposal
  only with positive evidence: its volume numbers INTERLEAVE with the
  base's into one contiguous row (1,2,4 + 3 ⇒ merge), or — with
  `--online` — the base series exists in the bibliographic DB while the
  suspect does not. A numbering COLLISION (both claim volume 1) or both
  existing online means two distinct series: the pair is listed in the
  advisory table and never merged.

Different first-name initials never merge (Karel vs Josef Čapek, Dan vs
Eric Brown stay apart). Multi-author strings ("Wilhelm a Jacob Grimmové")
are skipped and listed separately — splitting them is C7/C8 territory.
Likewise, books listing multiple series are skipped and listed (applying
a single-name proposal would drop the other series — edit those in
`bmf gui`), and a series order glued into the NAME ("Mark Stone #73") is
left to C14's split, which pre-fills accept on its own.

## Series overview (`bmf series`)

`bmf series` renders the read-only inventory: every series with its book
count, volume coverage ("1–3, 5 (missing: 4)" — missing volumes are
INFORMATION, a personal library need not be complete), duplicate volume
numbers (⚠ two books claiming the same slot — a duplicate folder or a
wrong index) and the suspect pairs with their verdicts. It writes
nothing and needs no network unless you pass `--online`.

## Adding a genre or series merge

There is deliberately NO fuzzy auto-matching for genres (and series):
on the real library's 1,621 distinct genre names, distance-2 "typos"
were ~50% false pairs
(Afrika→Amerika, vlaky→války, etika→erotika). To merge two spellings,
add a row to `GENRE_ALIASES` and re-run — every merge is an explicit,
reviewable line. Series real-word renames work the same way through
`SERIES_ALIASES`:

```python
SERIES_ALIASES: dict[str, str] = {
	"Perry Rodan": "Perry Rhodan",          # typo, fold cannot see it
	"Mark Stone (edice)": "Mark Stone",     # edition suffix
}
```

The target resolves through the library's own fold group (the most
frequent spelling of the target wins), and — unlike genre rows — each
series row lands as PENDING: it retitles a whole group at once, so you
confirm it once in `bmf gui` and it is done.

## Workflow notes

- `bmf analyze --normalize` runs this pass at the end of analyze over the
  SAME scan (all books, verified ones included — the clustering needs the
  whole library; the flag exists because a second scan costs minutes on NFS
  even fully cached). The merge happens after analyze's review writer
  finalizes, so the analyzer's own entries are safe: PENDING entries get
  the C15/C16 proposals overlaid, decided ones are skipped. All three
  categories run — use the standalone command when you want `--authors`
  / `--genres` / `--tags` scoping or a dry-run first.
- Run `normalize --apply` BEFORE `bmf gui`/`bmf apply`. A later
  `bmf analyze` rotates review.yaml to `.bak` and merges by uuid:
  decided entries (including pre-filled `accept` from normalize) are
  carried verbatim, and pending normalize entries survive for books the
  analyzer itself does not re-flag (`finish()` carries unprocessed
  priors). Only when analyze re-flags the same book is a PENDING entry
  rebuilt fresh (the normalize proposal keys drop out) — re-running
  normalize re-proposes them.
- Author renames change the placement target: `bmf apply` moves those
  folders to the canonical author directory, so finish with
  `bmf abs-rescan --apply` (ABS keeps its own database). Series renames
  do NOT move folders — but they rewrite metadata.json, so the final
  `bmf abs-rescan --apply` covers them too.
- The command is idempotent: once the canonical values are on disk, a
  re-run finds nothing to propose.
