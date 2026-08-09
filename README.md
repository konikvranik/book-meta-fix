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
