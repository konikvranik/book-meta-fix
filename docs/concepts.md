# Concepts

**English** | [Čeština](cs/concepts.md)

The mental model behind `book-meta-fix`. This complements
[architecture.md](architecture.md) (how it's built) and
[how-to/index.md](how-to/index.md) (how to run it). The per-category corruption reference
with real examples lives in [corruption-catalog.md](corruption-catalog.md).

## The central bet: don't trust embedded metadata

Calibre, at import time, **writes the (possibly wrong) DB metadata back into
the ebook file**. So the title/author declared inside an EPUB's `content.opf`
or a PDF's Info dictionary is *not independent evidence* — it can simply echo
the corruption we are trying to fix. Only signals that come from the **book's
actual text** can confirm a record:

1. **ISBN scanned from the content text** (the copyright page) — strongest.
2. **Fuzzy title/author match against the first-page text** (rapidfuzz,
   accent-insensitive). The author match recognises the same author under a
   different printed format — initials for given names ("A. Buškov" for
   "Alexandr Buškov"), dropped middle names, inflected/transliterated
   surnames ("Strugačtí" for "Strugackij") — while the first given name
   stays mandatory, so a surname mentioned in prose or a same-surname
   homonym never confirms.
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
| `OK` | passes all detector rules | eligible for apply's placement (clean path) |
| `VERIFIED` | OK **and** confirmed by book content | eligible for apply's placement |
| `AUTO_FIXABLE` | high-confidence fix, safe to apply automatically | `review.yaml` with `action: accept` pre-set, or auto-applied (C5 delete, C6 lock-file, MISSING_ISBN/YEAR/COVER enrich) |
| `NEEDS_REVIEW` | uncertain — a human must decide | `review.yaml`, you set the action |
| `UNFIXABLE` | cannot be resolved without manual input | `review.yaml` (fix the `proposed` values by hand) |

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
loop (`reconcile_loop`) instead of a single expensive call. The loop's *fast*
tier is not bound to Z.AI: with a Google Antigravity subscription, an
**Agent Client Protocol** agent (`BMF_ANTIGRAVITY_CMD`, e.g. Google's
`agy_acp_server.par` — see README "Fast tier via Google Antigravity (ACP)")
serves the first attempts while Z.AI keeps only the paid fallback role.

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
       │ passed  →  accept (source llm:high — eligible for the auto-verified
       │            pre-fill when the identity is content-confirmed)
       │ failed  →  return last proposal as confidence=low (an acceptable-missing
       │            book with content-confirmed identity is still auto-accepted
       │            as-is; others stay human-reviewed)
```

`verify_proposal` checks title + author against the first-page text (fuzzy,
accent-insensitive) plus an exact ISBN check. On failure it returns a short
reason ("the title 'X' not found in first-page text (fuzzy 0.41)") that is
appended to the next attempt's prompt. Books with no readable text skip
verification and accept the Flash result as-is.

**Rate limiting** — all calls (Flash + final + retries) go through a shared
leaky bucket, and HTTP-429 responses are dispatched on their **Z.AI sub-code**
(see [architecture.md → Concurrency model](architecture.md#concurrency-model)):
a **global 429/1302 cooldown** for the real `Rate limit reached for requests`
(one 1302 parks all workers, because Z.AI's free tier cascade-throttles every
model when one 429s), short interval-spaced retries for `1305 The service may
be temporarily overloaded` (server-side capacity — chronic on the free flash
models and NOT our fault, so it never arms the fleet cooldown; after a bounded
retry budget the loop falls through to the paid final model, and a fleet-wide
streak of consecutive rejections pauses the overloaded model for ~3 minutes),
and a per-model skip for `1308 Usage limit reached` (quota exhausted for the
run).

**Tolerant JSON** — GLM models frequently emit invalid JSON: Python literals
(`None`/`True`), trailing commas, **unescaped double-quotes inside string
values** (`"reasoning": "...contains "PROLOG"..."`), **raw control
characters** (newlines) inside strings, truncation at the token limit, and
**commentary-wrapped JSON** — a valid object followed by explanatory prose
and a second, fenced copy. `_parse_llm_json` salvages all of these: a cheap
built-in sanitizer handles the common cases, then `json-repair` (the `[llm]`
extra) recovers the hard ones, and wrapped responses yield their first
balanced `{...}` object (carved out by a string-aware brace scan, so this
works even without json-repair; a json-repair list result yields its first
dict — the model's answer, not its restated copy) so a near-perfect response
is never thrown away over a syntax slip.

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
  proposed:                 # our suggested fix — edit to override, null deletes
    title: R.U.R.
    author: Karel Čapek
    isbn: '9788072451648'
    source: embedded+openlibrary
  action: accept            # ← you fill this in
```

**Actions** (per entry):

| Action | Effect |
|---|---|
| `accept` | apply `proposed` (edit the values to override the analyzer; a `null` value deletes that field) |
| `delete` | remove the book folder (C6 ~$ Word lock-file; tar.gz-backed) |
| `keep` | like `accept`, but the entry is retained (not pruned) in review.yaml |
| `verified: true` | the user's persistent OK mark: apply stores it in metadata.json, analyze skips the book afterwards, apply routes it to the target path. Analyze pre-fills it when its own proposal completes the book (projected post-apply state is detector-clean), `bmf normalize` pre-fills a fresh deterministic (HIGH) entry whose projection is likewise clean — a leftover missing field keeps the book open — or — for an accepted entry — when the FINAL identity is confirmed against the content AND an independent record: an online source, a content-confirmed `llm:high` answer whose author/series passed the existence check, or the content tier itself (accept-missing stamp / text-mined fix — title+author bound to the book's own page text; embedded OPF never counts), or the author-pool tier (content had nothing to confirm against — scanned PDF, no title page — but the author is an established library author with ≥3 books and the title is neither an author nor a series name; proves the author, not the title — the accepted trade, re-openable via `--recheck-ok`). The content tier closes the books no source knows instead of re-extracting them every run; a verified book with a missing cover no longer re-queries the enrichers (`--recheck-ok` re-opens it). Flash-tier and unconfirmed answers do not count; benign MISSING_* leftovers may remain (plus a C2 title matching the ebook filename — noise once the identity is confirmed), a NEEDS_REVIEW leftover blocks it |

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

1. **databazeknih.cz by ISBN** (if `--databazeknih`) — exact match; best for
   CZ/SK. Returns genres (broad categories + user tags), ISBN, publisher,
   language, description, cover. Scraping (no API key), 2 requests/book.
2. **Self-hosted CZ provider by title** (if `--abs-czech URL` /
   `BMF_ABS_CZECH_URL`) — your own
   [audiobookshelf_czech_metadata](https://github.com/stecik/audiobookshelf_czech_metadata)
   instance aggregating ~17 CZ audiobook storefronts behind ABS's
   custom-provider `/search` API. Audio-edition metadata (publisher, year,
   cover, genres, language); no ISBN endpoint (title+author only). The
   provider's keyword search yields loose matches (Rozhlas/podcast rows with
   a `?` author), so a conflicting author always rejects a match and an
   author-less one needs a near-exact title (≥ 90). When
   several storefronts list the same book, the highest-resolution cover wins
   (probed via a streaming image-header read).
3. **databazeknih.cz by title** (if `--databazeknih`) — fuzzy title match
   gates the result so the wrong book's genres aren't attached; prefers a
   year-matching edition.
4. **legie.info** (if `--legie`) — CZ/SK sci-fi/fantasy; short stories and
   series/universe databazeknih's book search misses (identity only).
5. **OpenLibrary by ISBN** — international editions; weak CZ coverage (~10%).
6. **Google Books by ISBN** — often rate-limited without an API key.
7. **OpenLibrary by title**.

For a book with a cover diagnosis (C11 / MISSING_COVER) the two CZ sources
also compare covers against each other: the higher-resolution one wins, and
only the `cover_url` is swapped — the identity-anchored metadata stays from
the winning lookup.

## Placement patterns

`bmf apply` places each applied book: clean/`verified` ones move to a path
built from a format string (default `{author}/{title} ({id})`); books with
unresolved problems go to `<library>/<needfix-dir>/<original relative
path>` (default `needfix/`), preserving the structure so you can trace
provenance; dead records (no ebook file) to `needfix/empty/`. A resolved
book moves back out of needfix on the next apply.

| Field | Example | Notes |
|---|---|---|
| `{author}` | `Karel Čapek` | first author |
| `{author_sort}` | `Čapek, Karel` | "Lastname, Firstname" |
| `{title}` / `{title_sort}` | `R.U.R.` | title_sort moves leading The/A/An |
| `{id}` | `4895` | calibre_id |
| `{isbn}` | `9788072451648` | empty if missing |
| `{year}` / `{language}` | `1920` / `ces` | |
| `{series}` / `{series_index}` | `Ren Dhark` / `3` | empty if not in a series |
