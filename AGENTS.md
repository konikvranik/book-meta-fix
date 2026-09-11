# AGENTS.md — guide for AI agents working on this codebase

Read this before editing. It captures the conventions, layout, and
non-obvious gotchas that matter when changing this code. Companion docs:
[docs/architecture.md](docs/architecture.md),
[docs/diagrams.md](docs/diagrams.md),
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
  catalog.py       diagnosis-code help texts (CATEGORY_HELP: C1–C20, MISSING_*,
                   EMPTY_BOOK, …) for the GUI's clickable code popups; mirrors
                   docs/corruption-catalog.md. Msgids are wrapped in _() at MODULE
                   level on purpose — babel only extracts literal _("…") calls, and
                   the module imports lazily (first click) long after init_language
  readers.py       parse metadata.json (primary) / metadata.opf (fallback) / path
  library.py       traverse library tree + SQLite cache (parallel scan: the walk
                   splits per top-level dir and the per-folder cache-hit/parse/
                   uuid-mint work fans out over a thread pool — scan_workers/
                   BMF_SCAN_WORKERS/--scan-workers, default 8, 1 = serial; Cache
                   is thread-safe: one connection, check_same_thread=False +
                   _lock around SQL, file I/O outside the lock, results sorted
                   back to path order. books rows validate on the SLIM
                   fingerprint _folder_fingerprint — the book dir's own mtime
                   (membership → formats/primary file) + the metadata source
                   file's (mtime, size), 1–2 stat RPCs; the old per-file scan
                   (~7 GETATTRs per folder) measured ~100 s per scan on the
                   real NFS v3 library and the NAS serializes concurrent
                   GETATTRs (threads give 1.05×), so RPC COUNT is the only
                   lever — cover rewrites no longer false-invalidate rows,
                   and scan_library loads all rows with ONE batched SELECT
                   (Cache.load_all) instead of per-folder queries;
                   _stat_folder survives for abs_client.changed_folders.
                   Also hosts the `covers` table — the
                   persistent C11 verdict store keyed (path, mtime_ns, size)
                   that covers.analyze_cover attaches to via set_cover_cache,
                   so an unchanged cover.jpg is never Pillow-decoded twice,
                   in-run (memo) or across runs)
  detectors.py     rules C1–C14 → Diagnosis (C13 = location mismatch; exists only
                   when detect() gets library_root/pattern kwargs; C14 = series
                   order glued into the series NAME "Mark Stone #73" →
                   split_series_index + pre-filled accept, lossless split; C1
                   gains a POOL pattern when detect() gets known_authors= —
                   run_pipeline builds a KnownAuthorPool from the whole scan
                   and threads it through its detect wrapper: the TITLE
                   string resolving to an ESTABLISHED library author (cluster
                   >= _POOL_AUTHOR_MIN_BOOKS books, defined here and shared
                   with pipeline — a 1-2 book "author" is one corrupted
                   record away from a fake and must not flag legit titles
                   like "R.U.R.") fires C1 HIGH
                   either as a variant pair (title and author are the same
                   person in two spellings — real title lost) or a classic
                   swap (author field holds the real title; when it is ITSELF
                   another known author the reason flags ambiguity — biography
                   territory). Pattern 2 skips author folders carrying
                   nobility/origin particles (_NAME_PARTICLES — "de
                   Saint-Exupéry" is a 4-token NAME, not a title). Other
                   callers stay library-blind, same as C13)
  normalize.py     LIBRARY-WIDE pass (`bmf normalize`, the only emitter of C15
                   author-name variants — initials vs full names, diakritika,
                   titles, anonym family, swapped/comma order — and C16 genre/
                   tag variants): clusters spellings ACROSS books (a per-book
                   detector cannot see them), proposes whole-list replacements
                   through review.merge_normalizations; deterministic classes
                   pre-fill accept, letter-variant/comma-suspect clusters stay
                   pending. Genres canonicalize to Czech via fold groups +
                   the curated GENRE_ALIASES table (lookup runs THROUGH
                   fold_genre — table keys are human-readable, group keys are
                   token-sorted); genres and tags share ONE vocabulary (both
                   serialize to dc:subject). NO fuzzy tier for genres on
                   purpose: distance-2 matching was ~50% false pairs on the
                   real 1621-name vocabulary (Afrika→Amerika) — misspellings
                   become explicit alias rows. The author fuzzy tier is
                   surname-anchored (given-name token_sort ≥ 80, or ≥ 40 when
                   one side is mojibake) and downgrades the cluster to MEDIUM
                   (pending); _given_compat forbids in-position token skipping
                   (it let "Kevin J." match "Poul" — false HIGH merges in the
                   wild); different first-name initials NEVER merge (homonym
                   guard: Karel vs Josef Čapek). Also hosts KnownAuthorPool +
                   build_known_author_pool — the C1 pool, built by run_pipeline
                   per analyze over the same clusters (variant spellings land
                   on the cluster canonical; anonym family excluded; lone
                   spellings indexed under their own fold key), read-only
                   after build so worker threads share it freely.
                   Third emitter: C18 series-name variants (fold_series is
                   order-SENSITIVE — close sub-series must not auto-merge;
                   fold merges HIGH/accept, SERIES_ALIASES rows and evidenced
                   SuspectPairs MEDIUM/pending). Suspects = token-prefix
                   pairs ("Mark Stone"/"Mark Stone (edice)") + fuzzy
                   closeness to a VERIFIED series name, weighed by volume
                   NUMBERING (sets interleaving into one contiguous row ⇒
                   merge; both claiming volume 1 ⇒ distinct, never merged)
                   and optionally by an injected online_check callable
                   (Enricher.series_exists, persistent cache) — the engine
                   stays I/O-free. A prefix pair whose extension carries
                   CONTENT tokens ("Star Wars" → "Star Wars - Akademie
                   Jedi") is a named SUB-SERIES: verdict distinct up front,
                   no tier may retitle a named line into its franchise
                   umbrella (measured 2026-09-10: umbrella vols {3,4} +
                   a lone line volume {2} read as "contiguous" and renamed
                   the lines on every run; decorative tails like "(edice)"
                   stay mergeable, _SERIES_DECOR_TOKENS).
                   analyze_sequence gives the per-group
                   volume overview (missing = info, duplicates/anomalies =
                   warnings) that `bmf series` renders. The volume INDEX is
                   never proposed — only the series NAME (_apply_fields
                   keeps the book's own index half); dict-glued "#N" names
                   are SKIPPED (C14's split owns them) and multi-series
                   books are skipped and reported (a single-name proposal
                   would drop the other series)
  extractors.py    per-format content extraction → ExtractedMeta; PLUS
                   stream_full_text (FULL_TEXT_LIMIT = 2M chars): the WHOLE-book
                   text chunk-by-chunk for the GUI preview — EPUB per spine
                   member, PDF through a piped pdftotext, TXT raw, .doc via a
                   piped catdoc, opaque formats (mobi/pdb/…) through ONE
                   ebook-convert render (nothing streams DURING the render,
                   that is the "progressively, if possible" caveat), anything
                   else via extract()'s widest window; _pump_pipe owns the
                   child reaping (wait after EOF, kill on hang — GeneratorExit
                   runs the finally too, so closing a stale stream leaves no
                   zombie poppler). The pipeline's evidence windows
                   (first_page/broader) are untouched — this is preview-only
  filecheck.py     CONTENT-PROBE validity of ebook files — the engine of `bmf clean --files`
                   (C17 invalid-file delete proposals). Safety model: a file is
                   deletable ONLY when its content is recognizable as NO book format
                   (file_content_kind probes zip/pdf/mobi/rar/palmdb/text on the content;
                   a valid book under a wrong extension — an EPUB named .pdf — is
                   RECOGNIZED and never proposed; "unrecognized" implies invalid only for
                   _FULLY_PROBED_SUFFIXES, elsewhere it stays unknown). Calibre veto:
                   calibre_reads_file = ebook-meta exit 0 AND EMPTY stderr (measured: it
                   exits 0 with a traceback on garbage, filename fallback). Deliberate
                   misses: truncated PDFs / text junk stay unflagged (safety > recall).
                   scan_invalid_files + merge_file_deletions write `action: delete`
                   entries (proposed.delete_files) into review.yaml via the
                   merge_normalizations contract (decided never touched, pending only
                   overlaid); apply re-checks each file before unlink and snapshots it
  duplicates.py    LIBRARY-WIDE C19 pass (`bmf merge`, or `analyze --merge` over the
                   run's scan): duplicate folders of the SAME WORK — the default
                   {id} pattern keeps a double import's paths apart, so apply's
                   collision-merge can never reach them. Detection is EXACT-ONLY
                   on purpose: identical folded first author + folded title (the
                   fold_series recipe), or the same canonical ISBN; NO fuzzy tier
                   (a library-wide 0.85 sweep over ~5k books multiplies false
                   pairs — misspellings belong to C15). Final gate mover.same_book
                   (year tie-breaker → year-differing pairs never merge UNLESS
                   the ISBNs already match — a matching ISBN identifies the
                   edition even with a wrong year in one record), dead
                   records excluded (EMPTY_BOOK owns them). Survivor =
                   mover._pick_base (valid ISBN first, lowest calibre_id) —
                   deterministic; every other member becomes a review entry with
                   proposed.merge_into naming the survivor.
                   merge_duplicate_proposals follows the C17 contract (decided
                   untouched, pending overlaid, fresh entries pre-filled
                   `action: merge` ONLY when both sides carry the same valid
                   ISBN — the exact-match-without-proof tier stays pending;
                   never born verified). apply executes merges BEFORE its main
                   loop (a survivor may be moved by its own placement later in
                   the run), re-checks same_book on FRESH disk state per merge
                   (stale proposal → skipped, entry kept), spools the loser's
                   sidecars + cover into the shared deletion tar.gz (the folder
                   is already gone when the snapshot runs — copies go to a
                   run-lifetime spool dir with explicit arcnames), leaves a
                   survivor with its own decided whole-folder delete alone
                   (conflict of decisions), and invalidates the cache rows of
                   BOTH folders
  text_meta.py     offline page-text mining (1st stage of fix cascade)
  encoding.py      mojibake detection + repair
  isbn.py          ISBN extract/canonicalize/validate
  verifier.py      compare DB meta vs BOOK CONTENT (do NOT trust embedded) + identity primitives
  classify.py      disposition for report/epubgen (detect + identity gate + opt-in OK-audit)
  enrichers.py     databazeknih.cz / legie.info / self-hosted audiobookshelf_czech_metadata
                   provider (opt-in via BMF_ABS_CZECH_URL, aggregates ~17 CZ audiobook
                   storefronts behind ABS's /search contract; source key "abs_czech",
                   no ISBN endpoint — title+author only, narrator/duration unmodelled;
                   when the instance enables its databazeknih scraper the provider's
                   /search also serves structured DBK rows — measured 2026-09-11 those
                   rows carry title/author/publisher/publishedYear/description/cover
                   but NO isbn/genres/series, and the storefront scrapers return empty
                   under the provider's 8 s budget unless it is a warm query)
                   / OpenLibrary / Google Books → EnrichedMeta. Enricher.lookup runs
                   ALL applicable sources in PARALLEL and MERGES same-book results:
                   the first hit in priority order (dbk-isbn > abs_czech > dbk-title >
                   legie > OL/GB-isbn > OL-title) is the ANCHOR whose fields win;
                   every other result may only FILL empty fields (_merge_fill —
                   editions of one work legitimately disagree on year/isbn, so
                   fill-only, plus genre union) and only after the strict same-book
                   gate _merge_same_book: title >= 70 AND author agreement >= 80, or
                   a near-exact title >= 90 whenever the author cannot be compared
                   on either side — a same-titled DIFFERENT work ("Nová válka s
                   mloky") must never contribute a field. A pure-ISBN fan-out skips
                   the gate (every source answered the same exact key); a title
                   riding on an isbn query is still gated. databazeknih's detail
                   parse also recovers the series NAME from the detail page's
                   series box (_DBK_SERIES_RE — the page carries no volume index,
                   series_index stays None). Cover resolution
                   preference: equivalent provider matches (near-tied title+author)
                   are decided by probe_image_size (streaming image-HEADER read, no
                   body download), and Enricher.upgrade_cover cross-compares the two
                   CZ sources for cover-diagnosed books (pipeline want_cover plumbing
                   through _online_fill) — strictly-better cover_url only, identity-
                   anchored fields never change; alternative cached under "coveralt:".
                   Junk-tolerant match picking: the provider's keyword search (and its
                   Rozhlas rows with author "?") yields loose hits, so a CONFLICTING
                   author never wins and an author-less match needs a near-exact title
                   (>= 90) — same gates in _cover_same_book for borrowed covers
  pipeline.py      orchestration: ThreadPoolExecutor, per-book state machine +
                   apply_review (metadata writes + PLACEMENT — the former organize;
                   the delete action has TWO shapes: folder rmtree (C6 default) and
                   C17 file-level deletion — action delete + proposed.delete_files
                   removes ONLY the named files after a per-file RE-CHECK
                   (filecheck.file_is_invalid; a file that became valid or vanished
                   since the proposal is skipped and recorded in
                   summary[skipped_files]), everything lands in the same
                   deletion_snapshot tar.gz). Progress contract: progress_callback fires (0, total) once the
                   processing set is known — BEFORE the first book, so a bar shows
                   its total/ETA immediately instead of pulsing at 0/None until
                   the first LLM-bound completion — then (done, total) per book;
                   scan_progress_callback is forwarded to scan_library (workers =
                   scan_workers) so analyze renders the scan as its own labelled
                   phase before the processing bar (cli.py swaps the rich tasks).
                   The incremental OK-filter (_filter_not_ok) fans its detect()
                   calls over the same pool — C11 cover decode dominates it and
                   Pillow releases the GIL — and covers.analyze_cover's memo +
                   persistent cache make repeat decodes free. llm_skip_ids
                   (analyze feeds it ReviewWriter.decided_ids()) drops the LLM
                   for books whose prior review entry is already decided —
                   detection/enrichment still run (the entry needs its refresh),
                   only the token-costly call is skipped. The optional
                   scanned_books out-param hands the caller the FULL scan as a
                   PRE-verified-filter snapshot — analyze feeds it to its
                   --normalize tail (cli._run_normalize_pass, shared with the
                   normalize command) so the library-wide C15/C16/C18 clustering
                   runs over the same scan without a second (NFS-slow) walk;
                   verified books stay in it on purpose: clustering needs the
                   closed books to anchor the clusters. Right after the scan
                   run_pipeline builds the KnownAuthorPool (normalize) over
                   all_books and threads it into the detect wrapper (C1 pool
                   pattern) and _process_book (the Step-2d swap-repair tier
                   _try_known_author_swap: a C1 book whose title resolves to a
                   known author gets the author from the pool canonical and
                   the title from its OWN text — title_from_text, else the
                   weaker embedded title — gated by confirm_identity; a
                   classic swap is only trusted when the mined title AGREES
                   with the author field (a biography titled with its subject
                   has both names in its text and would survive a naive swap
                   self-test); every failure stays for review — the classic
                   swap with review's raw-swap hint (which fires ONLY for
                   HIGH classic-swap C1, never the MEDIUM heuristics or the
                   variant pair — see _build_proposed), the variant pair
                   with no proposal at all; counted in stats[swap_fixed]. Runs after
                   _try_deterministic_fix and only when that returned nothing
                   — _content_proposal's _is_better gate refuses exactly the
                   stuck clean-looking-but-wrong titles, so the two tiers
                   complement each other)
  llm.py           Z.AI provider: LeakyBucket + global 429 cooldown + reconcile_loop + tolerant
                   JSON salvage + get_provider (the provider FACTORY: BMF_LLM_PROVIDER picks the
                   branch — auto/Z.AI/antigravity-ACP/mock/off; the Antigravity branch composes
                   AntigravityAcpProvider with a quality stage per BMF_ANTIGRAVITY_FALLBACK: agy
                   (default — a second ACP pool on gemini-flash-high) or glm (a ZaiProvider loop),
                   so a ZAI_API_KEY alone no longer means the Z.AI fast tier)
  acp.py           AntigravityAcpProvider — a Google Antigravity subscription as the loop's FAST
                   tier via the Agent Client Protocol (the official `agy_acp_server.par` from the
                   ACP Registry; any ACP agent works — the command is user-configured,
                   BMF_ANTIGRAVITY_CMD / --antigravity-cmd). AcpAgentConnection = one agent
                   subprocess speaking ACP v1 (JSON-RPC 2.0, newline-delimited, over stdio; the
                   reader thread owns stdout, per-id futures resolve responses, agent→client
                   requests get non-interactive answers: permission → "cancelled", fs/terminal →
                   method-not-found, since bmf advertises NO capabilities). Fresh session per
                   prompt (a session keeps history — reuse would bleed one book's evidence into
                   the next); the composed message LEADS with ACP_NO_TOOLS_PREAMBLE — the
                   addressee is an autonomous IDE agent, and without the ban it tool-cascades
                   around the question (measured on 1.1.1: ~30 model round-trips per book, it
                   lists the session cwd and deliberates about the verifier; 2.2 s with the
                   preamble vs 5.3 s on a clean probe, tens of seconds on feedback turns), and
                   session/new gets a NEUTRAL per-provider scratch dir (tempfile bmf-acp-*,
                   NOT the library — the agent's tools explore the session cwd; removed on
                   close). Connections are RECYCLED after PROMPTS_PER_PROCESS (8) prompts:
                   the agent offers no session/delete (1.1.1 capabilities: list/resume only)
                   and every session pins a ~150 MB localharness child until the server
                   process exits — graceful close reaps the children (verified: no orphans),
                   so recycling bounds the leak per pool slot.
                   Google's PROMPT-LEVEL safety filter answers with Prohibited Use
                   boilerplate INSTEAD of a model turn (the verbatim first-page text
                   of a few CZ sci-fi/fantasy books trips it; deterministic per
                   prompt — a blocked prompt blocks on every retry and on both Gemini
                   pools): detected in _call via PROMPT_BLOCK_MARKERS, answered with
                   ONE retry on the same evidence WITHOUT first_page_text (the one
                   prompt part bmf does not author), and a still-blocked prompt
                   returns the PROMPT_BLOCKED_ERROR marker so reconcile_loop SKIPS
                   the agy quality stage (same filter, same evidence) while a GLM
                   fallback (different provider, different filter) still runs.
                   Auth handled once via the -32000 → authenticate dance (terminal-type
                   logins are refused with guidance — headless bmf cannot run them); model chosen
                   through session/set_config_option (category "model", best-effort). Model names
                   are FAMILY-matched against the agent's own options (match_model_option:
                   exact value/name, else token subset with digit tokens skippable — measured on
                   the real 1.1.1 agent, whose options are gemini-3.8/3.7/3.6-flash-
                   high|medium|low + gemini-pro-agent/gemini-3.1-pro-low: "gemini-flash-low"
                   → gemini-3.8-flash-low, "gemini-pro" → gemini-pro-agent); defaults
                   gemini-flash-low (fast tier) + gemini-flash-high (quality stage). The launch
                   command needs the registry's --uid= arg (value stays EMPTY — the
                   Google launcher reads it as a GROUP NAME for setgid; a filled uid crashes
                   the process at startup); the distributed .par IS the
                   server (a ~1.9 GB self-contained ELF — chmod +x when an archive manager
                   drops the bit; its localharness_external sibling must sit NEXT TO IT: the
                   agent's startup resolves the harness beside argv[0] and every session/new
                   of a harness-less install dies with -32603 Internal error — session
                   creation builds the connection on it even though bmf denies tools). Pool of connections =
                   the in-flight cap (BMF_ACP_MAX_INFLIGHT, default 4; the AGY FALLBACK pool is a
                   separate ONE-slot lane by default — BMF_ANTIGRAVITY_FALLBACK_MAX_INFLIGHT, books
                   queue on it: fallback is the exception path and measured 2026-09-08 prompt turns
                   spike 30–100 s SERVER-side under load — degraded turns even echo mojibake and
                   self-report as gemini-2.0-flash, so parallel fallback slots buy latency-queueing,
                   not throughput; the model RESETS to the agent default at every session/new, which
                   is why set_model runs per prompt), transport errors retry
                   once on a fresh process, THREE consecutive failures disable the fast tier for
                   the run (the quality stage takes over per book). reconcile_loop mirrors
                   ZaiProvider's contract/source labels; the QUALITY stage (_run_fallback) is
                   BMF_ANTIGRAVITY_FALLBACK: 'agy' (default — a second AntigravityAcpProvider
                   pool on gemini-flash-high, ONE attempt carrying the verifier's rejection
                   reason from the failed fast attempts in the evidence feedback — a stronger
                   model without it repeats the rejected answer, llm:high on verify pass /
                   llm:low) or 'glm'
                   (the injected zai_fallback.reconcile_loop(max_flash=1), pre-seeded with the
                   same feedback — Z.AI's measured rate
                   machinery stays untouched for exactly the calls that need it).
                   SELF-MANAGED agent (no separate install command): ensure_acp_agent — used by
                   get_provider when BMF_ANTIGRAVITY_CMD is empty/'auto' with an agy opt-in
                   (provider antigravity or the sentinel) or an existing cache — checks the ACP
                   Registry, downloads (~700 MB streamed, progress_cb renders it inside analyze's
                   bar) / upgrades atomically (os.replace; a failed download keeps the previous
                   version), extracts the agy_acp_server member AND its localharness*
                   sibling (installed_acp_release treats a binary WITHOUT the sibling as
                   not-installed, so legacy stripped caches self-heal via one same-version
                   re-download; a sidecar harness=null — archive carried none — skips the
                   check to avoid a re-download loop), chmod +x, and serves argv incl. the
                   registry's --uid= arg; offline keeps the
                   installed agent. Cache: ~/.cache/book-meta-fix/acp (BMF_ACP_CACHE_DIR/XDG) with
                   a version.json sidecar. Plain auto NEVER downloads without opt-in or cache
  review_writer.py streaming review.yaml writer (queue + writer thread);
                   decided_ids() exposes the prior entries with action set so
                   run_pipeline's llm_skip_ids can skip the LLM for them (the
                   writer carries a decided prior VERBATIM — a fresh proposal
                   would be discarded unread; without the skip every analyze
                   re-run before apply re-buys the whole decided pool, measured
                   2026-09-08: ~538 decided entries re-LLM'd per run), and the
                   decided-carry branch stamps verified: true on an ACCEPTED
                   prior whose proposal projects detector-clean (the carried
                   twin of _projected_clean's fresh-entry pre-fill; keep stays
                   exempt — it must remain re-reviewable — and llm:-source
                   proposals without identity confirmation are never
                   auto-closed; _identity_verified admits llm:high and the
                   accept-missing/text_meta content tier, both only with
                   identity_confirmed, plus online sources — "content" is what
                   lets the decided accept-missing pool (~867 books) close on
                   the first re-analyze instead of re-extracting forever)
  review.py        parse review.yaml (multi-doc + legacy list) + update_paths
                   + _is_retired_c2_stem (load-time retirement of the removed
                   C2 stem-match entries: a PENDING entry whose ONLY diagnosis
                   is reason "title == primary file stem" is DROPPED wherever
                   review.yaml loads — _load_raw_entries, review_writer's
                   .bak prior path and generate_review — else finish() would
                   carry the stale noise forever, the analyzer no longer
                   flagging those books; DECIDED entries and mixed-reason/
                   multi-diagnosis entries stay, the latter get rebuilt fresh
                   by the next analyze)
                   + merge_normalizations (bmf normalize --apply merges C15/C16/C18
                   proposals IN PLACE: pending entries get proposed.authors/
                   genres/tags overlaid + the diagnoses appended, DECIDED
                   entries are skipped — never clobber a user decision — and
                   unknown books get fresh entries, accept pre-filled only
                   when every change is deterministic/HIGH; a fresh accepted
                   entry whose projected post-apply state is detector-clean —
                   same projection as review_writer.projected_clean, hoisted
                   module-level for this reuse — is ALSO born verified: true,
                   so apply fixes AND closes it; a leftover MISSING_* keeps
                   the book open on purpose, a spelling fix is not identity
                   evidence and closing would cancel the enricher retries)
                   + merge_empty_deletions (bmf clean --empty --apply writes
                   EMPTY_BOOK delete proposals IN PLACE: pending entries get
                   action: delete pre-filled + the diagnosis appended, DECIDED
                   entries skipped — including analyze's pre-filled accept —
                   unknown books get fresh action: delete entries; the
                   empty-folder fact re-verified per book via rule_empty_book
                   at write time)
  writers.py       atomic metadata.json/.opf writers + ensure_uuid + clear_verified
  mover.py         move/merge engine used by apply's placement (organize fn kept for tests)
  epubgen.py       bmf epubgen
  crosscheck.py    bmf crosscheck
  covers.py        generated-cover detection (pixel math) + replacement & in-book extraction fallback
                  + embedded-cover strip (EPUB zip+OPF surgery) + strip_generated_covers
                  (the per-folder engine of `bmf clean --covers`: sidecar → .bak, embedded EPUB probe+strip;
                  two selectors with independent scopes — generated/invalid × external/embedded — plus
                  min_size (px on the SHORTER side, external-only): real-but-small covers (the
                  databazeknih thumbnails) → <name>.bak over the same candidate set so the book
                  re-fires MISSING_COVER and Enricher.upgrade_cover refetchs a strictly bigger one;
                  the EMBEDDED cover is deliberately kept — it is the recovery fallback when no source
                  serves anything bigger; sizes ride analyze_cover's persistent cache;
                  "invalid" =
                  cover files no decoder reads (image-ext or cover.* name — ABS picks covers by EXTENSION
                  only, prefers cover.*, else first png/jpg/jpeg/webp, so a cover.html/HTML-as-.jpg becomes
                  the item cover and ffmpeg fails "Invalid data found"); checked by image_is_readable
                  (Pillow full decode, no Pillow = delete nothing) → <name>.bak like the generated path;
                  ABS_IMAGE_EXTS (the png/jpg/jpeg/webp whitelist) is the single definition shared
                  with abs_client's stored-cover audit)
  abs_client.py    Audiobookshelf API client + the engine of `bmf abs-rescan`: changed_folders
                  (stat-only walk over iter_book_folders, max file mtime ≥ since), match_items
                  (folder → ABS item: exact path → relPath → unique folder-name match — covers
                  different mount prefixes and placement moves), AudiobookshelfClient (PER-ITEM
                  /api/items/{id}/scan — NOT /api/items/batch/scan, which was measured
                  answering 200 while processing nothing). ABS keeps its own DB and
                  plain scans skip "unchanged" folders, so apply's disk writes are only pushed
                  into ABS through this per-item rescan; scan endpoints need an ADMIN token
                  (BMF_ABS_URL/BMF_ABS_TOKEN/BMF_ABS_LIBRARY/BMF_ABS_WORKERS; module-level
                  _http_get_json/_http_post/_http_delete are the monkeypatch seams for the
                  no-network tests and take an optional session= kwarg — the client shares ONE
                  requests.Session across all calls so the per-item loop keeps TCP+TLS
                  connections alive instead of handshaking per call). scan_items fans the items
                  over a ThreadPoolExecutor (workers knob: --abs-workers/BMF_ABS_WORKERS,
                  default 4, 1 = serial; per-thread SCAN_CALL_PAUSE keeps each connection's
                  burst gentle) and drives a progress_callback(done, total) under the counter
                  lock — the CLI renders it as a rich progress bar with ETA because the
                  synchronous per-item scans run for minutes even in parallel. The two
                  pre-scan sweeps take callbacks too: changed_folders fires done-only
                  (lazy walk, total unknowable without doubling the stat RPCs → the CLI's
                  pulsing TRANSIENT bar counts folders; the walk is silent tens of
                  seconds over NFS and happens in dry-run as well), broken_cover_items
                  fires (done, total) per item (each stored cover row costs one exists()
                  stat — a determinate bar under --fix-covers). With --apply,
                  unmatched folders (moved/new — no item id to scan) additionally get a PLAIN
                  library scan fired AFTER the per-item loop (async server-side, fire-and-
                  forget; after the loop on purpose — a racing library scanner would double
                  the server load and could collide with our per-item scans); its failure
                  only warns, it cannot fail the run.
                  abs-rescan --fix-covers = the ABS-DB half of the cover cleanup:
                  broken_cover_items audits EVERY item's stored media.coverPath (exposed by
                  the items listing) — a row is broken when its target's extension is not in
                  covers.ABS_IMAGE_EXTS (metadata.json/cover.html — the stale ffmpeg
                  "Invalid data found" rows no current ABS build writes or heals) or when it
                  maps under an ABS library folder onto library_root and the file is gone
                  (paths outside the folders, e.g. ABS's uploaded /metadata/items covers,
                  get the ext check only); clear_item_cover nulls the row via
                  DELETE /api/items/{id}/cover and the cleared ids join the rescan set
                  (--since must NOT filter them). Run strip-covers --invalid FIRST — a folder
                  still holding an unreadable cover.jpg would get it re-picked
  gui.py           bmf gui — keyboard-first Tkinter review.yaml editor (no new writer: loads raw
                  entry dicts, writes via review._header + review._render_entry; detail split is
                  RESPONSIVE: WIDE detail pane = the scrollable review form LEFT (header,
                  Found-problems section, fields, C19 merge panel, covers) + the content text
                  preview RIGHT across the FULL pane height; NARROW detail (< 940 px — a small
                  window OR the outer list sash dragged wide) = form on TOP, preview at the
                  BOTTOM, with hysteresis so a sash drag cannot flap. ttk's Panedwindow -orient
                  is READ-ONLY and Tk has no reparent, so the switch (_apply_detail_orient)
                  destroys and recreates only the thin Panedwindow — the pane frames are its
                  SIBLINGS (children of the detail frame, a layout ttk's content manager
                  explicitly supports) and thus survive with every widget and half-typed edit
                  intact; lift() re-stacks them above the fresh widget, and the horizontal
                  repack must pack the scrollbars BEFORE their clients — pack allocates in
                  order and the canvas's 720 px request starves a trailing scrollbar to 1x1.
                  NARROW mode additionally swaps both native scrollbars for ONE chained
                  scrollbar spanning the detail's right edge: its virtual range concatenates
                  the form canvas overflow + the preview text overflow, and _chain_scroll
                  maps a position piecewise — the form scrolls to its end FIRST, the text
                  picks up only past it (BOTH widgets' moveto fractions span their WHOLE
                  content, not just the overflow — the canvas follows the Text convention
                  here). The wheel chains the same way (_on_wheel: a wheel falling off the
                  form's bottom scrolls the text, the text's bottom is the page end; the
                  form-fits no-op in _scroll_canvas returns False now so the carry can fire).
                  The old
                  drag-to-resize grip is gone — the pane sash IS the resize knob now,
                  loading the WHOLE book text PROGRESSIVELY via extractors.
                  stream_full_text (chunks append as they arrive through _after;
                  _content_gen gates stale streams — the worker STOPS pulling and
                  closes the generator, which reaps pipe children; an empty stream
                  falls back to extract()'s widest window or its error message;
                  mojibake detection/recode defaults run ONCE on the complete text,
                  per-chunk detection would flicker; a format-radio click reloads via
                  the _format_var trace/_on_format_changed — historically the radios
                  had no command at all and a click never reloaded anything; the old
                  first-page/broader toggle and its Ctrl+T are GONE); the
                  Found-problems section lists ALL diagnoses via sort_diagnoses (decision-waiting
                  proposals → damage → MISSING_* info, stable; the old "primary + (+N more)"
                  header line hid the load-bearing one behind the counter) — the body
                  is ONE disabled tk.Text styled FLAT on the form background, height
                  == wrapped rows (recounted on <Configure>, _sync_problems_height)
                  and NO scrollbar, because a disabled Text still SELECTS with the
                  mouse: the lines are copyable (Ctrl+C / right-click copy menu —
                  clipboard writes are MANUAL, event_generate("<<Copy>>") chained
                  from inside a binding never reaches the class binding) and every
                  CODE span is clickable, opening catalog.category_help in a popup;
                  the click resolves on ButtonRelease through a WIDGET-level index
                  lookup + txt.compare — tag_bind <Button-1> is unreliable under
                  event_generate — and a drag-selection (sel non-empty) suppresses
                  the popup; the C19 merge panel
                  (_load_merge_panel, packed only for merge entries) shows the survivor + pick
                  reason + cluster siblings and a per-field comparison via merge_projection_rows —
                  survivor value / this book's value / the EFFECTIVE result computed through the
                  SAME mover.merge_meta + _apply_fields projection apply executes, so the panel
                  cannot disagree with the outcome; source radios ⬅/➡ write explicit picks into
                  proposed[field] (apply's merged_transform stamps them over the automatic
                  result; ∅ = keep empty), the cover row offers thumbnails of both sides and
                  proposed.cover_source auto/survivor/loser/loser_epub (the EPUB extraction is
                  generated-placeholder gated), merge_file_plan pre-reports every move and
                  name collision, and Reset to automatic drops all picks; Tab-trap bindtag,
                  per-format embedded covers, Ctrl+G double-decode recode,
                  clickable path link / list double-click = open folder via open_folder_in_manager;
                  Verified checkbox (Ctrl+O) = the persistent user-OK mark; "+ library" search
                  matches the whole library via a fulltext index built by ONE background sweep at
                  startup (build_library_index: parallel NFS-aware walk; folders are served from
                  the SQLite books cache — Cache.get, only misses read via read_book_folder and
                  are put back — the sweep used to re-read every metadata.json over NFS per GUI
                  start; cache errors degrade to direct reads; the
                  same sweep feeds the author/series autocomplete pools; haystack = entry search
                  + manifest-only fields description/publisher/tags/subtitle + ALL series entries
                  (entry `current` carries only the FIRST series — a multi-series book's second
                  series is searchable only through this half), so a book matching only via its
                  annotation is found; uuid minted via ensure_uuid at index time).
                  Searches (search_library_index) are instant in-memory multi-word filters that
                  serve SHALLOW COPIES (the GUI mutates served entries; the index stays pristine);
                  merged entries stay in memory for the whole session (no drop on toggle-off —
                  Tab+Space can accidentally toggle the checkbox), only CHANGED ones are written
                  on save: entries_to_write/library_entry_changed (a pure C14 series-split
                  prefill does NOT count as a change — _is_pure_series_prefill; the mass fix is
                  analyze's pre-filled accept). _filtered_indices exempts listed library entries
                  via their INDEX haystack keyed in _lib_uuids (uuid → hay) — the entry itself
                  lacks the description the index matched on, and a stale superset match hides
                  when the needle narrows). Multi-select in the list (Ctrl+click toggle /
                  Shift+click range, _BookList selection_toggle/selection_extend — focus row ≠
                  selection: the detail pane follows the FOCUS; refresh_list banks+intersects
                  the selection so rows hidden by a filter leave it) feeds the Ctrl+E bulk
                  edit (apply_bulk_field + bulk_edit dialog): one author/series value (or ∅
                  delete) into every selected entry's proposed, pending → accept, series
                  ORDER never touched; the dialog holds a grab and _on_ctrl_key ignores
                  shortcuts under any grab; the row's author/series labels are decision-aware
                  (entry_author_label/entry_series_label — accept/keep shows the proposed
                  value, so a bulk edit is visible in the list at once) — plus the Shift
                  bulk twins (dispatch: Shift combos are matched BEFORE the Ctrl passthrough
                  set, so Ctrl+Shift+A wins over select-all): Ctrl+Shift+A bulk accept
                  (apply_bulk_action — an explicit selection OVERRIDES decisions, unlike
                  the field edit), Ctrl+Shift+O bulk verified toggle (apply_bulk_verified;
                  clears when ALL selected carry the mark), Ctrl+Shift+M bulk cover delete
                  (execute_bulk_cover_delete — sidecar cover.jpg/.bak + embedded EPUB strip,
                  immediate like the single-book path, AND drops proposed.cover_url rebind-
                  style: apply re-downloads it for C11/MISSING_COVER, so keeping the URL
                  would undo the deletion), Ctrl+Shift+D bulk decision-clear
                  (apply_bulk_action(..., None) — the mass VETO for pre-filled
                  delete/accept; C17 entries arrive action: delete, the user filters
                  by the delete state and un-decides what should survive), and
                  Ctrl+Shift+R remove-from-review (confirm dialog → drop selected
                  entries entirely — no files touched, neither delete nor accept;
                  prunes _lib_uuids/_lib_index/thumbs like a merge and saves
                  review.yaml IMMEDIATELY; a book whose problem persists gets
                  re-flagged by the next analyze/clean), and Ctrl+J merge-selected (execute_merge +
                  merge_selected dialog + _after_merge): the dialog picks the SURVIVOR
                  (default = focus row; same_book mismatch only warns — the user decides,
                  unlike the automatic placement merge) and every other selected book is
                  folded in via mover.merge_folders (files move, metadata field-merged
                  survivor-first, loser folders removed); merged losers leave self.entries
                  (dropped by IDENTITY — uuid-less legacy entries cannot be keyed), the
                  survivor's current is refreshed from disk (_build_current), _lib_uuids/
                  _lib_index/thumb caches are pruned so "+ library" cannot re-serve ghosts,
                  the SQLite cache rows are best-effort invalidated and review.yaml is saved
                  IMMEDIATELY (disk and file must agree — a stale entry would fail the next
                  apply with "folder not found"); the survivor keeps its action/proposal
                  for apply to finish. The dialog ALSO carries a per-field grid (rows =
                  MERGE_FIELDS, columns = selected books, one radio per cell): each cell
                  shows merge_field_value — a DECIDED proposal counts as that book's value
                  (the list-label convention), pending/library books show the disk value —
                  and the picks (execute_merge values=) override the automatic merge:
                  _apply_merge_choice writes them onto the merged metadata, merge_choice_
                  proposal REBASES only CONFLICTING existing proposed keys (no proposal
	                  noise for clean fields; a stale analyzer suggestion cannot undo an
	                  explicit pick at the next apply); untouched defaults reproduce the
	                  automatic gap-fill (survivor's value, else first found), so confirming
	                  as-is loses nothing. Ctrl+Shift+J split-book is the UNDO of a wrong
	                  merge (execute_split + split_book dialog + _after_split): two
	                  UNRELATED works sharing one folder (a C19/placement merge that
	                  should not have happened) — the dialog lists the folder's ebook
	                  files (embedded title/author hints load in a BACKGROUND thread,
	                  extract() may spawn a calibre subprocess), the checked ones move
	                  out into a NEW book folder placed by mover.compute_target_path
	                  with cfg.path_pattern (collisions incl. dest == the source folder
	                  get move_book's "(dup N)"; a split book has no calibre id, so the
	                  default {id} pattern yields "(noid)"); the new book's metadata
	                  prefills from the primary moved file's EMBEDDED block, falling
	                  back to the text-mined fields (txt carries no embedded block),
	                  with the dialog's author/title values overriding, gets a FRESH
	                  uuid + write_book_meta + a best-effort cover via
	                  recover_cover_from_book (generated placeholders rejected, so
	                  MISSING_COVER re-fires and the enrichers retry); a failed move
	                  ROLLS BACK the already-moved files (no half-split folders); the
	                  source entry keeps its identity/decision (only its file set
	                  shrinks, current refreshed), and the new book joins the list as
	                  a LIBRARY-served entry (library_entry_from_meta + _lib_uuids hay
	                  registration — editable at once, written to review.yaml only
	                  once changed); cache rows of both folders invalidated, review.yaml
	                  saved IMMEDIATELY (same disk-agrees contract as the merge)
  cli.py           click commands: scan, report, analyze, apply, epubgen, crosscheck,
                  strip-covers, normalize, series, merge, abs-rescan, gui
                  (series = the READ-ONLY C18 companion — overview table of
                  every series with book counts, volume coverage and the
                  suspect pairs, writes nothing; normalize gained --series/
                  --online selectors and analyze --normalize runs the series
                  tier too; merge = the C19 duplicate sweep — dry-run reports
                  the clusters, --apply fills review.yaml via duplicates.
                  merge_duplicate_proposals, and analyze --merge chains the
                  same pass over the run's scan (after review_writer.finish(),
                  same ordering rule as the --normalize tail); organize is a deprecation stub — placement lives in apply; in abs_rescan
                  the two _() header strings sit OUTSIDE the f-string — babel on py3.10
                  cannot extract calls from f-string holes — the SAME reason the ACP info
                  line's "fast tier"/"no Z.AI fallback" strings are hoisted into locals
                  before their f-string; strip_covers exposes the engine's
                  two selectors as --generated/--invalid optional-value flags — click
                  is_flag=False + flag_value="both": bare flag = both, a value
                  external/embedded narrows, NO flag at all falls back to generated-only
                  so bare `bmf strip-covers` keeps its historical behaviour; clean hosts
                  a THIRD selector --files (default OFF, plain boolean flag — the C17
                  file probes are opt-in): dry-run only reports, --apply writes the
                  delete proposals into cfg.review_file via filecheck.merge_file_deletions
                  (deletion itself is apply's job), and a FOURTH --min-size N
                  (pixels, shorter side; implies --covers, env default
                  BMF_COVER_MIN_SIZE honoured only while covers are on — an explicit
                  --no-covers beats the env): small sidecar covers → .bak via the
                  engine's min_size selector, and the touched books ALSO get their
                  `verified` flag cleared (else analyze's skip-verified default would
                  never re-fire MISSING_COVER, so the bigger cover would never be
                  fetched), and a FIFTH --empty (default OFF, plain boolean): dead
                  records (EMPTY_BOOK folders — no ebook file, only metadata
                  sidecars) get `action: delete` proposals written into review_file
                  via review.merge_empty_deletions (same three-way contract as
                  filecheck.merge_file_deletions: decided never touched, pending
                  overlaid, fresh born delete; the empty-folder fact re-checked per
                  book through rule_empty_book at write time). The OPT-IN delete
                  path — the default EMPTY_BOOK fate stays quarantine in
                  needfix/empty/. Run AFTER analyze: a later analyze rebuilds
                  pending EMPTY_BOOK entries back to accept; analyze
                  takes --llm-provider/--antigravity-cmd/--antigravity-model and closes
                  the provider in its finally block — only the ACP provider actually
                  holds subprocesses — and --normalize, which chains the normalize
                  pass onto the END of the run via _run_normalize_pass (the shared
                  post-scan half of the normalize command) over run_pipeline's
                  scanned_books: strictly AFTER review_writer.finish(), because
                  merge_normalizations rewrites review.yaml in place and would
                  race the streaming writer; an interrupted run skips the tail)
```

## Non-obvious gotchas

- **The verifier does NOT trust embedded metadata.** Calibre wrote the
  (possibly wrong) DB metadata back into the file at import time, so the title
  inside `content.opf`/PDF Info is *not* independent evidence. Only the book's
  actual text (ISBN scanned from content, fuzzy title on first-page text) can
  confirm a record. Don't "fix" the verifier to trust embedded OPF.
  `_author_in_text` additionally runs a STRUCTURAL variant matcher
  (`_author_variant_in_text`) before its fuzzy fallback: the printed credit is
  often the same author in another format — initials for given names (incl.
  glued "G.J.Arnaud"), dropped middle names/initials, inflected
  (Baxter → Baxterovi) or transliterated (Strugačtí/Andersen vs
  Strugackij/Anderson, fuzzy ≥ 74 with len ≥ 6) surnames, a co-author joined
  by a connective. The FIRST given name is mandatory (initial or full, in
  order) — that is the homonym guard (Karel vs Josef Čapek, Kevin J. vs Poul
  Anderson; same rule as normalize's `_given_compat`); a bare single letter
  counts as an initial only with its dot or directly beside the surname
  (Czech prepositions "s"/"v"/"u" otherwise pose as initials); given names
  are never fuzzy/inflection-matched (a Czech-inflected given in a dedication
  is a different person — "Jamesi Baxterovi" in a Stephen Baxter book is the
  son); a surname-only author gets no structural credit (a prose mention is
  indistinguishable).
- **Source of truth = `metadata.json`**, not `.opf`. Writers update *both*.
  Readers prefer `metadata.json`, fall back to `.opf`.
- **Field coverage is end-to-end**: whatever the enrichers/LLM return must
  reach disk. `_build_proposed` (`review.py`) proposes it, `_apply_action`
  (`pipeline.py`) maps it onto `BookMeta`, writers emit it in BOTH
  metadata.json and metadata.opf (series as `calibre:series` /
  `calibre:series_index`, genres+tags as `dc:subject`). `proposed.tags` is
  wired too (whole-list replace like genres — `bmf normalize` proposes it;
  the writers always serialized `meta.tags`, only the apply branch was
  missing). A pending `bmf normalize` entry survives a fresh `bmf analyze`
  only when the analyzer does NOT re-flag the book (finish() carries
  unprocessed priors); a re-flagged book's pending entry is rebuilt fresh
  and the normalize proposal keys drop out — re-running normalize
  re-proposes them. When adding a field,
  wire all three links — historically series/language/description were
  fetched but silently dropped at one of them. Series travels through
  review.yaml as flat strings and is serialized into the manifest as the
  ABS-NATIVE string list `["Name #N"]` (writers `_abs_series_string`):
  current Audiobookshelf parses metadata.json series as a STRING array and
  its validator DROPS non-string entries — the former `{"name", "index"}`
  object form silently erased the series on the next item re-scan (found
  via `bmf abs-rescan`). The wild stored shapes (plain string `"Name #N"`,
  `{"name", "index"}` dicts, `sequence` key) are normalised by
  `models.series_entry_pair()` — the single normalizer behind
  `BookMeta.series_pair()` (GUI display, placement patterns, the OPF
  mirror) and the writer's serialization; a plain `"Name #N"` string splits
  into name + index at read time (ABS's own parseSeriesString convention),
  so C14 fires only for a glued name in DICT form. An edit with an emptied
  series name clears the series.
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
  for identity. The text gates (`acquire_identity` steps 3-4,
  `confirm_identity`, `verify_proposal`) search every content window
  WHOLE-text: `broader_text` is prefix-aligned with `first_page_text` (all
  extractors build it as a longer prefix of the same stream), so the old
  4000-char search cap made the broader window a no-op — a title page a few
  pages in needs the depth, and the `_TITLE_DEEP_PENALTY` positional logic
  is what keeps deep-only hits honest (deep non-verbatim titles max out at
  0.80, exactly the fuzzy_strong boundary).
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
  inclusion set). Since the content tier of `_identity_verified` (see the
  verified bullet) the stamp ALSO closes the book: the entry is born
  `verified: true`, apply persists the flag, and the next `analyze` skips
  the book entirely — no more re-firing the same accept every run (the
  pre-content-tier behaviour: a MISSING_ISBN/YEAR book reappeared as
  auto-accept on every `analyze` because the detector re-fires, the field
  being genuinely still missing). A
  MISSING_COVER book whose cover was recovered does NOT re-fire (cover.jpg is
  now present); one whose cover could not be recovered is now closed too —
  its enricher cover lookup will not be retried unless `--recheck-ok`
  re-opens the book. A co-occurring
  `NEEDS_REVIEW` diagnosis (e.g. C11 generated cover) blocks the auto-accept
  and keeps the book in review. If any enricher/text_meta DID return data,
  `enriched` is already set and those fields are proposed + applied normally;
  the one exception is an LLM answer that FAILED verification — `source ==
  "llm:low"` is treated as nothing-recovered too (the untrusted proposal is
  DISCARDED, not auto-applied; leaving it in place stranded acceptable-missing
  books in pending forever, measured: 72 MISSING_ISBN books with llm:low
  answers whose identity the content confirmed).
  The stamp has a weaker second tier, `source="author-pool"`
  (`_pool_confirms_author`): when `acquire_identity` found nothing in the
  content (scanned PDF, no title page in the extract) but the author resolves
  in the run's KnownAuthorPool with >= `_POOL_AUTHOR_MIN_BOOKS` (3) books and
  the title is substantive (>= 10 folded chars, not a "Neznámý" shape) and is
  neither a known author nor a known SERIES name (run_pipeline also builds a
  folded `known_series` set beside the pool; the book's OWN series is exempt —
  a first volume legitimately shares the series title), the same minimal stamp
  fires. Weaker BY DESIGN — it proves the author field, not that the title
  belongs to this book; the owner accepted the trade (reversible via
  `--recheck-ok`). Counted in stats[`pool_verified`]. Metadata stays
  untouched — canonicalising author spellings is C15/normalize's job.
- **Placement lives in `apply`, not a separate command** (the former
  `bmf organize` is a deprecation stub). After writing an entry's metadata,
  `apply_review` routes the folder via `_placement_target` + `_place_applied_book`
  (`pipeline.py`, using `mover` primitives): every DECIDED entry (accept/
  keep) → the pattern target path — the decision outranks residual detector
  complaints, so an accepted book always moves back OUT of `needfix/`
  (a complaint that is real re-fires on the next analyze; only `verified`
  closes a book for good). No ebook file (EMPTY_BOOK) → `needfix/empty/`
  — the one hard fact no approval changes (needfix prefix, and a nested
  `empty/`, stripped for idempotent re-runs). Placement no longer runs the
  detectors at all — pure path math on the final metadata with NO content
  reads/identity gate; that expensive work belongs to analyze, which is why
  apply is fast where organize was slow. (The former detector-gated routing
  buried accepted books: a still-flagged one under needfix/ recomputed its
  needfix destination, got `already_correct` and cycled forever — measured
  2026-09-11 on a C9-whitelist-gap anonym accepted via C13.)
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
  and never re-enters review. `bmf normalize` pre-fills it too: a FRESH
  deterministic (HIGH) entry whose projection through the same function
  (hoisted module-level as `review_writer.projected_clean`) is detector-clean
  is born verified — a leftover MISSING_* deliberately blocks it (a genre/
  spelling fix is not identity evidence, and closing would cancel that
  field's enricher retries). The relaxed twin `_identity_verified` (same
  place, runs only when the projection is NOT clean) closes an accepted
  entry whose FINAL identity is confirmed against the content AND an
  independent record: `enriched.identity_confirmed` with `source` in
  `_ONLINE_SOURCES` (databazeknih/legie/abs_czech/openlibrary/google_books —
  the pipeline stamps the flag only after `acquire_identity` + an
  author-filtered/ISBN-anchored online hit), with `source == "llm:high"` —
  the cached-author tier: the pipeline only keeps llm:high when
  `confirm_identity` bound the answer to the book's own text AND the
  post-LLM author/series existence ladder passed (an unconfirmed author is
  downgraded to llm:low), so the answer carries content-bound identity + a
  known author even when no bibliographic DB knows the book — OR with
  `source == "content"` — the accept-missing stamp / a text_meta fix:
  `acquire_identity`/`confirm_identity` bound title+author to the book's
  own PAGE TEXT (never the embedded OPF) — OR with `source ==
  "author-pool"` — the pool tier of the accept-missing stamp: content had
  nothing to confirm against, but the author is an established library
  author and the title guards passed (see the accept-missing bullet); the
  weakest admitted tier, it proves the author field, not the title.
  Measured 2026-09-09: without the
  content tier ~867 accepted-missing books re-entered EVERY analyze run
  (re-extracted over NFS just to re-confirm the identity and be
  re-accepted); with it the carry path closes them on the first re-analyze
  and apply persists the flag. The trade the owner accepted: a verified
  MISSING_COVER book stops re-querying enrichers for a cover —
  `--recheck-ok` is the (blunt) way back in. "embedded" (calibre-written
  OPF) and the flash/loop tiers still do NOT count. `verifier.identity_agrees`
  checks
  the projected identity still agrees with the confirmed record (an
  extracted/C1-swap title must not have overridden it), and the projected
  state may keep only benign leftovers (OK-verdict or MISSING_*); any C2
  blocks (the stem-match reason was retired with its detector — see
  detectors.rule_c2_filename_title). A NEEDS_REVIEW leftover or EMPTY_BOOK
  blocks the pre-fill. Counted as `verified_prefilled` in the analyze
  summary.
  The DECIDED-prior carry path in `review_writer._handle` stamps the same
  flag on an already-accepted prior whose proposal projects clean (same
  guards: `keep` exempt, unconfirmed `llm:` proposals never auto-closed) —
  so a decided book that a re-analyze re-flags gets closed by the NEXT
  apply instead of cycling forever. Related: analyze skips the LLM
  entirely for decided priors (`llm_skip_ids`) — re-running analyze
  before apply must not re-buy the answers the entry already holds.
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
  proposed by the analyzer itself (`_build_proposed`'s C1 fallback, gated to
  the HIGH classic-swap shape only — MEDIUM heuristic hits and variant pairs
  stay proposal-free: a mechanical swap there would mangle a correct record
  or re-spell the same wrong one), and the
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
- **The `merge` action (C19) folds a duplicate folder into its survivor.**
  `action: merge` + `proposed.merge_into` (the survivor's library-relative
  path) comes from `bmf merge`/`analyze --merge` (see duplicates.py in the
  module layout for the detection policy). `apply_review` runs merges BEFORE
  its main loop — a survivor may be moved by its own placement later in the
  run — and the entry is pruned once merged (counted in
  `summary["merged_folders"]`; the placement-merge counter `merged` is a
  different thing). The OTHER proposed keys are the GUI merge panel's
  explicit picks and are the OPPOSITE of ignored: after
  `mover.merge_folders`'s automatic survivor-wins merge, the remaining
  proposed fields are stamped over the result via the merged_transform hook
  (a null = ∅ clears the field), and `proposed.cover_source`
  (survivor/loser/loser_epub) picks which cover.jpg ends up at the survivor
  — the loser's cover is spooled BEFORE the merge destroys the folder and
  swapped in AFTER (survivor's old cover → cover.jpg.bak); the bulk field
  edit still skips merge entries (picks are per-entry). `keep`-semantics do
  not apply: there is no retain variant of a merge.
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
- **The test suite runs on a private Xvfb, not on the desktop.** The Tk
  smoke tests in ``tests/test_gui.py`` build real widgets; a session-scoped
  autouse fixture in ``tests/conftest.py`` starts a dedicated Xvfb
  (``:99``–``:144``, first free display) and points ``DISPLAY`` at it for
  the whole run, so GUI test windows render into an invisible framebuffer
  instead of popping up over the user's work (the agent shell inherits
  ``DISPLAY=:1`` — without this every ``make test`` flashes ~25 windows on
  the desktop). Headless machines gain too: the Tk tests now run instead of
  skipping. Graceful fallback: no Xvfb binary or no free display → the old
  behaviour (desktop windows / skip). ``BMF_TEST_REAL_DISPLAY=1`` opts out
  for visually debugging a GUI test. When running smoke harnesses OUTSIDE
  pytest, wrap them in ``xvfb-run`` yourself.
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
- **CLI logging routes through the shared rich console** (`cli._ConsoleLogHandler`
  installed by `_setup_logging`): every progress bar renders on the module-level
  `cli.console`, and rich prints text ABOVE an active Live display only when the
  print goes through the SAME console the Progress uses — a raw stderr
  StreamHandler interleaved with and corrupted the bar. The handler emits the
  formatted record via `console.print(..., markup=False, highlight=False,
  soft_wrap=True)` (verbatim text, no width crop, stream semantics preserved).
  Never add a second `Console` for bars and never write logs straight to
  stderr in CLI paths — one console keeps bars and log lines coordinated.

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
