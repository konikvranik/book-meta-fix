# book-meta-fix (bmf)

Detect and fix metadata of ebooks in a Calibre-style library.

Designed for a ~5,000-book library where Calibre mis-classified many records
(swapped author/title, filename-as-title, encoding corruption, translators
listed as authors). **Source of truth is `metadata.json`** (Audiobookshelf
manifest); on write, both `metadata.json` and `metadata.opf` are updated so
Audiobookshelf and Kavita pick up the fixes on rescan.

## Status

- [x] Scan (`bmf scan`)
- [x] Detect (`bmf detect`) — C1–C10 rules
- [x] Verify (content vs metadata cascade)
- [x] Enrich (databazeknih.cz scraping for CZ/SK genres + metadata; OpenLibrary + Google Books fallback)
- [x] Report + YAML review (`bmf report`, `bmf apply`)
- [x] Organize (`bmf organize`) — split OK vs needfix
- [x] EPUB generation (`bmf epubgen`)
- [ ] LLM reconciliation (Z.AI, for C1/C4/C5) — pending `ZAI_API_KEY`
- [x] Tests (51 passing) + docs

## Quick start

```bash
cd ~/priv/git/book-meta-fix
make dev-install                  # create .venv, install package + dev deps

# 1. See what's wrong (statistics only, no writes)
bmf detect --limit 500

# 2. Generate a review file (this also scans; no separate `bmf scan` needed)
bmf report --skip-enrich -o review.yaml --limit 1000

#    Optional: enrich with CZ/SK genres from databazeknih.cz (no API key,
#    2 HTTP requests per book, opt-in scraping). Adds genres + metadata to
#    the proposed block.
bmf report --databazeknih -o review.yaml --limit 1000

# 3. Edit review.yaml — set `action: accept|reject|swap|edit` per entry
$EDITOR review.yaml

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

> **Note:** every command that needs book metadata (`detect`, `report`,
> `organize`, `epubgen`) runs an internal scan via `scan_library()`. There is
> no need to run `bmf scan` first — its only purpose is to print summary
> statistics. The scan uses a SQLite cache (`bmf_cache.db`) so repeated runs
> are fast; pass `--no-cache` to force a full re-parse.

### Streaming `review.yaml` (live results)

`bmf report` writes `review.yaml` **incrementally** — as each book finishes
processing, its entry is appended to the file, Unix-pipe style. You can
`tail -f review.yaml` and watch the proposals arrive while the run continues.
On start, the existing `review.yaml` is moved to `review.yaml.bak` (so user
decisions from a prior run are preserved); on clean finish the `.bak` is
deleted. If the run is interrupted (Ctrl-C, crash), the `.bak` is kept so you
can recover the pre-run state.

- **Ctrl-C is safe**: results collected so far are already in the file, and
  `finish()` carries over any prior entries the run didn't reach (e.g. with
  `--limit`). Nothing a user previously decided is silently dropped.
- **`--auto-apply` is inline**: high-confidence proposals are written to
  `metadata.json`/`metadata.opf` as they're produced, and those books are
  omitted from `review.yaml` (which only holds what still needs a human).
- **Format**: multi-document YAML (`---` per entry). `bmf apply` reads both
  the new multi-doc form and the legacy single-list form.

## Commands

| Command | What it does |
|---|---|
| `bmf scan` | Traverse library, parse metadata, print summary stats |
| `bmf detect` | Run C1–C10 detector rules, show category breakdown + samples |
| `bmf report` | Full pipeline (scan+detect+extract+verify+enrich) → generate `review.yaml` |
| `bmf apply <file>` | Apply approved changes from a review.yaml (dry-run by default) |
| `bmf apply --apply <file>` | Actually write `metadata.json` + `metadata.opf` |
| `bmf organize` | Move OK books to a clean path pattern; broken to `needfix/` |
| `bmf organize --apply` | Actually move the folders |
| `bmf epubgen` | Generate missing `.epub` files for OK books (from pdb/mobi/pdf/doc/txt) |
| `bmf epubgen --apply` | Actually generate the EPUBs |

Common options: `--library PATH`, `--limit N`, `--no-cache`, `-o FILE`,
`--skip-enrich`, `--skip-verify`, `--databazeknih`.

## Enrichment sources

Online metadata lookups are **off by default** (`--skip-enrich` is the default
for `report`). Enable them with the flags below; results are cached in
`bmf_cache.db` so re-runs don't re-hit the network.

| Flag | Source | Strengths | Notes |
|---|---|---|---|
| `--databazeknih` | databazeknih.cz | **Best for CZ/SK**. Returns genres (broad categories + user tags), ISBN, publisher, language, description, cover. | Scraping (no API key). 2 requests/book. Fuzzy title match gates the result so the wrong book's genres aren't attached. |
| *(always on when enrichment enabled)* | OpenLibrary | ISBN + title search, international editions | Weak CZ coverage (~10%) |
| *(always on when enrichment enabled)* | Google Books | ISBN lookup | Often rate-limited without an API key |

Lookup order when enrichment is on: **databazeknih (if enabled) → OpenLibrary by ISBN → Google Books by ISBN → OpenLibrary by title**. First hit wins.

```bash
# Enrich with CZ/SK genres only (no international fallbacks needed for a CZ library)
bmf report --databazeknih --limit 100 -o review.yaml

# Enable via env var instead of the flag
echo 'BMF_DATABAZEKNIH=1' >> .env
```

## How the fix pipeline picks a proposal

For each NEEDS_REVIEW book, `bmf report` tries to recover correct metadata in
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

`bmf report --llm` uses Z.AI's GLM API as the fallback. Five model settings
were measured on a sample of hard CZ/SK books (`scripts/llm_experiment.py`):

| Variant | ok% | in tok | out tok | reasoning | wall s | Cost ($/1M in/out) |
|---|---|---|---|---|---|---|
| **glm-5.2 reasoning_effort=low (default)** | 100% | 1529 | 346 | yes | 6.7 | 1.40 / 4.40 |
| glm-4.6 thinking=disabled | 100% | 1522 | 139 | no | 3.0 | 0.60 / 2.20 |
| glm-4.5-air thinking=disabled | 100% | 1522 | 122 | no | 6.5 | 0.20 / 1.10 |
| glm-4.5-flash | 100% | 1527 | 96 | no | 7.6 | free |
| glm-4.7-flash | 100% | 1522 | 147 | no | 3.4 | free |

Non-reasoning models use 3–4× fewer output tokens, but on CZ/SK series they
hallucinate more (returning the title of a different book by the same author,
dropping diacritics, inventing authors). **GLM-5.2 with `reasoning_effort=low`
is the default** — it keeps quality while cutting ~60% of reasoning tokens vs
the model default. Switch when you know what you are doing:

```bash
# Cheapest, accepts lower CZ/SK quality (good when the LLM is a rare fallback)
bmf report --llm --llm-model glm-4.5-flash

# GLM-4.6 non-reasoning: cheaper than 5.2, better than flash on CZ
bmf report --llm --llm-model glm-4.6   # thinking=disabled is the default for 4.x

# More reasoning (slow, costly) for a hard batch
bmf report --llm --llm-reasoning-effort max
```

| Knob | CLI | Env | Applies to |
|---|---|---|---|
| Model | `--llm-model` | `ZAI_MODEL` | all |
| Reasoning effort | `--llm-reasoning-effort` | `ZAI_REASONING_EFFORT` | GLM-5.x (`low` default) |
| Thinking toggle | `--llm-thinking` | `ZAI_THINKING` | GLM-4.x (`disabled` default) |

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

**Rate limiting**: all calls (Flash + final + retries) go through a shared
leaky-bucket smoother (default capacity 5, one token every `--llm-min-interval`
seconds). This keeps the aggregate request rate constant regardless of when
worker threads arrive, which avoids tripping Z.AI's dynamic RPM limit (429 code
1302). Free-tier Flash models are throttled more aggressively than paid ones
and share Z.AI's cascade-cooldown bug (one model getting rate-limited can take
the others down with it); on a Flash 429 the loop falls through to the paid
final model immediately rather than burning more free-tier attempts.

Toggles:

| Knob | CLI | Env | Default |
|---|---|---|---|
| Loop on/off | `--no-llm-loop` | `BMF_LLM_LOOP=0` | on |
| Flash model | `--llm-flash-model` | `ZAI_FLASH_MODEL` | `glm-4.5-flash` |
| Final model | `--llm-final-model` | `ZAI_FINAL_MODEL` | `glm-5.2` |
| Burst capacity | `--llm-burst` | `BMF_LLM_BURST` | `5` |

```bash
# Single fast cheap call, no loop (e.g. for a quick test run)
bmf report --llm --no-llm-loop --llm-model glm-4.5-flash

# Stricter rate matching for a free plan (1 call/burst, 2s apart)
bmf report --llm --llm-burst 1 --llm-min-interval 2.0
```

## Library layout expected

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
| `BMF_LIBRARY` | `/mnt/share_nfs/Shared eBooks` | Library root |
| `BMF_CACHE` | `bmf_cache.db` | SQLite cache path |
| `BMF_REVIEW` | `review.yaml` | Default review file path |
| `ZAI_API_KEY` | — | Z.AI API key (LLM, optional — phase 7) |
| `ZAI_BASE_URL` | `https://api.z.ai/api/paas/v4/` | Z.AI base URL |
| `ZAI_MODEL` | `glm-5.2` | Z.AI model |

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
| — | MISSING_ISBN / MISSING_YEAR | AUTO_FIXABLE (enrich) |

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
