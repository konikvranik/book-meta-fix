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
  verifier.py      compare DB meta vs BOOK CONTENT (do NOT trust embedded) + identity primitives
  classify.py      SINGLE source of truth for OK/needfix disposition (detect + identity gate + opt-in OK-audit)
  enrichers.py     databazeknih.cz / legie.info / OpenLibrary / Google Books → EnrichedMeta
  pipeline.py      orchestration: ThreadPoolExecutor, per-book state machine
  llm.py           Z.AI provider: LeakyBucket + global 429 cooldown + reconcile_loop + tolerant JSON
  review_writer.py streaming review.yaml writer (queue + writer thread)
  review.py        parse review.yaml (multi-doc + legacy list)
  writers.py       atomic metadata.json/.opf writers
  mover.py         bmf organize
  epubgen.py       bmf epubgen
  crosscheck.py    bmf crosscheck
  covers.py        generated-cover detection (pixel math) + replacement & in-book extraction fallback
  gui.py           bmf gui — keyboard-first Tkinter review.yaml editor (no new writer: loads raw
                  entry dicts, writes via review._header + review._render_entry; scrollable detail
                  column, Tab-trap bindtag, per-format embedded covers, Ctrl+G double-decode recode,
                  clickable path link / list double-click = open folder via open_folder_in_manager)
  cli.py           click commands: scan, report, analyze, apply, organize, epubgen, crosscheck, gui
```

## Non-obvious gotchas

- **The verifier does NOT trust embedded metadata.** Calibre wrote the
  (possibly wrong) DB metadata back into the file at import time, so the title
  inside `content.opf`/PDF Info is *not* independent evidence. Only the book's
  actual text (ISBN scanned from content, fuzzy title on first-page text) can
  confirm a record. Don't "fix" the verifier to trust embedded OPF.
- **Source of truth = `metadata.json`**, not `.opf`. Writers update *both*.
  Readers prefer `metadata.json`, fall back to `.opf`.
- **Classification is unified in `classify.py`.** The rule "an identified
  MISSING_* book (author+title confirmed against the content, no co-occurring
  `NEEDS_REVIEW`) is acceptable, not broken" lives EXACTLY ONCE in
  `classify.is_acceptable_missing` + `classify.classify`. `report`, `organize`,
  `epubgen`, and the pipeline's accept-missing gate all call it, so the OK /
  needfix disposition cannot drift between commands. `organize` routes an
  identified MISSING_* book to the OK path (it is NOT broken); `report` does
  not count it as broken. The identity primitives (`acquire_identity`,
  `IdentityResult`, `safe_extract`, `has_usable_text`) live in `verifier.py`.
  Per the agreed identification policy, author+title confirmed against the
  content is sufficient; the year is never required for identity.
- **Identity-confirmed MISSING_* books are auto-accepted** (`pipeline.py`
  `_process_book`, gated by `accept_missing_if_identified`, default on). When
  a `MISSING_ISBN`/`MISSING_YEAR`/`MISSING_COVER` book has its author+title
  confirmed against the book's content (`acquire_identity`) and no enricher
  recovered the field, the pipeline stamps a minimal
  `EnrichedMeta(identity_confirmed=True, source="content")`. `review_writer`
  then pre-fills `action: accept` (its identity_confirmed-no-proposal branch),
  and `bmf apply` prunes the entry — a safe no-op for MISSING_ISBN/MISSING_YEAR
  (`_apply_action` skips when `proposed` is empty). For MISSING_COVER,
  `_apply_action` additionally attempts cover recovery (enricher `cover_url`,
  then extracting the cover from the book file via
  `covers.recover_cover_from_book`, which rejects generated placeholders) even
  with empty `proposed`. The verdict stays `AUTO_FIXABLE` (in the review
  inclusion set); a MISSING_ISBN/YEAR book reappears as auto-accept on the next
  `analyze` because the detector re-fires (the field is genuinely still
  missing) — that is intended: zero manual work, bulk-pruned by `apply`. A
  MISSING_COVER book whose cover was recovered does NOT re-fire (cover.jpg is
  now present); one whose cover could not be recovered still re-fires. A co-occurring
  `NEEDS_REVIEW` diagnosis (e.g. C11 generated cover) blocks the auto-accept
  and keeps the book in review. If any enricher/text_meta DID return data,
  `enriched` is already set and those fields are proposed + applied normally —
  this path fires only when nothing was recovered.
- **`organize` does NOT content-verify OK books by default.** Consistent with
  `report`/`analyze`, OK books are routed on the detector verdict alone; the
  MISMATCH audit is opt-in via `--verify-ok` (and strict on UNCERTAIN unless
  `--no-strict-verify`). All three commands share `--accept-missing` (default
  on) and `--verify-ok` (default off). `--no-accept-missing` disables the
  identity gate → pure detector verdict, no content reads (the historic fast
  `report`).
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
  appended as each finishes. `.bak` carry-over preserves prior user decisions,
  matched by the book's **uuid** (NOT calibre_id) so a decision survives an
  `organize` folder move. `bmf apply` reads both the multi-doc and legacy
  single-list forms.
- **The `keep` action is accept-but-retain.** `action: keep` applies the
  proposed fields + cover exactly like `accept` (reuses the `_apply_action`
  accept branch) but is **NOT pruned** from `review.yaml` after a WRITE apply
  (`apply_review` skips adding its uuid to `succeeded_uuids`; counted in
  `summary["kept"]`). The next `bmf analyze` **skips** the book entirely:
  `run_pipeline(skip_uuids=...)` filters it out before any detect/extract/
  enrich work, and its review entry is carried over byte-for-byte by
  `ReviewWriter.finish()`. So a kept book is frozen — visible in review.yaml
  but never re-processed — until the user flips its action back to `null`,
  which re-includes it. The skip set is built by `ReviewWriter.keep_uuids()`
  from the prior `review.yaml` and passed in by the `analyze` CLI.
- **The book `uuid` is the unified identity** (`models.BookMeta.uuid`): it lives
  in `metadata.json` (source of truth), mirrored to `metadata.opf`, and is the
  single key for **carry-over** (`.bak` match), **pruning** (`apply` drops
  applied entries by uuid), and the **cache PK** (`library.Cache`, looked up by
  path but keyed by uuid so a row follows a moved book). `calibre_id` is now
  informational only (parsed from the folder name). The uuid is **lazy-minted**
  the first time a book is needed — `scan_library` calls `writers.ensure_uuid`
  on a cache miss, `apply` mints before writing — via a *minimal, key-preserving
  inject* (loads the manifest, sets only `uuid`). `write_book_meta` is itself a
  **surgical merge** onto the existing `metadata.json`: it overlays only the
  fields bmf manages and preserves ABS-owned fields it does not model
  (narrators, chapters, asin, explicit, abridged, publishedDate, ...); never
  rebuild the manifest from a fixed dict (that nulled those fields on every
  apply). `metadata.opf` is regenerated wholesale (a derived mirror). Side
  effect: the first scan
  after this change writes a uuid into every uuid-less book's `metadata.json`
  (identity augmentation, not a content mutation; deliberately not dry-run
  gated). A genuinely legacy `review.yaml` whose entries predate uuids cannot be
  matched and is re-decided fresh (clean break).
- **GUI conventions** (`gui.py`): the detail side is ONE scrollable canvas (no
  Notebook) — covers and content sit below the fields. ``Tab`` cycles ONLY
  the editable fields; this is implemented by prepending a custom bindtag
  FIRST in every focusable widget's bindtags, because ``bind_all`` binds the
  "all" tag which runs LAST and loses to Tk's default focus traversal (the
  original bug). Dynamically created widgets (format radios, embedded-cover
  checkboxes) must be re-trapped via ``_trap_subtree``. ``Ctrl+A`` is
  rebound per Entry (X11's default is "home", not select-all). Wheel routing
  (``_on_wheel``): the widget under the pointer scrolls first (its class
  binding already ran — "all" runs last); the form canvas takes over only at
  that widget's edge, and ``_scroll_canvas`` hard-refuses to scroll while the
  form fits the viewport (the form is FIXED, never drifts). Cover previews
  sit in fixed-HEIGHT slots whose WIDTH is synced per row
  (``_sync_cover_slots``) — a purely fixed width overflows a narrow pane and
  pack then squeezes the trailing cells out of shape. Per-format EMBEDDED
  covers are previewed via ``gui.embedded_cover_thumb`` — no
  generated-placeholder gate there (unlike ``covers.recover_cover_from_book``)
  because the point is to SEE a calibre placeholder. For EPUB the preview
  reads the zip directly (``covers.epub_cover_image``): calibre's
  ``ebook-meta --get-cover`` renders page 1 as a "default cover" even for a
  genuinely coverless EPUB, which would fake a cover right after a strip.
  Their checkboxes STRIP the embedded cover via
  ``covers.strip_cover_from_book`` — surgical zip+OPF rewrite, the e-book
  file itself is never deleted; EPUB only (MOBI/AZW3/PRC covers live in
  binary EXTH headers with no safe removal path, so their checkboxes are
  disabled). Clicking anywhere on a cover toggles its checkbox (the tiny
  overlay square alone is a hard target; a click on the checkbox widget
  itself goes to the checkbox, so the label binding never double-toggles).
  Cover detection is defined once in ``covers._opf_cover_parts``
  and shared by the strip and the probe. The content view's
  ``Ctrl+G`` repair uses ``encoding.recode`` on the manually picked pair
  ("přečteno jako" = the codec the text was wrongly read through,
  "skutečně je" = the real encoding of the bytes) — a DIFFERENT
  corruption than the single mojibake ``readers`` repairs (utf-8 bytes
  mis-decoded twice through a single-byte codec; often only PART of the text,
  which is why the repair decodes byte-wise via ``_mixed_utf8_decode``
  instead of a whole-string round-trip); keep the two paths separate. A pair
  that cannot run is diagnosed by ``encoding.recode_failure_reason`` and the
  hint offers the reversed direction as a click (users pick the direction
  backwards far more often than the codecs are wrong: utf-8→cp1250 chokes on
  cp1250's five undefined byte positions, which common Czech chars hit —
  Á = C3 81, ‘ = E2 80 98). U+FFFD (a byte lost to an earlier
  ``errors="replace"`` decode) is tolerated everywhere via
  ``_for_lost_bytes`` (FFFD→SUB, mapped back in the result so lost
  positions stay visible) — one destroyed byte must not grey out the
  repair. TWO-layer chains exist in the wild (real sample: cp1250 CZ text
  mis-read as cp1251 → Cyrillic look-alikes → saved utf-8 → mis-read as
  cp1250 → saved utf-8; one recode layer only reaches the Cyrillic middle):
  ``encoding.repair_chain`` searches the second layer (encode → utf-8 →
  ``_encode_dropping`` → decode, budgeted orphan-drop) and the GUI applies
  it automatically, naming the chain in the hint; it never fires when the
  plain single-layer repair already succeeds.
- **GUI smoke harnesses under Xvfb must run their checks inside a real
  ``mainloop()``** (``root.after(60, check); root.mainloop()``), never an
  ``update()``-polling loop. This box has a threaded Tcl build: a worker
  thread's ``root.after()`` raises ``RuntimeError: main thread is not in main
  loop`` while no main loop is dispatching, and ``_after`` deliberately
  swallows that — so with plain ``update()`` polling every async load
  (content, thumbnails) silently never applies and the smoke fails in
  confusing, load-dependent ways.
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
