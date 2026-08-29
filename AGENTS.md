# AGENTS.md — guide for AI agents working on this codebase

Read this before editing. It captures the conventions, layout, and
non-obvious gotchas that matter when changing this code. Companion docs:
[docs/architecture.md](docs/architecture.md),
[docs/concepts.md](docs/concepts.md),
[docs/how-to/index.md](docs/how-to/index.md),
[README.md](README.md).

**Docs are bilingual**: English originals (`README.md`, `docs/`) have
Czech mirrors (`README.cs.md`, `docs/cs/` — same tree structure). When
you change a documented page, update BOTH language versions (including
in-page/TOC anchors, which differ once headings are translated) and keep
the `**English** | [Čeština](…)` switcher lines intact.

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
- **Localization** (`i18n.py`, gettext): user-facing strings (CLI messages,
  option help, GUI labels, the review.yaml header comment) go through
  `_()` from `book_meta_fix.i18n`. **msgids are English** — English is also
  the fallback (no en catalog exists). Czech lives in
  `src/book_meta_fix/locales/cs/LC_MESSAGES/bmf.po`; the compiled `.mo` is
  committed so installs/tests don't need pybabel. Language resolution:
  `--lang` flag > `BMF_LANGUAGE` (env/.env) > locale autodetect (`cs*` → cs,
  else en). After adding/changing `_("...")` strings run `make i18n-extract`
  (needs pybabel), fill in Czech translations in the .po, then
  `make i18n-compile` and commit both .po and .mo. Known limitation: click
  help texts are evaluated at import time, so `--lang` affects runtime
  messages only, not already-built help texts (BMF_LANGUAGE does, because
  cli.py calls `init_language()` at import). Tests assert English msgids;
  `tests/conftest.py` pins `BMF_LANGUAGE=en` for the whole suite — only
  `tests/test_i18n.py` manages the catalog itself.

## Module layout

```
src/book_meta_fix/
  models.py        core dataclasses + Verdict/Confidence enums (shared vocab)
  config.py        Config dataclass + .env walk-up loader
  i18n.py          gettext wrapper: _() with English msgids, cs catalog, locale detect
  readers.py       parse metadata.json (primary) / metadata.opf (fallback) / path
  library.py       traverse library tree + SQLite cache
  detectors.py     rules C1–C13 → Diagnosis (C13 = location mismatch; exists only
                   when detect() gets library_root/pattern kwargs)
  extractors.py    per-format content extraction → ExtractedMeta
  text_meta.py     offline page-text mining (1st stage of fix cascade)
  encoding.py      mojibake detection + repair
  isbn.py          ISBN extract/canonicalize/validate
  verifier.py      compare DB meta vs BOOK CONTENT (do NOT trust embedded) + identity primitives
  classify.py      disposition for report/epubgen (detect + identity gate + opt-in OK-audit)
  enrichers.py     databazeknih.cz / legie.info / OpenLibrary / Google Books → EnrichedMeta
  pipeline.py      orchestration: ThreadPoolExecutor, per-book state machine +
                   apply_review (metadata writes + PLACEMENT — the former organize)
  llm.py           Z.AI provider: LeakyBucket + global 429 cooldown + reconcile_loop + tolerant JSON
  review_writer.py streaming review.yaml writer (queue + writer thread)
  review.py        parse review.yaml (multi-doc + legacy list) + update_paths
  writers.py       atomic metadata.json/.opf writers + ensure_uuid + clear_verified
  mover.py         move/merge engine used by apply's placement (organize fn kept for tests)
  epubgen.py       bmf epubgen
  crosscheck.py    bmf crosscheck
  covers.py        generated-cover detection (pixel math) + replacement & in-book extraction fallback
                  + embedded-cover strip (EPUB zip+OPF surgery) + strip_generated_covers
                  (the per-folder engine of `bmf strip-covers`: sidecar → .bak, embedded EPUB probe+strip)
  gui.py           bmf gui — keyboard-first Tkinter review.yaml editor (no new writer: loads raw
                  entry dicts, writes via review._header + review._render_entry; scrollable detail
                  column, Tab-trap bindtag, per-format embedded covers, Ctrl+G double-decode recode,
                  clickable path link / list double-click = open folder via open_folder_in_manager;
                  Verified checkbox (Ctrl+O) = the persistent user-OK mark)
  cli.py           click commands: scan, report, analyze, apply, epubgen, crosscheck,
                  strip-covers, gui
                  (organize is a deprecation stub — placement lives in apply)
```

## Non-obvious gotchas

- **The verifier does NOT trust embedded metadata.** Calibre wrote the
  (possibly wrong) DB metadata back into the file at import time, so the title
  inside `content.opf`/PDF Info is *not* independent evidence. Only the book's
  actual text (ISBN scanned from content, fuzzy title on first-page text) can
  confirm a record. Don't "fix" the verifier to trust embedded OPF.
- **Source of truth = `metadata.json`**, not `.opf`. Writers update *both*.
  Readers prefer `metadata.json`, fall back to `.opf`.
- **Field coverage is end-to-end**: whatever the enrichers/LLM return must
  reach disk. `_build_proposed` (`review.py`) proposes it, `_apply_action`
  (`pipeline.py`) maps it onto `BookMeta`, writers emit it in BOTH
  metadata.json and metadata.opf (series as `calibre:series` /
  `calibre:series_index`, genres+tags as `dc:subject`). When adding a field,
  wire all three links — historically series/language/description were
  fetched but silently dropped at one of them. Series travels through
  review.yaml as flat strings and is packed into the ABS
  `[{"name", "index"}]` list at apply; the wild stored shapes (plain string
  `"Name #N"`, `sequence` key) are normalised by `BookMeta.series_pair()` —
  the single accessor for GUI display, placement patterns and the OPF
  mirror. An edit with an emptied series name clears the series.
- **Classification is unified in `classify.py`.** The rule "an identified
  MISSING_* book (author+title confirmed against the content, no co-occurring
  `NEEDS_REVIEW`) is acceptable, not broken" lives EXACTLY ONCE in
  `classify.is_acceptable_missing` + `classify.classify`. `report` and
  `epubgen` call it, the pipeline's accept-missing gate reuses
  `is_acceptable_missing`, and apply's placement routing
  (`pipeline._placement_target`) reuses it too — so the OK / needfix
  disposition cannot drift between commands. `report` does not count an
  identified MISSING_* book as broken. The identity primitives
  (`acquire_identity`, `IdentityResult`, `safe_extract`, `has_usable_text`)
  live in `verifier.py`. Per the agreed identification policy, author+title
  confirmed against the content is sufficient; the year is never required
  for identity.
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
- **Placement lives in `apply`, not a separate command** (the former
  `bmf organize` is a deprecation stub). After writing an entry's metadata,
  `apply_review` routes the folder via `_placement_target` + `_place_applied_book`
  (`pipeline.py`, using `mover` primitives): `verified` or clean or
  acceptable-missing → the pattern target path; unresolved → `needfix/`
  (prefix stripped, so a resolved book moves back OUT). Deliberately
  metadata-only — the plain detector WITHOUT the C13 rule (the move itself
  resolves C13) and with NO content reads/identity gate; that expensive work
  belongs to analyze, which is why apply is fast where organize was slow.
  The destination is RECOMPUTED from the final metadata (user may have fixed
  author/title — the review `location` proposal is informational only). An
  occupied target holding the same work is merged (our approved metadata is
  the `merge_meta` base; the occupant fills gaps); a different work gets
  `(dup N)`. A moved `keep` entry has its `path` refreshed via
  `review.update_paths`, or the next apply would fail with "folder not
  found". `--no-place` (apply_review `place=False`) skips moving entirely.
- **The `verified` flag is the persistent user-OK mark.** A review entry may
  carry `verified: true` (GUI checkbox / `Ctrl+O`; orthogonal to `action`, so
  "accept + verified" = fix AND close in one pass). `apply_review` sets
  `meta.verified` and `write_book_meta` persists it into `metadata.json`
  ONLY — `_json_overlay` emits the key just when set (a constant false would
  pollute every manifest) and the OPF mirror never carries it (Calibre reads
  OPF only). ABS ignores unknown manifest keys; worst case an ABS rewrite
  drops the flag and the book simply re-enters review — fail-safe.
  `run_pipeline(skip_verified=True)` (analyze default) drops verified books
  right after the scan, before any detection. `bmf analyze --recheck-ok`
  clears the flag on disk (`writers.clear_verified` pops the key, keeps a
  `.bak`) and invalidates the cache rows so the scan re-reads them.
  Analyze also PRE-FILLS the flag: `ReviewWriter._projected_clean` applies
  the entry's own `proposed` through `_apply_fields` onto a shallow copy and
  re-detects — when the projected state is detector-clean (a proposed
  `cover_url` and C13 are credited), the entry is born `verified: true`, so
  a book the analyzer's proposal completes is fixed AND closed in one apply
  and never re-enters review. The relaxed twin `_identity_verified` (same
  place, runs only when the projection is NOT clean) closes an accepted
  entry whose FINAL identity is confirmed against the content AND an online
  source: `enriched.identity_confirmed` with `source` in `_ONLINE_SOURCES`
  (databazeknih/legie/openlibrary/google_books — the pipeline stamps the
  flag only after `acquire_identity` + an author-filtered/ISBN-anchored
  online hit; LLM answers and the content-only MISSING_* stamp do NOT
  count). `verifier.identity_agrees` checks the projected identity still
  agrees with the online record (an extracted/C1-swap title must not have
  overridden it), and the projected state may keep only benign leftovers
  (OK-verdict or MISSING_*); a NEEDS_REVIEW leftover or EMPTY_BOOK blocks
  the pre-fill. Counted as `verified_prefilled` in the analyze summary.
- **EMPTY_BOOK (dead record) is the FIRST rule and routes to
  `needfix/empty/`.** `rule_empty_book` fires when the folder holds only
  metadata sidecars / their `.bak`/`.tmp` backups / `cover.jpg` — no ebook
  file, no subdirectory, no other file. With the book gone, no other rule's
  verdict matters (that is why it runs before C6). The entry pre-fills
  `accept` (the move is mechanical); `_placement_target` checks
  `not meta.formats` BEFORE the verified/clean routing — a dead record goes
  to `needfix/empty/<relpath>` even when verified, and the prefix strip
  handles both `needfix/` and the nested `empty/` so re-runs are
  idempotent. Metadata stays untouched (nothing to verify against).
- **C13 (location mismatch) is opt-in per caller.** `detectors.location_rule`
  is appended by `detect()`/`detect_all()` ONLY when `library_root` is passed
  (analyze does; report/epubgen stay location-blind → their semantics are
  unchanged). Ordering matters: C13 runs BEFORE the enrichment rules, so a
  misplaced-but-otherwise-fine book gets C13 as PRIMARY — the cheap
  no-extraction path in `_process_book` (`is_needs_review` is false for a
  C13 primary) and a pre-filled `accept` in `review_writer` (C13-led with
  only benign extras: OK-verdict, MISSING_*, or a cover category C11/
  MISSING_COVER — apply's cover recovery runs for a cover diagnosis
  ANYWHERE in the entry's list, and refusing would freeze the book because
  C13 would shadow the cover problem as the primary on every run; the
  proposal must not change title/author. EMPTY_BOOK-led pre-fills
  unconditionally: with the book file gone every other rule fires on the
  leftover sidecar metadata and none of those verdicts matters). ONE
  exception to the cheap path: a C13-primary book carrying a cover extra
  (`cover_shadowed` in `_process_book`) IS extracted and enriched, so an
  enricher can propose `cover_url` in the same pass — the LLM/accept-missing
  gates still key on the primary, so a misplaced book never pays for an LLM
  call. C13 is
  also the ONE non-OK diagnosis promoted over an OK-verdict primary (a
  misplaced genuine anonym must not be masked by its whitelisted C9-OK).
  `_apply_fields` ignores the `location` key — it is not a metadata field.
- **`report`/`analyze` flags** (`--accept-missing` default on,
  `--verify-ok` default off, `--no-strict-verify`) are unchanged; analyze
  additionally takes `--pattern`/`--no-check-location`/`--recheck-ok`,
  apply takes `--pattern`/`--needfix-dir`/`--no-place`
  (env: `BMF_PATTERN`, `BMF_NEEDFIX_DIR`).
- **LLM rate limiting is layered, and the 429 SUB-CODE decides the
  reaction** (`llm.py`): a `LeakyBucket` smoother (constant aggregate RPM),
  **in-flight semaphores** (`MAX_INFLIGHT_CALLS` / `--llm-max-inflight` /
  `BMF_LLM_MAX_INFLIGHT`, default 3, plus a stricter `FLASH_INFLIGHT_CALLS =
  min(2, ·)` sub-cap for flash-family models), and a code-aware circuit
  breaker. **Endpoint split** (`ZAI_FLASH_BASE_URL`, empty = AUTO): when the
  primary is the coding endpoint, glm-4.x flash is routed to the PaaS
  endpoint — measured 2026-08-28, the two endpoints' ~5-request ceilings are
  INDEPENDENT (6 flash@PaaS + 6 glm-5.3@coding simultaneously left the
  coding side at its solo baseline) and a coding-plan key calls glm-4.x flash
  on PaaS for FREE (paid models 1113 there — no cash balance; glm-5.x flash
  is not served on PaaS, 400/1210, so `_endpoint_for` keeps every glm-5.x
  model on the primary). One pooled httpx client PER endpoint
  (`_client` / `_flash_client`); `off`/`0` disables the split. The ENTIRE
  rate machinery is per endpoint URL too — bucket, cascade cooldown,
  429 escalation and the adaptive in-flight gate each live in per-URL state
  (the primary's in the classic attributes, other URLs in `_ep_*` dicts;
  `_bucket_for`/`_gate_for`/`_wait_cooldown(url)`/`_on_rate_limited(url)`/
  `_on_success(url)` pick the right one), so pressure on the flash endpoint
  never throttles the primary and vice versa. Only the model-family flash
  sub-cap semaphore (`_flash_sem`) is cross-endpoint by design (it caps the
  flash MODEL herd wherever it runs). The
  semaphores exist because the coding plan admits only ~5
  concurrent requests per ACCOUNT — Z.AI publishes no exact numbers (the
  limits are tier-based, Max > Pro > Lite, and dynamic per
  docs.z.ai/devpack/usage-policy); ~5 is the measured ceiling of one Pro-tier
  evening (a 12-deep simultaneous spike drew 7× 429/1302 while serial calls
  and a 6-deep burst passed; interactive clients — ZCode/chat — draw from the
  same ceiling). The bucket spaces call STARTS but not DEPTH: with 10 workers
  and multi-second reasoning calls, the fallback herd (flash dies → everyone
  pivots to the paid model at once) blew past the ceiling, and the 1302
  storms + metering chaos also surfaced as FALSE 1113 "insufficient balance"
  with quota clearly left (docs.z.ai/devpack/faq officially acknowledges 1113
  firing on a purchased coding package; the evening "1113 waves" were this
  herd, not server-side metering outages). The flash sub-cap is the community
  signal that the free pool's concurrency can be as low as 1 plus the fact
  that a deep flash herd only feeds the 1305 storm; flash calls acquire the
  flash semaphore INSIDE the global one (fixed order) so a flash wave cannot
  squeeze the paid fallback out of the global slots. The GLOBAL gate is an
  `_InflightGate` (semaphore with runtime-resizable capacity): external
  clients on the same plan (ZCode/chat) are invisible to bmf, so their
  ceiling pressure is OBSERVED — each 1302 / streak-tripped false 1113
  yields one slot (floor 1, log `in-flight cap N -> N-1`; debounced to at
  most one yield per _call, so a retry cascade cannot shrink repeatedly) and
  INFLIGHT_RECOVER_SUCCESSES clean 200s earn it back; it is the depth
  counterpart of the adaptive drip. Both semaphores are held
  only around the HTTP request itself — cooldown waits and bucket acquires
  stay outside them, or a parked fleet would deadlock on slots instead of
  waiting on the clock. The HTTP layer is ONE shared pooled httpx.Client for
  the whole run: keep-alive expiry 60 s (httpx's 5 s default would force a
  TLS re-handshake after every 30 s balance pause), pool sized cap+2, read
  timeout 180 s (the SDK's 600 s default would let one hung call squat a
  scarce in-flight slot for ten minutes). Z.AI returns HTTP 429 for three
  different conditions (`docs.z.ai/api-reference/api-code`): **1302**
  "Rate limit reached for requests" — OUR rate tripped the RPM window →
  the escalating global cooldown (`_wait_cooldown` → `_on_rate_limited`,
  honouring `Retry-After`, capped); Z.AI's free tier also cascade-throttles
  every model when one 429s, which is why the cooldown is fleet-wide. Do not
  remove it thinking the bucket is enough. **1305** "The service may be
  temporarily overloaded" — SERVER-side capacity (chronically frequent on
  the free flash models, evenings ~60 % of calls): must NOT arm the global
  cooldown (treating it as 1302 turned transient overloads into permanent
  60 s fleet lockouts); `_call` retries it shortly (interval-spaced via the
  bucket, small `OVERLOAD_RETRIES` budget) and then falls back to the paid
  model. The throughput keystone is the fleet-wide rejection STREAK:
  `OVERLOAD_STREAK` consecutive 1305/1113 rejections on one model across
  ALL calls/workers (reset by any HTTP 200 on it) pause it for
  `OVERLOAD_PAUSE_SEC` (3 min) — a half-dead flash (a 200 every few
  rejections, so its calls keep "succeeding" and per-call budgets never
  trip anything) would otherwise silently eat the shared RPM drip that the
  paid fallback needs; with the streak, the fleet parks flash within
  seconds of a wave and every drip slot goes to the model that answers.
  A straggler `_call` already inside its retry loop re-checks the pause
  before every further attempt (its retries would only feed a parked model
  and re-arm the pause they ignore — measured: flash drew 1302s while its
  own 180 s pause was running), and a rejection landing while a pause is
  still active extends it silently at debug level — the "pausing model"
  WARNING is announced once per pause, and again only if the pause expires
  without a 200 clearing the streak (then with the honest streak count,
  not the hardcoded threshold). The drip is ADAPTIVE: the configured interval is only the floor —
  1302/1113 stretch it (x1.3, capped ~4x floor; Z.AI's real ceiling is
  dynamic: four 1302s in two minutes at a steady 30 RPM were measured one
  evening), every 200 shrinks it back (x0.97). The pause length follows the
  code: 1305 capacity waves last minutes (`OVERLOAD_PAUSE_SEC` 180 s),
  1113 bursts pass in tens of seconds (`BALANCE_PAUSE_SEC` 30 s — the
  model it hits is usually the only working capacity left). While a pause
  runs, the per-book fallback chatter is debug-only and a once-a-minute
  "every model is paused" info line marks the (rare) fully-idle LLM stage.
  **1113** "Insufficient balance or no resource package" — also HTTP 429,
  documented as a hard billing error BUT on the coding endpoint it fires as
  a FALSE positive with quota left (dashboard-verified) when the account's
  ~5-request concurrency ceiling is blown — i.e. it is a symptom of the
  fallback herd storming, which the in-flight semaphore now prevents
  (max_tokens and the call parameters were experimentally refuted: every
  variant — streaming, no extra_body, small max_tokens, 6-deep parallel —
  passes when the platform is calm). Rare leftovers keep the transient
  streak treatment, never a run-long disable; if 1113 persists at
  `--llm-max-inflight 1`, THEN suspect real billing.
  **1308** "Usage limit reached" — quota exhausted: `_disable_model` adds
  the model to `_disabled_models` (model → reason) and every later `_call`
  for it short-circuits without an API hit. The openai client is built with `max_retries=0` — SDK retries
  fire outside the bucket and silently absorb 429s before `_call` can
  classify them. `_on_success` (any HTTP 200, not just parseable JSON)
  resets the escalation counter.
- **LLM JSON is salvaged, not rejected.** `_parse_llm_json` tries the cheap
  built-in sanitizer first, then `json-repair` (the `[llm]` extra) recovers
  unescaped quotes / control chars / truncation. JSON wrapped in commentary
  (a valid object followed by explanatory prose and a second fenced copy —
  measured on glm-5.3) is salvaged by carving out the first balanced
  `{...}` object (`_first_json_object`, works without json-repair); a
  json-repair LIST result yields its first dict — the model's answer, not
  its restated copy. The `json_repair` import is graceful (None if absent)
  — keep it optional.
- **`review.yaml` streams** (`review_writer.py`): one YAML document per book,
  appended as each finishes. `.bak` carry-over preserves prior user decisions,
  matched by the book's **uuid** (NOT calibre_id) so a decision survives an
  apply placement move. `bmf apply` reads both the multi-doc and legacy
  single-list forms.
- **The action model is `current` + `proposed` only** (`accept`/`delete`/
  `keep`, plus `null` = pending). `proposed` is the single edit surface:
  the GUI (and a human in a text editor) adjust the proposal's values
  directly — `proposed[field]: null` DELETES the field at apply time
  (`_apply_fields` in `pipeline.py`; writers serialize that as json null /
  OPF omission). There is NO `edited` block and no `reject`/`swap`/`edit`
  action anymore; legacy files are migrated on load (`review._migrate_entry`:
  `edited` merges over `proposed`, `edit`→`accept`, `reject`/`swap`→pending)
  in `_load_raw_entries`, `_load_prior` and `generate_review`. C1 swaps are
  proposed by the analyzer itself (`_build_proposed`'s C1 fallback), and the
  GUI's `Ctrl+W` merely swaps the two field values. A DECIDED entry's
  `proposed` (user adjustments incl. nulls) is carried verbatim through the
  next `analyze` (review_writer's prior path / `_entry_dict`); undecided
  entries get a fresh proposal.
- **The `keep` action is accept-but-retain — nothing more.** `action: keep`
  applies the proposed fields + cover exactly like `accept` (reuses the
  `_apply_action` accept branch) but is **NOT pruned** from `review.yaml`
  after a WRITE apply (`apply_review` skips adding its uuid to
  `succeeded_uuids`; counted in `summary["kept"]`). It does NOT freeze the
  book anymore — that role moved to the persistent `verified` flag — so the
  next `analyze` re-processes a kept book normally and its prior decision
  carries over (decided-prior path in `review_writer._handle`). A kept entry
  whose folder was moved by apply's placement gets its `path` rewritten
  (`review.update_paths`).
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
- **`.mbp` is an annotations sidecar, not a book** — recognized in
  ``readers.EBOOK_EXTS`` but deliberately LAST (never the primary format when a
  real book exists; excluded from epubgen's own ``_FORMAT_PRIORITY``).
  ``extractors.extract_mbp`` pulls the UTF-16 **big-endian** ``AUTH``/``TITL``
  records (Mobipocket is PalmOS-descended — an LE decode yields printable CJK
  garbage, so ``_mbp_utf16`` scores both variants and keeps the Latin one).
  The records were written by the reading device, NOT by calibre, so in
  book-less folders (64 in the library) they are the only identity evidence —
  but they fill only the EMBEDDED fields: no page text, so the verifier stays
  UNCERTAIN (honest — the content is gone), and proposals remain review-gated.
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
