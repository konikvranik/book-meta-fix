# book-meta-fix (bmf)

**English** | [Čeština](README.cs.md)

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
| [docs/how-to/](docs/how-to/index.md) | Step-by-step recipes (run a batch, tune the rate limit, debug, …) |
| [docs/corruption-catalog.md](docs/corruption-catalog.md) | The C1–C17 categories with real examples |
| [AGENTS.md](AGENTS.md) | Guide for AI agents editing this codebase (conventions, layout, gotchas) |

## Status

- [x] Scan (`bmf scan`)
- [x] Detect (`bmf report`) — C1–C14 rules
- [x] Verify (content vs metadata cascade)
- [x] Enrich (databazeknih.cz scraping for CZ/SK genres + metadata; legie.info for sci-fi/fantasy short stories & series; a self-hosted audiobookshelf_czech_metadata instance aggregating ~17 CZ audiobook storefronts; OpenLibrary + Google Books fallback)
- [x] Analyze + YAML review (`bmf analyze`, `bmf apply`)
- [x] Library-wide normalize (`bmf normalize`) — C15 author-name variants + C16 genre/tag variants, human-gated via review.yaml
- [x] Placement (`bmf apply`) — clean/verified books to the pattern path, unresolved to needfix/ (organize merged in)
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

#    Optional: query a self-hosted audiobookshelf_czech_metadata instance
#    (aggregates ~17 CZ audiobook storefronts; audio-edition metadata).
bmf analyze --abs-czech http://provider:8000 -o review.yaml --limit 1000

# 3. Edit review.yaml — set `action: accept|delete|keep` per entry
$EDITOR review.yaml
#    (or use the keyboard-driven GUI: `bmf gui --review review.yaml`)

# 4. Preview the changes (dry-run, no writes)
bmf apply review.yaml

# 5. Apply for real: writes metadata AND places each applied book —
#    clean/verified books to the pattern path, unresolved to needfix/
bmf apply --apply review.yaml

# 6. Generate missing EPUBs for OK books
bmf epubgen                        # dry-run
bmf epubgen --apply
```

> **Note:** every command that needs book metadata (`report`, `analyze`,
> `apply`, `epubgen`) runs an internal scan via `scan_library()`. There is
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
| `bmf report` | Run C1–C14 detector rules, show category breakdown + samples |
| `bmf analyze` | Full pipeline (scan+detect+extract+verify+enrich+location check) → generate `review.yaml` |
| `bmf apply <file>` | Apply approved changes from a review.yaml (dry-run by default) |
| `bmf apply --apply <file>` | Write `metadata.json` + `metadata.opf` AND place each book: clean/`verified` → target pattern path, unresolved → `needfix/`, dead records → `needfix/empty/` |
| `bmf gui` | Interactive keyboard-driven Tkinter editor for `review.yaml` |
| `bmf normalize` | Library-wide pass: cluster author-name variants (C15 — initials vs full names, diacritics, titles, anonym spellings, swapped order) and canonicalize genres/tags to Czech names (C16 — case/diacritics/word-order duplicates, EN→CZ aliases). Dry-run: show the clusters |
| `bmf normalize --apply` | Fill review.yaml with the C15/C16 proposals (deterministic ones pre-filled `accept`, judgement calls pending); `--authors`/`--genres`/`--tags` narrow the scope. Book writes happen via `bmf apply`; author renames also move folders, so finish with `bmf abs-rescan` |
| `bmf organize` | *(deprecated stub)* — placement was merged into `bmf apply` |
| `bmf epubgen` | Generate missing `.epub` files for OK books (from pdb/mobi/pdf/doc/txt) |
| `bmf epubgen --apply` | Actually generate the EPUBs |
| `bmf crosscheck` | Verify all formats in a folder are the same book; quarantine rogues |
| `bmf crosscheck --apply` | Actually move the mismatched format files |
| `bmf clean` | Unified library cleanup: strip invalid/generated covers and audit unconfirmed `verified` flags (dry-run) |
| `bmf clean --apply` | Actually rename bad covers to `.bak`, strip embedded placeholder covers, and clear `verified` flags on books with no ISBN whose author/series cannot be confirmed online |
| `bmf clean --apply --clear-all-verified` | Also clear `verified` flags from ALL books unconditionally (resetting them to review) |
| `bmf strip-covers` | Remove generated covers (dry-run: list affected books) |
| `bmf strip-covers --apply` | Actually remove them: `cover.jpg` → `.bak` + embedded EPUB covers stripped |
| `bmf strip-covers --invalid` | Remove INVALID covers instead: image-extension / `cover.*` files no decoder can read (behind ABS's ffmpeg "Invalid data found" errors) |
| `bmf strip-covers --generated embedded --apply` | Each selector (`--generated`, `--invalid`) takes an optional scope: `external`, `embedded`, or bare flag = both |
| `bmf abs-rescan` | Tell Audiobookshelf to re-read metadata of books changed recently (dry-run: list the mapping) |
| `bmf abs-rescan --apply` | Actually trigger the per-item ABS rescan (batch API); `--since 2h` narrows the window, `--force-all` re-scans the whole library |
| `bmf abs-rescan --fix-covers --apply` | Also null broken cover rows stored in the ABS database (coverPath at a non-image/missing file — the ffmpeg "Invalid data found" errors) and rescan those items |

Common options: `--library PATH`, `--limit N`, `--no-cache`, `-o FILE`,
`--skip-enrich`, `--skip-verify`, `--databazeknih`, `--legie`,
`--abs-czech URL`, `--accept-missing/--no-accept-missing` (default on).

`--accept-missing` (default): a `MISSING_ISBN`/`MISSING_YEAR`/`MISSING_COVER`
book whose author+title were confirmed against the book's content is
pre-filled `action: accept` in `review.yaml` (the missing field is cosmetic,
not an identity problem). `bmf apply` then prunes it in bulk — it's a safe
no-op when nothing was recovered. An LLM answer that failed verification
(`source: llm:low`) counts as nothing-recovered too: its untrusted proposal
is dropped and the book is accepted as-is. Use `--no-accept-missing` to keep
these for manual review. Books with a co-occurring `NEEDS_REVIEW` diagnosis
(e.g. a generated cover) are still sent to review.

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
accept, `Ctrl+D` delete, `Ctrl+K` keep, `Ctrl+G` recode content, `Ctrl+S`
save. Press `F1` for the full shortcut overlay.

**Bulk edit (`Ctrl+E`).** The list supports a multi-selection (`Ctrl+click`
toggles a row, `Shift+click` selects a range); `Ctrl+E` opens a small dialog
that sets the author or series — or, with `∅`, deletes the field — for
every selected book at once. The value lands in each book's `proposed`
block (a pending book becomes `accept`), the series order stays per-book,
and `Ctrl+S` writes the results as usual. Natural companion to the
`+ library` search: pull in every book of a broken series, select them,
fix the series name in one stroke.

**Bulk actions (`Ctrl+Shift+A/O/M`).** The same multi-selection drives three
more bulk commands — the Shift variant of the single-book shortcut means
"do it to all selected": `Ctrl+Shift+A` accepts every selected book (an
explicit selection overrides previous decisions), `Ctrl+Shift+O` toggles
the verified mark on all of them (clears it when every selected book
already carries it), and `Ctrl+Shift+M` deletes their covers (a dialog
mirrors the per-book checkboxes: `cover.jpg`, its `.bak`, the embedded
EPUB covers; a proposed `cover_url` is dropped so the next apply cannot
re-download what you just removed).

**Merge duplicates (`Ctrl+J`).** Select two or more books that are the same
work in multiple folders and press `Ctrl+J`. The dialog picks the survivor
(default: the focused row) and warns when `same_book` does not consider them
the same work; a field-by-field grid then shows every metadata field of every
selected book (a decided proposal counts as that book's value) — one radio
per cell picks which side the merged book keeps, with ∅ meaning "keep the
field empty". Untouched defaults reproduce the automatic gap-filling merge
(the survivor's value, else the first found). The picks are written straight
to the merged book and rebase a conflicting proposal, so a stale analyzer
suggestion cannot undo them at the next apply. Every other book's files move
into the survivor's folder, their folders are removed and their review
entries dropped — review.yaml is saved right after the merge, so the file
agrees with the disk. This is the explicit, user-driven counterpart of the
automatic merge apply performs when two folders collide at the same
placement target.

**Whole-library search (`+ library`).** The `Search:` box filters review
entries; tick `+ library` next to it and the same query also sweeps the
whole library — matching books that are NOT in review.yaml join the list
(sorted by author, their header says "not in review.yaml"). That's the
workflow for a series you know is broken (you spot bad metadata for the
"Mark Stone" books in Audiobookshelf → search the series, tick
`+ library`, and every Mark Stone book shows up, in review or not, ready
to edit). A fresh book behaves exactly like a review entry (fields,
covers, content view, actions); the only difference: `Ctrl+S` writes it
into `review.yaml` only once you decide or edit it (an action, the
verified mark, a proposal differing from current), so untouched books
never flood the file. The additions stay in the list for the rest of the
session — editing a book, saving or unticking the box does not throw
them away — and re-searching is idempotent (a book is never listed
twice). `bmf apply` then processes them like any other entry.

A fulltext index of the whole library is built by a background sweep the
moment the editor opens (progress in the status line; well under a minute
for ~5k books on NFS — the folder reads are parallelized, and the same
sweep feeds the author/series autocomplete). Every `+ library` search is
then an instant in-memory filter: no per-search library sweep, no
waiting; a query typed before the index is ready is answered automatically
the moment it lands. The index matches the same fields as the search box —
author, title, series, the folder path — plus the manifest-only fields
review entries never carry (annotation, publisher, tags), so a book with
broken metadata still matches when only its folder name or its annotation
mentions the series (typical for house-pseudonym series like Mark Stone,
where most books live under the real writers' folders). A book without a
uuid gets one minted during indexing (the same lazy identity augmentation
a scan performs).

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
| `--abs-czech URL` | self-hosted [audiobookshelf_czech_metadata](https://github.com/stecik/audiobookshelf_czech_metadata) | **Audio-edition CZ/SK metadata.** Your own instance aggregates ~17 CZ audiobook storefronts (Alza, Audiolibrix, Audioteka, Kosmas, Radioteka, Rozhlas, …) behind ABS's custom-provider `/search` API — publisher/year/cover/genres of the *audio* edition, ideal for an audiobook library. Fast (no third-party scraping from bmf's side). | Opt-in via base URL (`BMF_ABS_CZECH_URL`; token via `BMF_ABS_CZECH_TOKEN` for instances with `AUDIOBOOKSHELF_AUTH_TOKEN`). No ISBN endpoint — title+author only. Narrator/duration are not modeled by bmf and dropped. Junk-tolerant: the provider's keyword search (and its Rozhlas/podcast rows with a `?` author) yields loose matches, so a conflicting author is always rejected and an author-less match needs a near-exact title (≥ 90). Tried after databazeknih-by-ISBN, before its title search. || *(always on when enrichment enabled)* | OpenLibrary | ISBN + title search, international editions | Weak CZ coverage (~10%) |
| *(always on when enrichment enabled)* | Google Books | ISBN lookup | Often rate-limited without an API key |

Lookup order when enrichment is on: **databazeknih by ISBN (if enabled) → the self-hosted CZ provider by title (if a URL is configured) → databazeknih by title (if enabled) → legie.info (if enabled) → OpenLibrary by ISBN → Google Books by ISBN → OpenLibrary by title**. First hit wins.

**Cover resolution preference**: when the same book comes back several times
from the provider (multiple storefronts list it) or both CZ sources know it,
bmf probes the candidate covers by streaming just the image header (the image
body is never downloaded) and keeps the highest-resolution one — the
cross-source comparison runs for books with a cover diagnosis (C11 /
MISSING_COVER), and only the `cover_url` is swapped; the identity-anchored
metadata stays from the winning lookup.

```bash
# Enrich with CZ/SK genres only (no international fallbacks needed for a CZ library)
bmf analyze --databazeknih --limit 100 -o review.yaml

# Enable via env var instead of the flag
echo 'BMF_DATABAZEKNIH=1' >> .env

# Point at your self-hosted audiobookshelf_czech_metadata instance (base URL,
# not the /search endpoint; token only when deployed with auth enabled)
echo 'BMF_ABS_CZECH_URL=http://provider:8000' >> .env
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

### Fast tier via Google Antigravity (ACP)

If you have a **Google Antigravity** subscription, its Gemini models can serve
the loop's *fast* tier through Google's official **Agent Client Protocol**
agent — instead of glm-flash's chronically crowded free pool. The whole loop
can stay on the subscription (quick check on gemini-flash at low effort,
quality fallback on gemini-flash-high), or hand the quality stage to Z.AI when
a `ZAI_API_KEY` also exists — see the fallback knob below.

bmf speaks ACP v1 natively (JSON-RPC 2.0, one message per line over the agent
process's stdio — no extra dependency): it launches the agent, creates a
**fresh session per book** (a session keeps its history, so reusing one would
bleed one book's evidence into the next), denies tools and file access (the
evidence is already in the prompt), and parses the answer with the same JSON
salvage the Z.AI provider uses.

Setup — the short way (bmf manages the agent itself):

1. Opt in and run; the command that needs agy downloads the official agent
   itself on first use and keeps it current afterwards (registry check each
   run, upgrade when a newer version appears; offline runs keep the
   installed one):

   ```bash
   export BMF_LLM_PROVIDER=antigravity   # or: BMF_ANTIGRAVITY_CMD=auto
   bmf analyze --llm
   ```

   The first run streams ~700 MB (2.0 GB unpacked) into
   `~/.cache/book-meta-fix/acp` — shown as a progress bar inside the normal
   analyze bar; `XDG_CACHE_HOME` and `BMF_ACP_CACHE_DIR` relocate it. Both
   archive members are kept: the `agy_acp_server` binary and its
   `localharness_external` sibling (the agent resolves the harness at
   session creation, so an install without it fails every session — exec
   bits are set automatically), and an upgrade swaps both atomically (a
   failed download never destroys the working version). Plain auto mode
   (no explicit agy opt-in) adopts an EXISTING cache but never downloads
   on its own.

2. Log in once interactively (e.g. in Zed or the Antigravity IDE) — bmf is
   headless and cannot run an OAuth flow. The agent offers `Log in with
   Google` (personal), Gemini Enterprise, and a Gemini API key; credentials
   persist and bmf's `authenticate` handles expiry on its own. When they are
   missing, bmf says so and surfaces the agent's stderr (where login URLs
   land).

The manual way — pin your own binary, no cache, no auto-updates:

```bash
# current versioned link from the machine-readable registry (what editors use):
curl -s https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json \
  | python3 -c 'import json,sys; a=[x for x in json.load(sys.stdin)["agents"] if x["id"]=="antigravity-acp"][0]; print(a["distribution"]["binary"]["linux-x86_64"]["archive"])'
# as of 2026-09: .../agy-acp-server-agy_acp_server_1.1.1-linux-x86_64.zip —
# inside, agy_acp_server.par IS the server (a ~1.9 GB self-contained ELF;
# chmod +x when your archive manager drops the bit). Keep localharness_external
# NEXT TO IT (chmod +x too) — the agent needs it at session creation even
# though bmf denies tools; or point ANTIGRAVITY_HARNESS_PATH at it. The
# registry's launch spec adds the --uid= argument:
export BMF_ANTIGRAVITY_CMD="/opt/agy/agy_acp_server.par --uid="
bmf analyze --llm
```

Any ACP agent works the same way — e.g. `BMF_ANTIGRAVITY_CMD="gemini --acp"`
(Gemini CLI's ACP mode, a separate Google login). Provider selection
(`BMF_LLM_PROVIDER` / `--llm-provider`): **auto** (default) uses the ACP agent
when one is configured or already cached; **`zai`** never touches ACP;
**`antigravity`/`acp`/`agy`** forces the ACP branch (and self-manages the
agent download); **`off`** disables the LLM stage.

**The loop's two stages** both default to the subscription: the quick check
runs on **gemini-flash (low effort)** and the quality fallback on a **second
ACP pool with gemini-flash at high effort** (model names are family-matched
against the agent's own model list, so they survive generation bumps — on the
measured 1.1.1 agent, `gemini-flash-low` resolves to `gemini-3.8-flash-low`
and `gemini-flash-high` to `gemini-3.8-flash-high`, both answering in ~2 s).
Pro deliberately does NOT default: `gemini-pro` family-matches the Pro (High)
agent, which deliberates 16–37 s over one fallback book and rescued 0 of 50
LLM books in the measured run (flash passes verify 96 % of the time) — the
verifier is the quality gate, not the model tier; set
`BMF_ANTIGRAVITY_FALLBACK_MODEL=gemini-pro` if you want it anyway.
`BMF_ANTIGRAVITY_FALLBACK=glm` swaps
the quality stage to Z.AI's flash+paid loop instead (needs `ZAI_API_KEY`;
Z.AI's rate machinery applies untouched) — with a key configured, the whole
loop still stays on Antigravity unless you say otherwise.

| Knob | CLI | Env | Meaning |
|---|---|---|---|
| Agent command | `--antigravity-cmd` | `BMF_ANTIGRAVITY_CMD` (alias `BMF_ACP_COMMAND`) | explicit path (+ `--uid=` arg) = manual management; `auto`/empty = the self-managed cache (download + auto-update; see Setup) |
| Cache location | — | `BMF_ACP_CACHE_DIR` (or `XDG_CACHE_HOME`) | where the self-managed agent lives (default `~/.cache/book-meta-fix/acp`) |
| Fast-tier model | `--antigravity-model` | `BMF_ANTIGRAVITY_MODEL` (alias `BMF_ACP_MODEL`) | family-matched; `gemini-flash-low` default (newest flash at low effort — latency over deliberation), empty = the agent's default |
| Fallback provider | `--antigravity-fallback` | `BMF_ANTIGRAVITY_FALLBACK` (alias `BMF_ACP_FALLBACK`) | `agy` (default — second ACP pool on the fallback model) or `glm` (Z.AI flash+paid loop; needs a key) |
| Fallback model | `--antigravity-fallback-model` | `BMF_ANTIGRAVITY_FALLBACK_MODEL` (alias `BMF_ACP_FALLBACK_MODEL`) | agy fallback only; `gemini-flash-high` default, family-matched (`gemini-pro` = 16–37 s of deliberation, 0 rescues measured) |
| Prompt timeout | — | `BMF_ACP_TIMEOUT` | a hung turn is cancelled (`session/cancel`) after this many seconds (default 300) |
| In-flight agents | — | `BMF_ACP_MAX_INFLIGHT` | concurrent agent processes in the FAST pool (default 4; one agent ≈ 320 MB RSS, measured) |
| Fallback in-flight | — | `BMF_ANTIGRAVITY_FALLBACK_MAX_INFLIGHT` (alias `BMF_ACP_FALLBACK_MAX_INFLIGHT`) | agy quality pool size, default 1 — a serialized lane books queue on (fallback is the exception path, and prompt turns spike 30–100 s server-side under load, so parallel fallback slots buy little) |
| Politeness drip | — | `BMF_ACP_MIN_INTERVAL` | minimum seconds between prompt starts (default 0) |

Three consecutive transport failures (deleted binary, expired login) park the
fast tier for the rest of the run — later books go straight to the Z.AI
fallback instead of re-paying the spawn/timeout cost per book.

The agent is an autonomous IDE agent, not a completion endpoint, so bmf keeps
it on a short leash: every prompt leads with a no-tools directive (left alone,
the agent runs a tool cascade around the question — it lists the session
directory and deliberates over ~30 model round-trips per book, turning seconds
into tens of seconds), sessions are created in an empty scratch directory
instead of the library, and agent processes are recycled every few prompts
(the protocol offers no session/delete and every session pins a ~150 MB
harness process until the server exits). A flash answer lands in ~2 s.

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
       │ passed  →  accept (source llm:high — also eligible for the auto-verified
       │            pre-fill when the identity is content-confirmed)
       │ failed  →  return last proposal as confidence=low (an acceptable-missing
       │            book with content-confirmed identity is still auto-accepted
       │            as-is; others stay for review by the human)
```

`verify_proposal` checks both **title** and **author** against the book's
first-page text (fuzzy, accent-insensitive), plus an exact ISBN comparison. On
failure it returns a short reason ("the title 'X' is not found in the book's
first-page text (fuzzy 0.41)") that is appended to the next attempt's prompt.
Books with no readable text (image-only title pages, scanned PDFs) skip
verification and accept the Flash result as-is.

**Rate limiting**: all calls (Flash + final + retries) go through a shared
leaky bucket whose interval is **adaptive** (the configured value is only a
floor: 1302/1113 widen the drip, successes ease it back) **and** a hard
in-flight concurrency cap, and HTTP-429 responses are dispatched on their
**Z.AI sub-code** (all three arrive as 429 but mean opposite things):
1. the **leaky-bucket smoother** (count-per-time, default capacity 1 = pure
   even drip: exactly one call starts every `--llm-min-interval` seconds,
   evenly spaced, no bunching). `--llm-min-interval 2.0` = a steady 30
   evenly-spaced requests/minute — what Z.AI's sliding-window limit wants. A
   burst >1 lets several calls fire in the same second and trips the dynamic
   RPM limit; raise only with confirmed headroom; and
2. an **in-flight cap** (`--llm-max-inflight`, default 3) — the bucket spaces
   call *starts* but not call *depth*: with 10 workers and multi-second
   reasoning calls, the fallback herd (flash dies → everyone pivots to the
   paid model at once) exceeded the ~5 concurrent requests per account the
   coding plan admits (Z.AI publishes no exact numbers — the limits are
   tier-based and dynamic per their usage policy; measured: a 12-deep spike
   drew 7× 429/1302 while a 6-deep burst passed, and interactive clients —
   ZCode/chat — draw from the same ceiling), and the storms also surfaced as
   **false 1113** "insufficient balance" with quota left (acknowledged in
   Z.AI's own FAQ). Workers now queue on the semaphore instead of being
   rejected. Flash-family models get a stricter sub-cap (`min(2, cap)`): the
   free pool is chronically saturated and community reports put its
   concurrency as low as 1. With a coding-plan `ZAI_BASE_URL`, glm-4.x flash
   is additionally routed to the **PaaS endpoint** (`ZAI_FLASH_BASE_URL`,
   empty = auto): the two endpoints' concurrency ceilings are independent
   (measured), and flash there is free — its own pool and zero coding-plan
   credits. The global cap is also **adaptive** — an external
   client on the same plan (ZCode, chat) is invisible to bmf, so when Z.AI
   signals ceiling pressure (1302 / false 1113) bmf yields one in-flight
   slot and earns it back after ~20 clean responses; all calls share one
   pooled keep-alive HTTP connection set (no TLS re-handshake after a
   pause) with a finite read timeout so a hung call cannot squat a slot; and
3. a **global 429/1302 cooldown** (circuit breaker) — `Rate limit reached for
   requests` means OUR request rate tripped the RPM window (and Z.AI's free
   tier cascade-throttles the other models too), so when *any* worker sees a
   1302, *all* workers pause (`--llm-rate-limit-base` seconds, escalating
   5/10/20/…, honouring the server `Retry-After`, capped at
   `--llm-rate-limit-max`). One 429 parks the fleet instead of every worker
   hammering and 429-ing; and
4. **429/1305 overload handling** — `The service may be temporarily
   overloaded` is SERVER-side capacity (chronically frequent on the free
   flash models, not caused by our rate): the call retries shortly
   (interval-spaced, bounded budget) *without* arming the global cooldown,
   then falls back to the paid model; a fleet-wide streak of consecutive
   rejections pauses the overloaded model for ~3 minutes so every rate slot
   goes to the model that actually answers. 429/1113 "Insufficient balance"
   (short bursts on the coding endpoint that track flash-storm account
   throttling, despite quota left) gets the same handling with a shorter
   ~30 s pause.
   (429/1308 `Usage limit reached` skips the model for the rest of the run.)

See [how-to/llm.md → Tuning the LLM rate limit](docs/how-to/llm.md#tuning-the-llm-rate-limit)
for practical guidance.

**Invalid-JSON salvage**: GLM models often emit slightly broken JSON
(Python `None`/`True` literals, trailing commas, **unescaped quotes inside
string values**, raw control characters, truncation, **JSON wrapped in
commentary** — a valid object followed by explanatory prose and a second
fenced copy). `_parse_llm_json` recovers all of these — a cheap built-in
sanitizer handles the common cases, `json-repair` (the `[llm]` extra)
salvages the hard ones, and commentary-wrapped responses yield their first
balanced object — so a near-perfect response is never thrown away over a
syntax slip. You'll see `LLM JSON salvaged via json-repair …` or
`LLM JSON extracted from commentary-wrapped response …` in the log when
this kicks in.

Toggles:

| Knob | CLI | Env | Default |
|---|---|---|---|
| Loop on/off | `--no-llm-loop` | `BMF_LLM_LOOP=0` | on |
| Flash model | `--llm-model` | `BMF_LLM_MODEL` | `glm-4.7-flash` |
| Fallback model | `--llm-fallback-model` | `BMF_LLM_FALLBACK_MODEL` | `glm-5.3` |
| Steady call interval (s) | `--llm-min-interval` | `BMF_LLM_MIN_INTERVAL` | `2.0` |
| Max requests in flight | `--llm-max-inflight` | `BMF_LLM_MAX_INFLIGHT` | `3` |
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
replacement from an online source (the self-hosted CZ provider when
configured, else databazeknih.cz) when one is available.

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
└── needfix/                  # unresolved books placed here by `bmf apply`
    └── empty/                # dead records (no ebook file at all)
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
| `BMF_ABS_URL` | — | Audiobookshelf server base URL for `bmf abs-rescan` (e.g. `http://abs.lan:13378`; empty = the command is unavailable) |
| `BMF_ABS_TOKEN` | — | Audiobookshelf **admin** API token (Settings → Users → API key — the scan endpoints reject non-admin tokens) |
| `BMF_ABS_LIBRARY` | *(auto)* | ABS library name or id when the server hosts several book libraries |
| `BMF_ABS_WORKERS` | `4` | Parallel per-item scan requests for `bmf abs-rescan --apply` (`1` = serial) |
| `BMF_SCAN_WORKERS` | `8` | Parallel threads for the library scan (tree walk + per-folder metadata reads; NFS latency-bound). Also `bmf analyze/scan --scan-workers`. `1` = the historical serial scan |
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

## Corruption categories (C1–C17)

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
| C12 | author slug/artefact pollution (lost capitalization, leading `_`/`*`) | NEEDS_REVIEW |
| C13 | location mismatch (folder ≠ pattern-derived target) | AUTO_FIXABLE (move) |
| C14 | series order glued into the series name (`Mark Stone #73`) | AUTO_FIXABLE (split) |
| C15 | author-name variants / swapped order — library-level, emitted by `bmf normalize` only | AUTO_FIXABLE / NEEDS_REVIEW |
| C16 | genre/tag name variants (case, word order, EN/CZ, spelling) — library-level, emitted by `bmf normalize` only | AUTO_FIXABLE |
| C17 | invalid ebook file (content matches no book format) — emitted by `bmf clean --files` only | NEEDS_REVIEW (delete proposal) |
| — | EMPTY_BOOK (only metadata/backups/cover — the book file is gone) | AUTO_FIXABLE (`needfix/empty/`) |
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
  verified: true          # ← mark the book OK for good (see below)
```

**Actions:**
- `accept` — apply `proposed` (edit the values to override the analyzer; a
  `null` value deletes that field at apply time)
- `delete` — remove the book folder (C6 ~$ Word lock-file; tar.gz-backed)
- `keep` — like `accept`, but the entry is retained (not pruned) in this file

**Verified flag** (`verified: true`, orthogonal to the action — the GUI
checkbox / `Ctrl+O`): apply stores it in the book's `metadata.json`, later
`analyze` runs skip the book entirely, and apply routes it to the target
path even if some problems remain unrecoverable. Analyze pre-fills it when
its own proposal completes the book (the projected post-apply state is
detector-clean), so a fixed book never re-enters review. It is also
pre-filled for an accepted entry whose FINAL identity (the post-proposal
title/author, plus ISBN when known) is confirmed against the book's content
AND either an online source (databazeknih/legie/the self-hosted CZ provider/OpenLibrary/Google
Books) or a content-confirmed `llm:high` answer whose author/series passed the
existence check (flash-tier and unconfirmed answers do not count): such a book
is fixed AND closed in one apply even when benign fields stay missing (an
ISBN/year/cover no source has). A remaining NEEDS_REVIEW problem blocks the
pre-fill so a known defect stays visible — a missing cover is benign and may
stay, but a suspected generated Calibre cover (C11) is not. One C2 signal is
credited: a title matching the (never-renamed) ebook filename is noise once
the identity is confirmed, not corruption. Undo with
`bmf analyze --recheck-ok`.

Old review.yaml files with an `edited:` block or `action: edit|reject|swap`
are migrated on load (`edited` merges over `proposed`, `edit` becomes
`accept`, `reject`/`swap` reset to pending).

## How verification works

The verifier is the key insight: **embedded EPUB/PDF metadata is NOT trusted
as confirmation**, because Calibre wrote the (possibly wrong) DB metadata back
into the file at import time. Only **independent signals from the book's actual
text** can confirm a record:

1. **ISBN scanned from content text** (copyright page) — strongest signal
2. **Fuzzy title match against first-page text** (rapidfuzz)
3. **UNCERTAIN** if only embedded metadata is available (no readable text)

## Placement patterns (apply)

`bmf apply` does not only write metadata — after applying an entry it also
**places the book**: clean / `verified` books move to a path built from a
format string (default `{author}/{title} ({id})`), books with unresolved
problems move to `needfix/`, and dead records (no ebook file at all) to
`needfix/empty/`. The decision is re-derived from the FINAL metadata using
the metadata-only detectors — no content reads, so apply stays fast. The
former `bmf organize` command (which re-classified the whole library on
every run) is a deprecation stub; analyze flags misplaced books for you via
the C13 location check (pre-filled `action: accept`).

Available pattern fields:

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
bmf apply --apply review.yaml --pattern "{author_sort}/{title} ({id})"
bmf apply --apply review.yaml --pattern "{author}/{series}/{title}" --needfix-dir "_problems"
# (or set BMF_PATTERN / BMF_NEEDFIX_DIR in .env; --no-place skips moving entirely)
```

Broken books go to `<library>/<needfix-dir>/<original relative path>`
(default `needfix/`), preserving the original folder structure so you can
trace where they came from.

### Collision handling (duplicate-book merge)

When two OK books resolve to the same target path (common with an `{id}`-less
pattern, or duplicate `calibre_id`s), apply no longer blindly appends
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
bmf apply --apply review.yaml --pattern "{author}/{title}"   # merge dups, disambiguate editions
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

Collisions append ` (dup N)` to the folder name (the same convention apply's placement
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
  key), or `--abs-czech URL` / `BMF_ABS_CZECH_URL` for a self-hosted
  [audiobookshelf_czech_metadata](https://github.com/stecik/audiobookshelf_czech_metadata)
  instance (aggregates ~17 CZ audiobook storefronts; audio-edition metadata).
  OpenLibrary and Google Books remain as international fallbacks but
  have poor Czech ISBN coverage. `obalkyknih.cz` API requires a library key
  (not yet implemented).
- **Mojibake in EPUB content**: when Calibre imported a book with corrupt
  metadata, it wrote that corruption into the EPUB's `content.opf` too. The
  verifier cannot detect this via text matching (the corrupt title is present
  in both DB and content). Mitigated upstream by the C4 detector.
- **Scanned PDFs**: no text layer → no verification signal.
