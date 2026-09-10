# Architecture

**English** | [Čeština](cs/architecture.md)

This document describes how `book-meta-fix` (`bmf`) is structured internally:
the module map, the data flow for a single book, the concurrency model, and
the key design decisions. For *what* the corruption categories mean see
[concepts.md](concepts.md); for the same flows as UML diagrams (Mermaid)
see [diagrams.md](diagrams.md); for *how to run things* see
[how-to/index.md](how-to/index.md); for the command reference see the [README](../README.md).

## Goal

Repair metadata of a ~5,000-book Calibre-style ebook library where many
records are corrupt (swapped author/title, filename-as-title, mojibake,
translators listed as authors, generated placeholder covers). **The source of
truth is `metadata.json`** (the Audiobookshelf manifest); on write, both
`metadata.json` and `metadata.opf` are updated so Audiobookshelf and Kavita
pick up the fix on rescan. Every change is gated by a human-in-the-loop
`review.yaml` unless it is a high-confidence auto-fix.

## High-level data flow

```
                     library/  (Calibre-style folders)
                          │
                          ▼
            ┌─────────────────────────┐
            │  readers.py + library.py│  parse metadata.json/opf + path
            │  (+ SQLite cache)       │  → BookMeta
            └────────────┬────────────┘
                         ▼
            ┌─────────────────────────┐
            │   detectors.py (C1–C11) │  classify corruption → Diagnosis
            └────────────┬────────────┘
                         ▼
            ┌─────────────────────────┐
            │  extractors.py          │  read embedded meta + first-page text
            │  (+ text_meta mining)   │  + ISBN from content → ExtractedMeta
            └────────────┬────────────┘
                         ▼
            ┌─────────────────────────┐
            │   verifier.py           │  compare DB meta vs BOOK CONTENT
            │   (do NOT trust embed)  │  → VERIFIED / NEEDS_REVIEW / UNCERTAIN
            └────────────┬────────────┘
                         ▼
   ┌─────────────────────────────────────────────┐
   │  Fix cascade (cheap first, LLM last):       │  pipeline.py
   │   1. text_meta     (offline page-text mine) │
   │   2. ISBN lookup   (OpenLibrary/Google)     │  ← enrichers.py
   │   3. title lookup  (databazeknih.cz best)   │
   │   4. embedded-OPF compare (weakest)         │
   │   5. LLM fallback  (Z.AI GLM, self-correct) │  ← llm.py
   └────────────────┬────────────────────────────┘
                    ▼  (EnrichedMeta proposal or None)
        ┌───────────────────────┐
        │   review_writer.py    │  stream proposals → review.yaml
        │   (one `---` doc/blk) │  (tail -f live; .bak carry-over)
        └───────────┬───────────┘
                    ▼  human sets action: accept/delete/keep
                    (edits proposed values; null = field delete;
                     verified: true = the persistent user-OK mark)
        ┌───────────────────────┐
        │   review.py (parse) → │  bmf apply
        │   writers.py          │  atomic write metadata.json + .opf (.bak)
        └───────────┬───────────┘
                    ▼  optional downstream commands
   ┌────────────────┐  ┌───────────────┐  ┌───────────────────┐
   │ mover.py       │  │ epubgen.py    │  │ crosscheck.py     │
   │ bmf epubgen    │  │ bmf crosscheck │  │ (organize: stub)  │
   │ (OK→clean path)│  │ (missing epub)│  │ (rogue formats)   │
   └────────────────┘  └───────────────┘  └───────────────────┘
```

The vertical spine (`scan → detect → extract → verify → fix cascade → review`)
runs inside **one** command: `bmf analyze`. The other commands are either
read-only views (`scan`, `report`) or downstream writers
(`apply`, `epubgen`, `crosscheck`; `organize` is a deprecation stub — placement runs inside `apply`).

## Module map

All source lives under `src/book_meta_fix/`. Tests mirror the module name
(`tests/test_<module>.py`).

| Module | Responsibility |
|---|---|
| `models.py` | Core dataclasses: `BookMeta`, `Diagnosis`, `Book`; `Verdict` & `Confidence` enums. The shared vocabulary every other module speaks. |
| `readers.py` | Parse `metadata.json` (primary) and `metadata.opf` (fallback) into `BookMeta`; also `parse_path()` (the folder name is a weak signal). |
| `library.py` | Traverse the library tree, apply the SQLite cache, yield `BookMeta` for each book folder. Excludes Calibre scratch dirs, dotfiles, MS-Word `~$` lock files. |
| `detectors.py` | Rules C1–C11 → `Diagnosis`. `detect()` returns the highest-priority match with the rest attached as `.additional`; `detect_all()` returns all matches. |
| `extractors.py` | Per-format content extraction: embedded metadata + first-page text + ISBN-from-text. Multi-format fallback when the primary file is broken. → `ExtractedMeta` |
| `text_meta.py` | Offline page-text mining (ALL-CAPS title pages, `Název:`/`Autor:` labels, `Neznámý` drop). The first, free stage of the fix cascade. |
| `encoding.py` | Mojibake detection + repair (octal-escape `\376\377...` and mis-decoded cp1250/iso-8859-2). Flags unrecoverable fields for the LLM. |
| `isbn.py` | ISBN extraction/canonicalization/validation (10/13 digit, hyphenated, `ISBN:`-prefixed, trailing `X`). |
| `verifier.py` | Compare DB metadata against the book's **actual content** (not embedded meta). Cascading signals: ISBN match → fuzzy title → UNCERTAIN. `verify_proposal` + `confirm_identity` gate LLM/enricher proposals. |
| `enrichers.py` | Online metadata sources: a self-hosted audiobookshelf_czech_metadata provider (opt-in via `BMF_ABS_CZECH_URL`, CZ storefront aggregator), databazeknih.cz (CZ/SK, scrape), legie.info, OpenLibrary, Google Books. `Enricher` wraps a per-host `RateLimiter` + the SQLite cache. → `EnrichedMeta` |
| `pipeline.py` | Orchestration: `run_pipeline()` threads books through detect→extract→verify→fix-cascade, parallelised with a `ThreadPoolExecutor`. `_process_book()` is the per-book state machine. |
| `llm.py` | Z.AI LLM provider (OpenAI-compatible) — the last-resort fixer. `LeakyBucket` rate smoother + global 429 cooldown; `reconcile_loop` (Flash→feedback→final); tolerant JSON parsing; `get_provider` = the provider factory (`BMF_LLM_PROVIDER`: auto / zai / antigravity / mock / off). → `ReconciledMeta` |
| `acp.py` | `AntigravityAcpProvider` — a Google Antigravity subscription as the loop's FAST tier via the Agent Client Protocol (`BMF_ANTIGRAVITY_CMD`, the official `agy_acp_server.par`; any ACP agent works). One agent subprocess per connection, fresh session per book, tools denied; transport failures retry once, three in a row disable the fast tier; Z.AI (when configured) stays the paid fallback. → `ReconciledMeta` |
| `review_writer.py` | Streaming `review.yaml` writer: a queue + writer thread appends one YAML document per finished book (Unix-pipe style). `.bak` carry-over preserves prior user decisions. |
| `review.py` | Parse `review.yaml` (multi-doc stream + legacy single-list) → review entries with `action`. |
| `writers.py` | Atomic writers for `metadata.json` + `metadata.opf` (`.tmp` + `os.replace`, `.bak` history). |
| `mover.py` | the move/merge engine used by apply's placement: clean/verified books to the pattern path, unresolved to `needfix/`, dead records to `needfix/empty/`. |
| `epubgen.py` | `bmf epubgen`: generate missing `.epub` from the best sibling format (calibre `ebook-convert` → `pandoc`). |
| `crosscheck.py` | `bmf crosscheck`: verify multi-format folders hold the *same* book; quarantine rogue format files into isolated `needfix/` folders. |
| `covers.py` | Detect generated (Calibre placeholder) covers by pixel analysis; download real covers from enricher `cover_url`. C11 + MISSING_COVER. |
| `cli.py` | `click` CLI: `scan`, `report`, `analyze`, `apply`, `epubgen`, `crosscheck`, `gui` (plus a deprecation-stub `organize`). Thin layer over the modules above. |
| `config.py` | `Config` dataclass + `.env` walk-up loader. Resolution: CLI flag > env var > `.env` > default. |

## Core data models

```
BookMeta        what the library says now (authors, title, isbn, year, ...)
                normalized: authors=[..], isbn=digits-only-validated, year=int
                + provenance (source: json|opf|path) + encoding_repaired flags

Diagnosis       one detector's verdict on a BookMeta
                category (C1..C11 / OK / VERIFY_FAIL), reason, Confidence,
                Verdict (OK|VERIFIED|AUTO_FIXABLE|NEEDS_REVIEW|UNFIXABLE),
                proposed{...}, + .additional[] (other rules that also matched)

ExtractedMeta   what the book FILE actually contains
                embedded meta + first_page_text + broader_text + isbn_from_text
                (the independent signal the verifier trusts)

EnrichedMeta    a proposed fix from the cascade (online or LLM)
                + source ("openlibrary" / "databazeknih" / "llm:flash" / ...)
                + identity_confirmed (set when verify agrees)

ReconciledMeta  raw LLM output (title, authors, isbn, ..., confidence, reasoning)
```

A book flows `BookMeta → Diagnosis → (ExtractedMeta) → (EnrichedMeta) → review`.
`Verdict` decides where it lands: `OK/VERIFIED` books are eligible for
apply's placement; everything else is `NEEDS_REVIEW` and lands in
`review.yaml`. `bmf apply` also places every applied book (the former
`bmf organize`): clean/`verified` → the pattern path, unresolved →
`needfix/`, dead records → `needfix/empty/`.

## The fix cascade (cheap first)

`pipeline._process_book` tries to recover correct metadata for each
`NEEDS_REVIEW` book in cost order so the LLM is reached only as a last resort:

1. **Offline page-text mining** (`text_meta`) — title/authors/ISBN/year/publisher
   mined from the first-page text already extracted for verification. No network.
2. **Online by ISBN** (text-mined > embedded) — OpenLibrary + Google Books.
3. **Online by title + author** — this is the path that reaches
   **databazeknih.cz**, the strongest CZ/SK source.
4. **Embedded-OPF compare** (weakest — calibre may have overwritten the OPF).
5. **LLM fallback** (`llm.reconcile_loop`) — only when 1–4 all miss.

The LLM is gated on the book having *usable* first-page text (the #1 cost
saver: image-only title pages are skipped, never sent to the API).

## Concurrency model

`run_pipeline()` processes books in a `ThreadPoolExecutor` (`--workers`,
default 10). Per-book work is I/O-bound (content extraction, HTTP lookups, LLM
calls), so threads scale well. The shared objects are thread-safe:

- **openai client** — internal `httpx.Client` is thread-safe.
- **requests Session** (enrichers) — thread-safe for GETs.
- **SQLite Enricher cache** — `check_same_thread=False`, serialized by the
  connection.

Three layers keep the LLM call rate under Z.AI's dynamic RPM limit:

1. **Leaky-bucket smoother** (`LeakyBucket` in `llm.py`) — a count-per-time
   limiter shared across all workers **per endpoint URL** (with the flash
   endpoint split each endpoint drips — and adapts — on its own). With the
   default `--llm-burst 1` it is a
   pure even drip: exactly one call starts every `--llm-min-interval`
   (default 2.0 s ≈ 30 evenly-spaced RPM), no bunching. A burst >1 would let
   several calls fire in the same second — exactly what trips the dynamic RPM
   limit — so it stays 1 unless you have confirmed headroom.
2. **In-flight cap** (`MAX_INFLIGHT_CALLS` / `--llm-max-inflight`, default 3,
   plus a stricter `FLASH_INFLIGHT_CALLS = min(2, ·)` sub-cap for flash-family
   models) — the coding plan admits only ~5 concurrent requests per *account*
   (Z.AI publishes no exact numbers: the limits are tier-based and dynamic per
   the Devpack usage policy; measured: a 12-deep spike drew 7× 429/1302 while
   a 6-deep burst passed, and interactive clients — ZCode/chat — draw from
   the same ceiling). The bucket spaces call *starts* but not call *depth*:
   with 10 workers and multi-second reasoning calls, the fallback herd (flash
   dies → every worker pivots to the paid model at once) blew past the
   ceiling, and the storms also surfaced as **false 1113** "insufficient
   balance" with quota clearly left (Z.AI's own FAQ acknowledges 1113 firing
   on a purchased package). The global cap is a `_InflightGate` — a semaphore
   with runtime-resizable capacity — because external clients on the same
   plan are invisible: each 1302/false-1113 yields one slot (floor 1) and
   `INFLIGHT_RECOVER_SUCCESSES` clean 200s earn it back, the depth
   counterpart of the adaptive drip. The semaphores are held only around the
   HTTP request itself (cooldown waits and bucket acquires stay outside, or a
   parked fleet would deadlock on slots instead of waiting on the clock);
   flash calls acquire the flash semaphore inside the global one (fixed
   order), so a flash wave cannot squeeze the paid fallback out of the global
   slots — workers queue locally instead of being rejected. The HTTP layer is
   one shared pooled `httpx.Client` for the whole run: keep-alive expiry 60 s
   (outlives the 30 s balance pauses — httpx's 5 s default would force a TLS
   re-handshake after every pause), the pool sized to cap+2, and a finite
   read timeout (180 s, generous for max_tokens=8000 reasoning) so a hung
   call cannot squat a scarce slot for the SDK's 600 s default.
3. **Global 429/1302 cooldown** (circuit breaker) — Z.AI's free tier has a
   cascade bug: when *one* model gets a 429, the others (incl. the paid
   fallback) get throttled too. So when *any* worker sees a 1302 `Rate limit
   reached for requests`, a shared cooldown deadline is set that *all*
   threads wait on before their next call (`--llm-rate-limit-base` default
   5 s, escalating 5/10/20/…, honouring the server's `Retry-After`, capped at
   `--llm-rate-limit-max` default 60 s). One 429 parks the whole fleet
   instead of every worker hammering and 429-ing. The sub-code dispatch
   matters because Z.AI also returns 429 for **1305** "The service may be
   temporarily overloaded" (server-side capacity, chronic on the free flash
   models): that one is retried shortly, interval-spaced with a bounded
   budget, WITHOUT arming the fleet cooldown — treating it as 1302 used to
   turn transient overloads into permanent 60 s lockouts — and then falls
   back to the paid model. A fleet-wide streak of consecutive 1305/1113
   rejections on one model (reset by any 200 on it) pauses it — ~3 min for
   1305 capacity waves (`OVERLOAD_PAUSE_SEC`), ~30 s for 1113 bursts
   (`BALANCE_PAUSE_SEC`, usually hitting the last working model) — so a
   saturated free pool costs a few probing requests instead of eating the
   shared RPM drip book after book. The drip itself is adaptive
   (1302/1113 stretch it, successes shrink it back toward the floor), and
   when every model is paused at once the LLM stage is a quiet no-op with
   a once-a-minute idle notice.
   **1308** "Usage limit reached" disables just that
   model for the rest of the run. The openai client is built with
   `max_retries=0` so every 429 surfaces here for classification instead of
   being silently absorbed by SDK retries outside the bucket.

See [how-to/llm.md → Tuning the LLM rate limit](how-to/llm.md#tuning-the-llm-rate-limit)
for practical guidance.

## Caching

A SQLite database (`bmf_cache.db`, `--no-cache` to bypass) caches two things:

- **library traversal** — re-parsing `metadata.json`/`.opf` for unchanged
  folders is skipped (mtime-based), so repeated `report`/`analyze` runs are fast.
- **enricher lookups** — positive *and* negative results are cached. A negative
  (`__NOT_FOUND__`) entry expires after `BMF_ENRICH_NEGATIVE_TTL` (default 7
  days) so a transient failure or a pre-fix identity gets retried.

## File formats

`extractors.py` handles `.epub`, `.pdf`, `.pdb`, `.mobi`, `.prc`, `.doc`,
`.txt`, `.rtf`, `.html`, and comic archives `.cbz/.cbr/.cb7` (via
`ComicInfo.xml`). `epubgen.py` can generate a missing `.epub` from any of the
text sibling formats. Optional external tools extend coverage: `pdftotext`/
`pdfinfo` (poppler), `ebook-convert`/`ebook-meta` (calibre), `pandoc`,
`tesseract` (OCR for scanned PDFs / image-only title pages).

## Atomicity & safety

- **Writes** (`writers.py`) use `.tmp` + `os.replace()` with a `.bak` history —
  a crash mid-write never leaves a half-written manifest.
- **`review.yaml`** (`review_writer.py`) is appended one document at a time;
  Ctrl-C is safe — everything flushed so far is already on disk, and a `.bak`
  taken at start is kept if the run is interrupted so prior decisions survive.
- **Every command is a dry-run by default** (`apply`, `epubgen`,
  `crosscheck`); `--apply` is required to mutate the filesystem.
