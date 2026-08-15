# Concepts

The mental model behind `book-meta-fix`. This complements
[architecture.md](architecture.md) (how it's built) and
[how-to.md](how-to.md) (how to run it). The per-category corruption reference
with real examples lives in [corruption-catalog.md](corruption-catalog.md).

## The central bet: don't trust embedded metadata

Calibre, at import time, **writes the (possibly wrong) DB metadata back into
the ebook file**. So the title/author declared inside an EPUB's `content.opf`
or a PDF's Info dictionary is *not independent evidence* — it can simply echo
the corruption we are trying to fix. Only signals that come from the **book's
actual text** can confirm a record:

1. **ISBN scanned from the content text** (the copyright page) — strongest.
2. **Fuzzy title/author match against the first-page text** (rapidfuzz,
   accent-insensitive).
3. **UNCERTAIN** if only embedded metadata is available (no readable text).

This is why the verifier (`verifier.py`) and the LLM self-correction loop
both key off `first_page_text`, and why books with no readable text
(image-only title pages, scanned PDFs) are hard — there is nothing to
confirm against.

## Source of truth

`metadata.json` (the Audiobookshelf manifest) is the **source of truth**;
`metadata.opf` (Calibre OPF 2.0) is a fallback kept for Kavita/Calibre
compatibility. On write, **both** are updated atomically (`writers.py`) so a
rescan picks up the fix everywhere. Readers prefer `metadata.json`, fall back
to `.opf`, and use the folder path as a weak last-resort signal.

## Verdict buckets

After detection + verification each book lands in one `Verdict`
(`models.py`), which decides what happens to it:

| Verdict | Meaning | Where it goes |
|---|---|---|
| `OK` | passes all detector rules | eligible for `organize` (clean path) |
| `VERIFIED` | OK **and** confirmed by book content | eligible for `organize` |
| `AUTO_FIXABLE` | high-confidence fix, safe to apply automatically | `review.yaml` with `action: accept` pre-set, or auto-applied (C5 delete, C6 lock-file, MISSING_ISBN/YEAR/COVER enrich) |
| `NEEDS_REVIEW` | uncertain — a human must decide | `review.yaml`, you set the action |
| `UNFIXABLE` | cannot be resolved without manual input | `review.yaml` (reject or hand-edit) |

## Corruption categories (C1–C11)

Short summary — see [corruption-catalog.md](corruption-catalog.md) for the
full catalog with real examples and the rationale for each rule.

| Code | Description | Typical verdict |
|---|---|---|
| C1 | author/title swapped | NEEDS_REVIEW (`swap`) |
| C2 | filename used as title (diacritics lost) | NEEDS_REVIEW |
| C3 | series/library/publisher used as author | NEEDS_REVIEW |
| C4 | unrepairable mojibake | NEEDS_REVIEW (LLM) |
| C5 | literal placeholder record ("author"/"title") | AUTO_FIXABLE (delete) |
| C6 | MS-Word lock-file duplicate (`~$`) | AUTO_FIXABLE (delete) |
| C7 | glued authors ("byX...andY") | NEEDS_REVIEW |
| C8 | translator mislabeled as author | NEEDS_REVIEW |
| C9 | anonym (mostly fake; real anonym is whitelisted) | NEEDS_REVIEW |
| C10 | long multi-author list (anthology vs translator team) | NEEDS_REVIEW |
| C11 | generated cover (Calibre placeholder), by pixel analysis | NEEDS_REVIEW |
| — | MISSING_ISBN / MISSING_YEAR | AUTO_FIXABLE (enrich) |
| — | MISSING_COVER (no `cover.jpg` sidecar) | AUTO_FIXABLE (download) |

`detect()` returns the **highest-priority** match as the primary diagnosis and
attaches every other match to `.additional`, so one book can carry several
problems at once (e.g. C2 + C11).

## The fix cascade (cheap first, LLM last)

For each NEEDS_REVIEW book, `bmf analyze` recovers correct metadata in
cost order. The first stage that yields a useful, verified proposal wins:

1. **Offline page-text mining** (`text_meta`) — mines the first-page text for
   ALL-CAPS title-page runs, `Název:`/`Autor:`/`Nakladatelství:` labels, ISBN,
   year, publisher. No network.
2. **Online by ISBN** (text-mined > embedded) — OpenLibrary + Google Books.
3. **Online by title + author** — this is the path that reaches
   **databazeknih.cz**, the strongest CZ/SK source (genres + metadata).
4. **Embedded-OPF compare** (weakest — calibre may have overwritten the OPF).
5. **LLM fallback** (`llm.reconcile_loop`) — only when 1–4 all miss.

A proposal is only accepted if it passes `confirm_identity` (title + author
fuzzy-match the first-page text, or ISBN matches). Books without usable
first-page text skip the LLM entirely (no point spending tokens on nothing).

## The LLM self-correction loop

When the deterministic stages miss, the LLM fallback runs a self-correction
loop (`reconcile_loop`) instead of a single expensive call:

```
 1. GLM-4.x Flash (free, thinking off)  →  verify_proposal(title, author vs first-page text)
       │ passed  →  accept (source llm:flash)              [the common case — 0 cost]
       │ failed  →  inject feedback into the next attempt
       ▼
 2. GLM Flash with feedback  (max 2 Flash attempts)        [still 0 cost]
       │ passed  →  accept (source llm:loop)
       │ failed / 429  →  fall through
       ▼
 3. GLM-5.2 reasoning_effort=low  (paid, high quality)     [only the hard cases]
       │ passed  →  accept (source llm:high)
       │ failed  →  return last proposal as confidence=low (still human-reviewed)
```

`verify_proposal` checks title + author against the first-page text (fuzzy,
accent-insensitive) plus an exact ISBN check. On failure it returns a short
reason ("the title 'X' not found in first-page text (fuzzy 0.41)") that is
appended to the next attempt's prompt. Books with no readable text skip
verification and accept the Flash result as-is.

**Rate limiting** — all calls (Flash + final + retries) go through two shared
layers (see [architecture.md → Concurrency model](architecture.md#concurrency-model)):
a leaky-bucket smoother (constant aggregate RPM) and a **global 429 cooldown**
(one 429 parks all workers, because Z.AI's free tier cascade-throttles every
model when one 429s). On a Flash 429 the loop falls through to the paid final
model immediately rather than burning more free-tier attempts — but now the
final call *waits* for the cooldown first instead of instantly 429-ing too.

**Tolerant JSON** — GLM models frequently emit invalid JSON: Python literals
(`None`/`True`), trailing commas, **unescaped double-quotes inside string
values** (`"reasoning": "...contains "PROLOG"..."`), **raw control
characters** (newlines) inside strings, and truncation at the token limit.
`_parse_llm_json` salvages all of these: a cheap built-in sanitizer handles
the common cases, then `json-repair` (the `[llm]` extra) recovers the hard
ones so a near-perfect response is never thrown away over a syntax slip.

## The review.yaml workflow

The review file is the primary **human-in-the-loop** mechanism. `bmf analyze`
writes it **incrementally** — as each book finishes, its entry is appended
(`review_writer.py`, one `---` YAML document per book, Unix-pipe style). You
can `tail -f review.yaml` and watch proposals arrive.

```yaml
---
- id: 4895
  path: "Karel Capek/_apek_Karel-RURe_n_ (4895)"
  diagnosis:
    category: C2
    reason: "title == primary file stem"
    confidence: HIGH
  current:                  # what's in the DB now
    author: Karel Capek
    title: _apek_Karel-RURe_n_
  proposed:                 # our suggested fix
    title: R.U.R.
    author: Karel Čapek
    isbn: '9788072451648'
    source: embedded+openlibrary
  action: accept            # ← you fill this in
  # edited:                 # uncomment for action: edit
  #   title: R.U.R. (Rossum's Universal Robots)
```

**Actions** (per entry):

| Action | Effect |
|---|---|
| `accept` | apply `proposed` as-is |
| `reject` | leave unchanged |
| `swap` | swap author ↔ title (for C1 cases) |
| `edit` | apply only the fields under `edited:` (they override everything) |

On start, the existing `review.yaml` is moved to `review.yaml.bak` (prior
decisions preserved); on a clean finish the `.bak` is deleted; on interruption
it is kept so you can recover. `bmf apply` reads both the multi-doc form and
the legacy single-list form.

## Cover replacement

Calibre's "Generate cover" produces a placeholder (solid background + rendered
title/author text) at exactly 1200×1600. `covers.py` detects these by **pixel
analysis — no LLM** — and proposes a replacement from databazeknih.cz when a
`cover_url` is available.

Detection signals (each adds confidence; generated at ≥ 0.5):

| Signal | Weight |
|---|---|
| Dimensions == 1200×1600 (Calibre template signature) | +0.5 |
| Few unique colours (< ~50 at 64-colour quantization) | +0.3 |
| Dominant colour covers > 60% of pixels (flat background) | +0.2 |

- **C11** — generated cover detected → NEEDS_REVIEW, replacement proposed when
  `cover_url` is available.
- **MISSING_COVER** — no `cover.jpg` sidecar at all → AUTO_FIXABLE.

Cost: zero LLM tokens (~5 ms/book detection; one HTTP request per replaced
cover, rate-limited at 1 s/host).

## Enrichment sources

Online lookups are **off by default** (`--skip-enrich` is the default for
`analyze`). Results are cached in `bmf_cache.db`. Lookup order when enrichment
is on — **first hit wins**:

1. **databazeknih.cz** (if `--databazeknih`) — best for CZ/SK. Returns genres
   (broad categories + user tags), ISBN, publisher, language, description,
   cover. Scraping (no API key), 2 requests/book, fuzzy title match gates the
   result so the wrong book's genres aren't attached.
2. **OpenLibrary by ISBN** — international editions; weak CZ coverage (~10%).
3. **Google Books by ISBN** — often rate-limited without an API key.
4. **OpenLibrary by title**.

## Organize patterns

`bmf organize` moves OK/VERIFIED books to a path built from a format string
(default `{author}/{title} ({id})`). Broken books go to
`<library>/<needfix-dir>/<original relative path>` (default `needfix/`),
preserving the structure so you can trace provenance.

| Field | Example | Notes |
|---|---|---|
| `{author}` | `Karel Čapek` | first author |
| `{author_sort}` | `Čapek, Karel` | "Lastname, Firstname" |
| `{title}` / `{title_sort}` | `R.U.R.` | title_sort moves leading The/A/An |
| `{id}` | `4895` | calibre_id |
| `{isbn}` | `9788072451648` | empty if missing |
| `{year}` / `{language}` | `1920` / `ces` | |
| `{series}` / `{series_index}` | `Ren Dhark` / `3` | empty if not in a series |
