# AGENTS.md — guide for AI agents working on this codebase

Read this before editing. It captures the conventions, layout, and
non-obvious gotchas that matter when changing this code. Companion docs:
[docs/architecture.md](docs/architecture.md),
[docs/concepts.md](docs/concepts.md),
[docs/how-to.md](docs/how-to.md),
[README.md](README.md).

## What this project is

`book-meta-fix` (`bmf`) detects and repairs corrupt metadata in a
Calibre-style ebook library (~5,000 CZ/SK books). Calibre mis-classified many
records: swapped author/title, filename-as-title, mojibake, translators listed
as authors, generated placeholder covers. **Source of truth is `metadata.json`**
(Audiobookshelf manifest); on write both `metadata.json` and `metadata.opf`
are updated atomically. Every change is gated by a human-in-the-loop
`review.yaml` unless it is a high-confidence auto-fix.

## Tech stack

- Python **>=3.10**. `from __future__ import annotations` is used everywhere.
- `click` (CLI), `rich` (terminal output), `PyYAML`, `rapidfuzz` (fuzzy match),
  `Pillow` (cover pixel analysis), `lxml` (OPF/HTML parsing), `requests`.
- Optional extras (`pyproject.toml`): `[llm]` = `openai` + `json-repair`
  (Z.AI GLM client + invalid-JSON salvage); `[pdf]` = `pypdf`.
- External tools (optional): poppler (`pdftotext`/`pdfinfo`), calibre
  (`ebook-convert`/`ebook-meta`), `pandoc`, `tesseract` (OCR).

## Code conventions (important)

- **Indent with TABS, not spaces.** Both `src/` and `tests/` use tabs
  throughout — match the surrounding code exactly. (The `ruff` config selects
  `W`/`E`, so `make lint` reports thousands of pre-existing `W191`/`E101`
  tab warnings; this is known noise, not a regression. The meaningful rule
  sets are `F` (pyflakes), `I` (imports), `UP`, `B`.)
- **Dataclasses for models** (`models.py`): `BookMeta`, `Diagnosis`, `Book`,
  `Verdict`/`Confidence` enums. These are the shared vocabulary — extend them
  rather than passing ad-hoc dicts across modules.
- **Type hints everywhere**; keep them accurate when you change a signature.
- **Docstrings explain *why***, especially around the rate limiter, the
  verifier, and the JSON salvage — the reasoning is load-bearing.
- **No network in tests.** Online sources, the LLM, and HTTP are stubbed or
  mocked. Tests are per-module: `tests/test_<module>.py`.

## Module layout

```
src/book_meta_fix/
  models.py        core dataclasses + Verdict/Confidence enums (shared vocab)
  config.py        Config dataclass + .env walk-up loader
  readers.py       parse metadata.json (primary) / metadata.opf (fallback) / path
  library.py       traverse library tree + SQLite cache
  detectors.py     rules C1–C11 → Diagnosis
  extractors.py    per-format content extraction → ExtractedMeta
  text_meta.py     offline page-text mining (1st stage of fix cascade)
  encoding.py      mojibake detection + repair
  isbn.py          ISBN extract/canonicalize/validate
  verifier.py      compare DB meta vs BOOK CONTENT (do NOT trust embedded)
  enrichers.py     databazeknih.cz / legie.info / OpenLibrary / Google Books → EnrichedMeta
  pipeline.py      orchestration: ThreadPoolExecutor, per-book state machine
  llm.py           Z.AI provider: LeakyBucket + global 429 cooldown + reconcile_loop + tolerant JSON
  review_writer.py streaming review.yaml writer (queue + writer thread)
  review.py        parse review.yaml (multi-doc + legacy list)
  writers.py       atomic metadata.json/.opf writers
  mover.py         bmf organize
  epubgen.py       bmf epubgen
  crosscheck.py    bmf crosscheck
  covers.py        generated-cover detection (pixel math) + replacement
  cli.py           click commands: scan, report, analyze, apply, organize, epubgen, crosscheck
```

## Non-obvious gotchas

- **The verifier does NOT trust embedded metadata.** Calibre wrote the
  (possibly wrong) DB metadata back into the file at import time, so the title
  inside `content.opf`/PDF Info is *not* independent evidence. Only the book's
  actual text (ISBN scanned from content, fuzzy title on first-page text) can
  confirm a record. Don't "fix" the verifier to trust embedded OPF.
- **Source of truth = `metadata.json`**, not `.opf`. Writers update *both*.
  Readers prefer `metadata.json`, fall back to `.opf`.
- **LLM rate limiting is two-layered** (`llm.py`): a `LeakyBucket` smoother
  (constant aggregate RPM) **and** a global 429 cooldown / circuit breaker.
  Z.AI's free tier has a cascade bug — when one model 429s, all models get
  throttled — so when *any* worker sees a 429, *all* workers pause
  (`_wait_cooldown` → `_on_rate_limited` escalating, honouring `Retry-After`,
  capped). Do not remove the global cooldown thinking the bucket is enough.
- **LLM JSON is salvaged, not rejected.** `_parse_llm_json` tries the cheap
  built-in sanitizer first, then `json-repair` (the `[llm]` extra) recovers
  unescaped quotes / control chars / truncation. The `json_repair` import is
  graceful (None if absent) — keep it optional.
- **`review.yaml` streams** (`review_writer.py`): one YAML document per book,
  appended as each finishes. `.bak` carry-over preserves prior user decisions.
  `bmf apply` reads both the multi-doc and legacy single-list forms.
- **Every command runs an internal scan** via the SQLite cache — `bmf scan` is
  only for summary stats, not a prerequisite.
- **Every mutating command is dry-run by default** (`--apply` to write).

## Working with the code

- Run tests: `make test` (or `.venv/bin/pytest -q`).
- Run lint: `make lint` — expect the pre-existing `W191`/`E101` tab noise.
  To check only meaningful rules on files you changed:
  `.venv/bin/ruff check --select F,I,UP,B <files>`.
- Install: `make dev-install` (= `pip install -e ".[pdf,llm,dev]"`). Re-run if
  you change `pyproject.toml` deps.
- Experiment scripts live in `scripts/` (e.g. `llm_experiment.py` to measure
  model quality/cost).

## Adding things

- **A new detector rule** → add a function in `detectors.py` returning
  `Diagnosis | None`, register it in the priority order, add a `Cxx` code to
  the catalog (`docs/corruption-catalog.md` + README table), and a test in
  `tests/test_detectors.py`.
- **A new file format** → add an extractor branch in `extractors.py` (embedded
  meta + first-page text) and an `epubgen` source-preference entry if relevant.
- **A new enricher source** → follow `enrichers.py` (return `EnrichedMeta |
  None`, cache via the shared `Enricher`, rate-limit per host).
- **A new config knob** → add a field to `Config` (`config.py`), load it in
  `from_env()`, add a `@click.option` in `cli.py`, wire it through, document in
  `.env.example` + README. The LLM knobs fan out through `get_provider` in
  `llm.py`.

## Commit etiquette

- Keep changes focused; don't reformat untouched code (the tab style is
  intentional — mass-converting would create noise).
- Tests must pass (`make test`). Prefer adding a test for any behavioural
  change, especially in `llm.py`, `verifier.py`, or `detectors.py` where the
  logic is subtle.
- Don't commit real `ZAI_API_KEY` values, `.env`, `bmf_cache.db`,
  `review.yaml`, or `*.bak` — they're in `.gitignore` for a reason.
