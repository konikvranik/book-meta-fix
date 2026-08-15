# book-meta-fix (bmf)

Detect and fix metadata of ebooks in a Calibre-style library.

Designed for a ~5,000-book library where Calibre mis-classified many records
(swapped author/title, filename-as-title, encoding corruption, translators
listed as authors). **Source of truth is `metadata.json`** (Audiobookshelf
manifest); on write, both `metadata.json` and `metadata.opf` are updated so
Audiobookshelf and Kavita pick up the fixes on rescan.

## Documentation

| Doc | What it covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Module map, data flow, concurrency model, caching, atomicity |
| [docs/concepts.md](docs/concepts.md) | Verdict buckets, verification philosophy, fix cascade, LLM loop, review.yaml format |
| [docs/how-to.md](docs/how-to.md) | Step-by-step recipes (run a batch, tune the rate limit, debug, …) |
| [docs/corruption-catalog.md](docs/corruption-catalog.md) | The C1–C10 categories with real examples |
| [AGENTS.md](AGENTS.md) | Guide for AI agents editing this codebase (conventions, layout, gotchas) |

## Status

- [x] Scan (`bmf scan`)
- [x] Detect (`bmf report`) — C1–C10 rules
- [x] Verify (content vs metadata cascade)
- [x] Enrich (databazeknih.cz scraping for CZ/SK genres + metadata; legie.info for sci-fi/fantasy short stories & series; OpenLibrary + Google Books fallback)
- [x] Analyze + YAML review (`bmf analyze`, `bmf apply`)
- [x] Organize (`bmf organize`) — split OK vs needfix
- [x] EPUB generation (`bmf epubgen`)
- [x] Cross-format consistency (`bmf crosscheck`) — quarantine formats whose content differs from metadata
- [ ] LLM reconciliation (Z.AI, for C1/C4/C5) — pending `ZAI_API_KEY`
- [x] Tests (445 passing) + docs

## Quick start

```bash
cd ~/priv/git/book-meta-fix
make dev-install                  # create .venv, install package + dev deps

# 1. See what's wrong (statistics only, no writes)
bmf report --limit 500

# 2. Generate a review file (this also scans; no separate `bmf scan` needed)
bmf analyze --skip-enrich -o review.yaml --limit 1000

#    Optional: enrich with CZ/SK genres from databazeknih.cz (no API key,
#    2 HTTP requests per book, opt-in scraping). Adds genres + metadata to
#    the proposed block.
bmf analyze --databazeknih -o review.yaml --limit 1000

# 3. Edit review.yaml — set `action: accept|reject|swap|edit|keep` per entry
$EDITOR review.yaml
#    (or use the keyboard-driven GUI: `bmf gui --review review.yaml`)

# 4. Preview the changes (dry-run, no writes)
bmf apply review.yaml

# 5. Apply for real
bmf apply --apply review.yaml

# 6. Split library: OK books to clean paths, broken to needfix/
bmf organize                       # dry-run
bmf organize --apply

# 7. Generate missing EPUBs for OK books
bmf epubgen                        # dry-run
bmf epubgen --apply
```

> **Note:** every command that needs book metadata (`report`, `analyze`,
> `organize`, `epubgen`) runs an internal scan via `scan_library()`. There is
> no need to run `bmf scan` first — its only purpose is to print summary
> statistics. The scan uses a SQLite cache (`bmf_cache.db`) so repeated runs
> are fast; pass `--no-cache` to force a full re-parse.

### Streaming `review.yaml` (live results)

`bmf analyze` writes `review.yaml` **incrementally** — as each book finishes
processing, its entry is appended to the file, Unix-pipe style. You can
`tail -f review.yaml` and watch the proposals arrive while the run continues.
On start, the existing `review.yaml` is moved to `review.yaml.bak` (so user
decisions from a prior run are preserved); on clean finish the `.bak` is
deleted. If the run is interrupted (Ctrl-C, crash), the `.bak` is kept so you
can recover the pre-run state.

- **Ctrl-C is safe**: results collected so far are already in the file, and
  `finish()` carries over any prior entries the run didn't reach (e.g. with
  `--limit`). Nothing a user previously decided is silently dropped.
- **Format**: multi-document YAML (`---` per entry). `bmf apply` reads both
  the new multi-doc form and the legacy single-list form.

## Commands

| Command | What it does |
|---|---|
| `bmf scan` | Traverse library, parse metadata, print summary stats |
| `bmf report` | Run C1–C10 detector rules, show category breakdown + samples |
| `bmf analyze` | Full pipeline (scan+detect+extract+verify+enrich) → generate `review.yaml` |
| `bmf apply <file>` | Apply approved changes from a review.yaml (dry-run by default) |
| `bmf apply --apply <file>` | Actually write `metadata.json` + `metadata.opf` |
| `bmf gui` | Interactive keyboard-driven Tkinter editor for `review.yaml` |
| `bmf organize` | Move OK books to a clean path pattern; broken to `needfix/` |
| `bmf organize --apply` | Actually move the folders |
| `bmf epubgen` | Generate missing `.epub` files for OK books (from pdb/mobi/pdf/doc/txt) |
| `bmf epubgen --apply` | Actually generate the EPUBs |
| `bmf crosscheck` | Verify all formats in a folder are the same book; quarantine rogues |
| `bmf crosscheck --apply` | Actually move the mismatched format files |

Common options: `--library PATH`, `--limit N`, `--no-cache`, `-o FILE`,
`--skip-enrich`, `--skip-verify`, `--databazeknih`, `--legie`,
`--accept-missing/--no-accept-missing` (default on).

`--accept-missing` (default): a `MISSING_ISBN`/`MISSING_YEAR`/`MISSING_COVER`
book whose author+title were confirmed against the book's content is
pre-filled `action: accept` in `review.yaml` (the missing field is cosmetic,
not an identity problem). `bmf apply` then prunes it in bulk — it's a safe
no-op when nothing was recovered. Use `--no-accept-missing` to keep these for
manual review. Books with a co-occurring `NEEDS_REVIEW` diagnosis (e.g. a
generated cover) are still sent to review.

## Interactive editor (`bmf gui`)

`bmf gui` is a keyboard-first Tkinter editor for walking `review.yaml` one
book at a time, instead of hand-editing the YAML. It edits the same
`action` / `edited` / `notes` fields — the actual metadata is still written by
`bmf apply` afterwards.

```bash
bmf analyze -o review.yaml            # generate first (as above)
bmf gui --review review.yaml          # open the editor
bmf apply review.yaml                 # commit the decisions (dry-run first)
```

**Prerequisite:** the Tk bindings. On Debian/Ubuntu install
`python3-tk` (`sudo apt install python3-tk`). No extra pip package — Pillow
(already a dependency) drives the cover thumbnails.

**What it shows per book:** read-only *current* fields next to editable
*target* fields (copy any single field over with `Ctrl+L`), one-key
author↔title swap (`Ctrl+W`), a read-only view of the *proposed* block, the
book's folder path as a clickable link that opens it in your file manager
(double-clicking a list row does the same), cover
previews — current / `.bak` / recommended, plus the cover EMBEDDED in each
format file — each with its own checkbox on the cover, and clicking the
cover itself ticks it (`Ctrl+M` then removes
the checked embedded covers from the e-book files, which themselves stay
put — useful for cleaning out invalid calibre placeholders; EPUB only),
and a per-format content view with double-encoding repair (`Ctrl+G`, for
texts broken by a redundant cp1250→utf8 recode — or pick the codec pair
manually: "přečteno jako" = the wrong codec the text was once read through,
"skutečně je" = the real encoding of the bytes, nearly always utf-8; a
`⇄` swaps them). A pair that cannot run (e.g. utf-8→cp1250, whose undefined
bytes choke on common Czech chars) is explained in the hint, which offers
the reversed direction as a click; bytes already lost to an earlier
replace-decode (shown as `�`) don't block the repair — they stay marked at
their position. Two-layer chains (a wild sample: cp1250 text mis-read as
cp1251, re-saved utf-8, mis-read as cp1250 again) are repaired
automatically and named in the hint. The result is always rendered as
UTF-8 — but the `↻ Překódovat` toggle itself is NEVER auto-checked:
detecting the corruption only presets the codec pair and the hint, seeing
the repaired text is your call (tick it / press `Ctrl+G`). Drag the grip
under the content preview to resize it vertically (double-click resets).
The whole detail column scrolls; hovering a thumbnail in the list pops up
a larger cover. The list itself is canvas-rendered — the label always
sits left, the cover thumbnail flush against the right edge of the row
(ttk's Treeview can only show per-row images in its leftmost column).

**Everything is bound to `Ctrl+letter`** (bare letters keep typing into the
fields): `PgUp`/`PgDn` move between books, `Tab` cycles only the editable
fields (never buttons or read-only labels), `Ctrl+A` selects all in a field,
focus stays on the same field when you change book. Actions: `Ctrl+Enter`
accept, `Ctrl+R` reject, `Ctrl+E` edit, `Ctrl+D` delete, `Ctrl+K` keep,
`Ctrl+G` recode content, `Ctrl+S` save. Press `F1` for the full shortcut
overlay.

**The `keep` action** applies the proposal like `accept`, but the entry is
**retained** in `review.yaml` (not pruned) and `bmf analyze` **skips** the
book on the next run — useful for a record you've settled but want to keep
visible. To re-decide a kept book, set its action back to `pending`
(`Ctrl+0`) and re-run `analyze`.

## Enrichment sources

Online metadata lookups are **off by default** (`--skip-enrich` is the default
for `analyze`). Enable them with the flags below; results are cached in
`bmf_cache.db` so re-runs don't re-hit the network.

| Flag | Source | Strengths | Notes |
|---|---|---|---|
| `--databazeknih` | databazeknih.cz | **Best for CZ/SK**. Returns genres (broad categories + user tags), ISBN, publisher, language, description, cover. | Scraping (no API key). 2 requests/book. Fuzzy title match gates the result so the wrong book's genres aren't attached. |
| `--legie` | legie.info | **Best for CZ/SK sci-fi/fantasy**. Indexes short stories ("povídky") and the series/universe a work belongs to, which databazeknih's book search misses. Strong for identity (title + author + original title). | Scraping (no API key). No ISBN/Year/Publisher (identity only). Tried after databazeknih. |
| *(always on when enrichment enabled)* | OpenLibrary | ISBN + title search, international editions | Weak CZ coverage (~10%) |
| *(always on when enrichment enabled)* | Google Books | ISBN lookup | Often rate-limited without an API key |

Lookup order when enrichment is on: **databazeknih (if enabled) → legie.info (if enabled) → OpenLibrary by ISBN → Google Books by ISBN → OpenLibrary by title**. First hit wins.

```bash
# Enrich with CZ/SK genres only (no international fallbacks needed for a CZ library)
bmf analyze --databazeknih --limit 100 -o review.yaml

# Enable via env var instead of the flag
echo 'BMF_DATABAZEKNIH=1' >> .env
```

## How the fix pipeline picks a proposal

For each NEEDS_REVIEW book, `bmf analyze` tries to recover correct metadata in
**cheap-first order** so the LLM is reached only as a last resort:

1. **Offline — page-text mining** (`text_meta`): reads the book's first-page
   text (already extracted for verification) and mines title / authors / ISBN /
   year / publisher using CZ/SK heuristics — ALL-CAPS title-page runs, explicit
   `Název:` / `Autor:` / `Nakladatelství:` labels, the `Neznámý` placeholder
   drop, CSS-leakage stripping. No network. On a 30-book sample this finds a
   title for ~37% and any field for ~47% of NEEDS_REVIEW books.
2. **Online by ISBN** (`extracted.isbn_from_text` > embedded ISBN): OpenLibrary
   + Google Books.
3. **Online by title + author** (text-mined > embedded > DB): this is the path
   that reaches **databazeknih.cz** — the strongest CZ/SK source.
4. **Embedded-OPF compare** (weakest; calibre may have overwritten the OPF).
5. **LLM fallback** — only when 1–4 all miss.

The LLM fallback model and its reasoning controls are configurable; see below.

### LLM model choice

`bmf analyze --llm` uses Z.AI's GLM API as the fallback. Five model settings
were measured on a sample of hard CZ/SK books (`scripts/llm_experiment.py`):

| Variant | ok% | in tok | out tok | reasoning | wall s | Cost ($/1M in/out) |
|---|---|---|---|---|---|---|
| **glm-5.2 reasoning_effort=low (measured; glm-5.3 is now the default)** | 100% | 1529 | 346 | yes | 6.7 | 1.40 / 4.40 |
| glm-4.6 thinking=disabled | 100% | 1522 | 139 | no | 3.0 | 0.60 / 2.20 |
| glm-4.5-air thinking=disabled | 100% | 1522 | 122 | no | 6.5 | 0.20 / 1.10 |
| glm-4.5-flash | 100% | 1527 | 96 | no | 7.6 | free |
| glm-4.7-flash | 100% | 1522 | 147 | no | 3.4 | free |

Non-reasoning models use 3–4× fewer output tokens, but on CZ/SK series they
hallucinate more (returning the title of a different book by the same author,
dropping diacritics, inventing authors). **GLM-5.3 with `reasoning_effort=low`
is the fallback default** (the loop-off single-call default too) — it keeps
quality while cutting ~60% of reasoning tokens vs the model default. The loop's
first attempt defaults to the free `glm-4.7-flash`. Switch when you know what
you are doing:

```bash
# Cheapest, accepts lower CZ/SK quality (good when the LLM is a rare fallback)
bmf analyze --llm --llm-model glm-4.5-flash

# GLM-4.6 non-reasoning: cheaper than 5.2, better than flash on CZ
bmf analyze --llm --llm-model glm-4.6   # thinking=disabled is the default for 4.x

# More reasoning (slow, costly) for a hard batch
bmf analyze --llm --llm-reasoning-effort max
```

| Knob | CLI | Env | Applies to |
|---|---|---|---|
| Loop model | `--llm-model` | `BMF_LLM_MODEL` | loop first attempt (`glm-4.7-flash` default; the fallback model when the loop is off) |
| Fallback model | `--llm-fallback-model` | `BMF_LLM_FALLBACK_MODEL` | `glm-5.3` default; also the loop-off single-call default |
| Reasoning effort | `--llm-reasoning-effort` | `ZAI_REASONING_EFFORT` | GLM-5.x (`low` default) |
| Thinking toggle | `--llm-thinking` | `ZAI_THINKING` | GLM-4.x (`disabled` default) |

Legacy `ZAI_MODEL` / `ZAI_FLASH_MODEL` / `ZAI_FINAL_MODEL` are still read
(mapped to the fallback / loop model / fallback respectively) but log a
deprecation warning — migrate to the `BMF_LLM_LOOP_*` names.

Re-run the experiment yourself as Z.AI's lineup evolves:

```bash
.venv/bin/python scripts/llm_experiment.py --limit 10
```

### LLM self-correction loop

When the deterministic stages (offline text mining, online lookup) miss, the
LLM fallback runs a **self-correction loop** (on by default) instead of a
single expensive call:

```
 1. GLM-4.5-Flash (free, thinking off)  →  verify_proposal(title, author vs first-page text)
       │ passed  →  accept (source llm:flash)              [the common case — 0 USD]
       │ failed  →  inject feedback into the next attempt
       ▼
 2. GLM-4.5-Flash with feedback  (max 2 Flash attempts)   [still 0 USD]
       │ passed  →  accept (source llm:loop)
       │ failed / 429  →  fall through
       ▼
 3. GLM-5.2 reasoning_effort=low  (paid, high quality)    [only the hard cases]
       │ passed  →  accept (source llm:high)
       │ failed  →  return last proposal as confidence=low (still reviewed by the human)
```

`verify_proposal` checks both **title** and **author** against the book's
first-page text (fuzzy, accent-insensitive), plus an exact ISBN comparison. On
failure it returns a short reason ("the title 'X' is not found in the book's
first-page text (fuzzy 0.41)") that is appended to the next attempt's prompt.
Books with no readable text (image-only title pages, scanned PDFs) skip
verification and accept the Flash result as-is.

**Rate limiting**: all calls (Flash + final + retries) go through two shared
layers:
1. a **leaky-bucket smoother** (count-per-time, default capacity 1 = pure even
   drip: exactly one call starts every `--llm-min-interval` seconds, evenly
   spaced, no bunching). `--llm-min-interval 2.0` = a steady 30 evenly-spaced
   requests/minute — what Z.AI's sliding-window limit wants. A burst >1 lets
   several calls fire in the same second and trips the dynamic RPM limit; raise
   only with confirmed headroom; and
2. a **global 429 cooldown** (circuit breaker) — Z.AI's free tier has a
   cascade-cooldown bug: when one model gets rate-limited, the others (incl.
   the paid fallback) get throttled too. So when *any* worker sees a 429,
   *all* workers pause (`--llm-rate-limit-base` seconds, escalating
   5/10/20/…, honouring the server `Retry-After`, capped at
   `--llm-rate-limit-max`). One 429 parks the fleet instead of every worker
   hammering and 429-ing. On a Flash 429 the loop falls through to the paid
   final model immediately — but that final call now *waits* for the cooldown
   rather than instantly 429-ing too.

See [how-to.md → Tuning the LLM rate limit](docs/how-to.md#tuning-the-llm-rate-limit)
for practical guidance.

**Invalid-JSON salvage**: GLM models often emit slightly broken JSON
(Python `None`/`True` literals, trailing commas, **unescaped quotes inside
string values**, raw control characters, truncation). `_parse_llm_json`
recovers all of these — a cheap built-in sanitizer handles the common cases,
then `json-repair` (the `[llm]` extra) salvages the hard ones — so a
near-perfect response is never thrown away over a syntax slip. You'll see
`LLM JSON salvaged via json-repair …` in the log when this kicks in.

Toggles:

| Knob | CLI | Env | Default |
|---|---|---|---|
| Loop on/off | `--no-llm-loop` | `BMF_LLM_LOOP=0` | on |
| Flash model | `--llm-model` | `BMF_LLM_MODEL` | `glm-4.7-flash` |
| Fallback model | `--llm-fallback-model` | `BMF_LLM_FALLBACK_MODEL` | `glm-5.3` |
| Steady call interval (s) | `--llm-min-interval` | `BMF_LLM_MIN_INTERVAL` | `2.0` |
| Burst capacity | `--llm-burst` | `BMF_LLM_BURST` | `1` (even drip) |
| Base 429 cooldown (s) | `--llm-rate-limit-base` | `BMF_LLM_RATE_LIMIT_BASE` | `5` |
| Max 429 cooldown (s) | `--llm-rate-limit-max` | `BMF_LLM_RATE_LIMIT_MAX` | `60` |

```bash
# Single fast cheap call, no loop (e.g. for a quick test run)
bmf analyze --llm --no-llm-loop --llm-model glm-4.5-flash

# Stricter rate matching for a free plan (1 call/burst, 4s apart, longer cooldown)
bmf analyze --llm --llm-burst 1 --llm-min-interval 4.0 --llm-rate-limit-base 10
```

## Cover replacement

Calibre's default "Generate cover" produces a placeholder image (solid
background + rendered title/author text) at exactly 1200×1600. The pipeline
detects these by pixel analysis — **no LLM involved** — and proposes a
replacement from databazeknih.cz when one is available.

**Detection** (`covers.py` + `rule_generated_cover`): three signals, each adds
confidence; a cover is classified as generated at confidence ≥ 0.5:

| Signal | Weight | What it means |
|--------|--------|---------------|
| Dimensions == 1200×1600 | +0.5 | Calibre default template signature |
| Few unique colours (< ~50 at 64-colour quantization) | +0.3 | Solid background + text |
| Dominant colour covers > 60% of pixels | +0.2 | Flat background |

**Categories:**
- `C11` — generated cover detected (NEEDS_REVIEW). Replacement proposed when a `cover_url` is available.
- `MISSING_COVER` — no `cover.jpg` sidecar at all (AUTO_FIXABLE).

**Flow** (same as metadata proposals — no separate command):

```
bmf analyze --databazeknih           # detect C11/MISSING_COVER, fetch cover_url
# → review.yaml entry with action: accept (auto-set when databazeknih matched)
bmf apply review.yaml --apply        # downloads cover_url → cover.jpg (with .bak)
```

**Cost:** zero LLM tokens. Detection is Pillow pixel math (~5 ms/book).
Download is one HTTP request per replaced cover, rate-limited at 1 s/host.


```
<library>/
├── <Author>/
│   └── <Title> (<calibre_id>)/
│       ├── metadata.json     # primary source (Audiobookshelf manifest)
│       ├── metadata.opf      # fallback source (Calibre OPF 2.0)
│       ├── <Title> - <Author>.epub
│       ├── <Title> - <Author>.pdb
│       └── cover.jpg
└── needfix/                  # broken books moved here by `bmf organize`
    └── <Author>/...          #   (preserving the original relative subpath)
```

Excluded automatically from scans: `temp_calibre/`, `calibre-*/`, `needfix/`,
`~$*` (Word lock files), dotfiles.

## Configuration

Settings resolve from (highest precedence first):

1. **CLI flags** — `--library`, `--pattern`, ...
2. **Process environment variables** — `BMF_LIBRARY`, `ZAI_API_KEY`, ...
3. **`.env` file** — searched by walking up from CWD: `./.env`, `../.env`,
   `../../.env`, ... (the first existing file wins; values are loaded as
   defaults, so real env vars still win). Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   $EDITOR .env
   ```
4. **Built-in defaults**

| Variable | Default | Purpose |
|---|---|---|
| `BMF_LIBRARY` | `~/Books` | Library root |
| `BMF_CACHE` | `bmf_cache.db` | SQLite cache path |
| `BMF_REVIEW` | `review.yaml` | Default review file path |
| `BMF_LANGUAGE` | *(auto)* | Interface language — `cs` or `en`. Auto-detected from the user's locale (`cs*` → Czech, anything else → English). Can also be set per-run: `bmf --lang cs report` |
| `ZAI_API_KEY` | — | Z.AI API key (LLM, optional — phase 7) |
| `ZAI_BASE_URL` | `https://api.z.ai/api/paas/v4/` | Z.AI base URL |
| `BMF_LLM_MODEL` | `glm-4.7-flash` | LLM loop first-attempt model (fallback model when the loop is off) |
| `BMF_LLM_FALLBACK_MODEL` | `glm-5.3` | LLM paid fallback model |

### Localization (cs / en)

CLI messages, option help, the `bmf gui` editor and the `review.yaml`
header comment are localized via gettext. **Source strings (msgids) are
English**; English is also the fallback when no translation exists. Czech
lives in `src/book_meta_fix/locales/cs/LC_MESSAGES/bmf.po` (the compiled
`.mo` is committed, so a plain install never needs pybabel).

Language resolution (highest first): `--lang` CLI flag → `BMF_LANGUAGE`
(env/`.env`) → locale auto-detection. Note: click help texts are built at
import time, so `--lang` switches runtime messages only — use
`BMF_LANGUAGE` to get fully Czech `--help` output.

After changing translatable strings:

```bash
make i18n-extract   # update .po from source (needs pybabel)
$EDITOR src/book_meta_fix/locales/cs/LC_MESSAGES/bmf.po
make i18n-compile   # .po -> .mo
```

## Corruption categories (C1–C10)

See [`docs/corruption-catalog.md`](docs/corruption-catalog.md) for the full
catalog with real examples. Summary:

| Code | Description | Typical verdict |
|---|---|---|
| C1 | author/title swapped | NEEDS_REVIEW |
| C2 | filename used as title (diacritics lost) | NEEDS_REVIEW |
| C3 | series/library/publisher used as author | NEEDS_REVIEW |
| C4 | metadata has unrepairable mojibake | NEEDS_REVIEW (LLM) |
| C5 | literal placeholder record ("author"/"title") | AUTO_FIXABLE (delete) |
| C6 | MS-Word lock-file duplicate (`~$`) | AUTO_FIXABLE (delete) |
| C7 | glued authors ("byX...andY") | NEEDS_REVIEW |
| C8 | translator mislabeled as author | NEEDS_REVIEW |
| C9 | anonym (mostly fake — real anonym is whitelisted) | NEEDS_REVIEW |
| C10 | long multi-author list (anthology vs translator team) | NEEDS_REVIEW |
| C11 | generated cover (Calibre placeholder) detected by pixel analysis | NEEDS_REVIEW |
| — | MISSING_ISBN / MISSING_YEAR | AUTO_FIXABLE (enrich) |
| — | MISSING_COVER (no `cover.jpg` sidecar) | AUTO_FIXABLE (download) |

## YAML review format

```yaml
- id: 4895
  path: "Karel Capek/_apek_Karel-RURe_n_ (4895)"
  diagnosis:
    category: C2
    reason: "title == primary file stem"
    confidence: HIGH
  current:                # what's in the DB now
    author: Karel Capek
    title: _apek_Karel-RURe_n_
    year: 2012
    language: ces
  proposed:               # our suggested fix (from content/online)
    title: R.U.R.
    author: Karel Čapek
    isbn: '9788072451648'
    year: 1920
    source: embedded+openlibrary
  action: accept          # ← you fill this in
  # edited:               # uncomment for action: edit
  #   title: R.U.R. (Rossum's Universal Robots)
```

**Actions:**
- `accept` — apply `proposed` as-is
- `reject` — leave unchanged
- `swap` — swap author ↔ title (for C1 cases)
- `edit` — apply fields under `edited:` (these override everything)

## How verification works

The verifier is the key insight: **embedded EPUB/PDF metadata is NOT trusted
as confirmation**, because Calibre wrote the (possibly wrong) DB metadata back
into the file at import time. Only **independent signals from the book's actual
text** can confirm a record:

1. **ISBN scanned from content text** (copyright page) — strongest signal
2. **Fuzzy title match against first-page text** (rapidfuzz)
3. **UNCERTAIN** if only embedded metadata is available (no readable text)

## Organize patterns

`bmf organize` moves OK books to a path built from a format string. Default:
`{author}/{title} ({id})`. Available fields:

| Field | Example | Notes |
|---|---|---|
| `{author}` | `Karel Čapek` | first author |
| `{author_sort}` | `Čapek, Karel` | "Lastname, Firstname" |
| `{title}` | `R.U.R.` | |
| `{title_sort}` | `R.U.R.` | leading article moved (The/A/An) |
| `{id}` | `4895` | calibre_id |
| `{isbn}` | `9788072451648` | empty if missing |
| `{year}` | `1920` | empty if missing |
| `{language}` | `ces` | |
| `{series}` | `Ren Dhark` | empty if not part of a series |
| `{series_index}` | `3` | |

Examples:
```bash
bmf organize --pattern "{author_sort}/{title} ({id})"
bmf organize --pattern "{author}/{series}/{title}" --needfix-dir "_problems"
```

Broken books go to `<library>/<needfix-dir>/<original relative path>`
(default `needfix/`), preserving the original folder structure so you can
trace where they came from.

### Collision handling (duplicate-book merge)

When two OK books resolve to the same target path (common with an `{id}`-less
pattern, or duplicate `calibre_id`s), `organize` no longer blindly appends
` (dup N)`. It detects whether they are the **same book** and acts accordingly:

- **Same book** (ISBN agrees, **or** title + author fuzzy-match and the year
  doesn't disagree) ⇒ **merged** into one folder: all format files combined,
  metadata field-merged (ISBN record is the base, tie → lower id; missing
  fields filled from the other, authors/tags unioned). The loser folder is
  removed; the winner's `calibre_id` determines the merged path.
- **Different books** at the same path ⇒ each is **disambiguated** rather than
  merged: by **year** (`Title (2026)/`) when their years differ, otherwise by
  **id** (`Title (id123)/` — the `id` prefix keeps it visually distinct from a
  year). All colliding books get the suffix for consistency.
- ` (dup N)` survives only as a last-resort fallback (e.g. two different books
  sharing the same `calibre_id` under an `{id}` pattern, where id-suffixing
  can't help).

Dry-run by default; merges only run with `--apply`. The post-run summary shows
a "Merges" table (which loser merged into which winner) so the result is
auditable.

```bash
bmf organize --pattern "{author}/{title}" --apply   # merge dups, disambiguate editions
```

## Cross-format consistency (`bmf crosscheck`)

A book folder often holds several formats of the same title (`.epub`, `.pdf`,
`.pdb`, `.prc`, `.txt`, `.doc`, …). Sometimes a different book got mixed into
the folder — a file swapped in, or Calibre merged two records. `bmf crosscheck`
verifies that **every format in a folder is the same book the metadata
declares**, and quarantines the ones that aren't.

```bash
bmf crosscheck                  # dry-run: report rogues, move nothing
bmf crosscheck --apply          # move each rogue into its own needfix folder
```

**How it decides.** For each folder with ≥2 formats, every format file is
extracted and its content compared against the folder's metadata. The verdict
per format is AGREES / DISAGREES / UNCERTAIN, using only **text-mined signals**
(the project's core rule: embedded EPUB/PDF metadata is uninformative because
Calibre wrote the DB metadata back into the file at import):

1. **ISBN** scanned from the page text vs the DB ISBN — equal ⇒ AGREES, differ
   ⇒ DISAGREES (strongest signal).
2. **Title** — the DB title fuzzy-searched in the first-page text (partial_ratio,
   the same check `verify` uses). ≥ `--threshold` (0.8) ⇒ AGREES, <
   `--weak-threshold` (0.5) ⇒ DISAGREES, between ⇒ UNCERTAIN.

Per-folder decision:

| Decision | When | Action |
|---|---|---|
| `clean` | no DISAGREES | nothing moved |
| `quarantine` | ≥1 AGREES **and** ≥1 DISAGREES | the DISAGREES files are rogues → moved |
| `ambiguous` | DISAGREES but no AGREES | **not moved** — the metadata itself may be wrong (nothing corroborates it); review manually |
| `skipped` | fewer than 2 formats | nothing to cross-check |

**Quarantine path.** Each rogue moves into its **own fully-isolated** folder so
two rogues from the same book are never merged (they may be different wrong
books):

```
<library>/needfix/crosscheck/<Author> - <Title> (<id>) - <filename>/<filename>
```

Collisions append ` (dup N)` to the folder name (the same convention `organize`
uses — never merge, never overwrite). The book folder's cache entry is
invalidated on a real move so the next scan re-parses it.

**Limitations.** Metadata-anchored only (a format is "right" when it agrees with
the metadata). Formats with no extractable text (image-only PDFs, comics without
`ComicInfo.xml`/OCR) are UNCERTAIN and never auto-quarantined. Pairwise
disagreements that the metadata can't resolve are reported but not auto-resolved.

**`.mbp` sidecars.** Mobipocket annotation files (`.mbp`, the reading-position
bookmarks from the old Mobipocket Reader) are recognized as format files. They
are not books — but they carry UTF-16 `AUTH`/`TITL` records written by the
reading device, which calibre never touched. In folders where the actual book
file was lost (64 in this library, several book-less), the `.mbp` is the only
identity evidence left, and its author/title flow into the review as
suggestions. A `.mbp` never becomes the primary format when a real book exists
(it is last in the format preference) and is excluded from `epubgen` sources.

## Optional external tools

- **`pdftotext` / `pdfinfo`** (poppler-utils) — PDF content & metadata extraction
- **`ebook-convert`** + **`ebook-meta`** (calibre) — EPUB generation from
  pdb/mobi/doc, and fallback metadata extraction
- **`pandoc`** — fallback EPUB generation from txt/doc/rtf/html

The tool works without them, but with reduced format coverage.

## Known limitations

- **Online enrichment for CZ/SK books**: use `--databazeknih` for
  CZ/SK-focused lookup via databazeknih.cz scraping (genres + metadata, no API
  key). OpenLibrary and Google Books remain as international fallbacks but
  have poor Czech ISBN coverage. `obalkyknih.cz` API requires a library key
  (not yet implemented).
- **Mojibake in EPUB content**: when Calibre imported a book with corrupt
  metadata, it wrote that corruption into the EPUB's `content.opf` too. The
  verifier cannot detect this via text matching (the corrupt title is present
  in both DB and content). Mitigated upstream by the C4 detector.
- **Scanned PDFs**: no text layer → no verification signal.
