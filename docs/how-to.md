# How-to

Practical recipes. For *why* things work see [concepts.md](concepts.md); for
the module/data-flow detail see [architecture.md](architecture.md); for the
full command reference see the [README](../README.md).

Every mutating command (`apply`, `organize`, `epubgen`, `crosscheck`) is a
**dry-run by default** — add `--apply` to actually change the filesystem.

## Setup

```bash
make dev-install                  # create .venv, install package + dev + pdf + llm extras
cp .env.example .env              # then edit .env: set BMF_LIBRARY, ZAI_API_KEY, ...
```

`make dev-install` installs `pip install -e ".[pdf,llm,dev]"`. The `[llm]`
extra pulls `openai` (the Z.AI client) and `json-repair` (salvages invalid
LLM JSON). Without it, `--llm` is unavailable and the LLM JSON salvage falls
back to the built-in (weaker) repair.

External tools are optional but extend coverage: `pdftotext`/`pdfinfo`
(poppler) for PDFs, `ebook-convert`/`ebook-meta` (calibre) for EPUB
generation, `pandoc` for txt/doc, `tesseract` for OCR of scanned PDFs.

## 1. First look (no writes)

```bash
bmf report --limit 500            # detector breakdown C1–C11 + samples (statistics only)
bmf report --category C2 --samples 5
```

## 2. Generate a review file (the main loop)

```bash
# Offline, no network, no LLM — the safe default
bmf analyze --skip-enrich -o review.yaml --limit 1000

# Add CZ/SK genres + metadata from databazeknih.cz (opt-in scraping)
bmf analyze --databazeknih -o review.yaml --limit 1000

# Add the LLM fallback for the hardest cases (needs ZAI_API_KEY)
bmf analyze --databazeknih --llm -o review.yaml --limit 1000
```

`review.yaml` is written **incrementally** — `tail -f review.yaml` to watch
proposals arrive. Prior `review.yaml` is moved to `review.yaml.bak` on start;
on Ctrl-C the `.bak` is kept so you can recover. Every command runs an internal
scan via the SQLite cache (`bmf_cache.db`), so you never need to run `bmf scan`
first.

## 3. Edit + apply

```bash
$EDITOR review.yaml               # set action: accept|reject|swap|edit per entry
bmf apply review.yaml             # dry-run preview
bmf apply --apply review.yaml     # actually write metadata.json + metadata.opf
```

Actions: `accept` (apply proposed), `reject` (leave), `swap` (author↔title for
C1), `edit` (apply only the fields under `edited:`).

## 4. Organize the library

```bash
bmf organize                       # dry-run: OK→clean path, broken→needfix/
bmf organize --apply
bmf organize --pattern "{author_sort}/{title} ({id})" --apply
bmf organize --needfix-dir "_problems" --apply
```

OK/VERIFIED books move to the pattern; broken books move to
`<library>/<needfix-dir>/<original relative path>` (provenance preserved).

## 5. Generate missing EPUBs

```bash
bmf epubgen                        # dry-run
bmf epubgen --apply                # generate from best sibling format
```

## 6. Cross-check multi-format folders

```bash
bmf crosscheck                     # dry-run: find rogue format files (a different book)
bmf crosscheck --apply             # quarantine rogues into isolated needfix/ folders
```

## Enabling CZ/SK enrichment

```bash
bmf analyze --databazeknih -o review.yaml            # per-run flag
# or persist it:
echo 'BMF_DATABAZEKNIH=1' >> .env
```

Lookup order when enrichment is on (first hit wins): databazeknih (if enabled)
→ OpenLibrary by ISBN → Google Books by ISBN → OpenLibrary by title.

## Running with the LLM fallback

```bash
bmf analyze --llm -o review.yaml                       # default loop: Flash→final
bmf analyze --llm --llm-model glm-4.6                  # cheaper, weaker CZ
bmf analyze --llm --no-llm-loop --llm-model glm-4.5-flash   # single cheap call
bmf analyze --llm --llm-reasoning-effort max           # slow/costly, hard batch
```

The LLM is a **last resort** — the deterministic stages (text mining, online
lookup) run first and the LLM only sees books they missed *and* that have
usable first-page text. Output is always a *proposal* for `review.yaml`,
never auto-applied. See [concepts.md → The LLM self-correction loop](concepts.md#the-llm-self-correction-loop).

### Tuning the LLM rate limit

Z.AI's coding plan enforces a dynamic requests-per-minute limit; 429
`Rate limit reached for requests` (code 1302) trips when too many calls land
in a rolling window. Two layers keep you under it (see
[architecture.md → Concurrency model](architecture.md#concurrency-model)):

1. **Leaky-bucket smoother** — constant aggregate RPM.
2. **Global 429 cooldown** — Z.AI's free tier cascade-throttles *every* model
   when one gets a 429, so when *any* worker sees a 429, *all* workers pause.

| Knob | CLI | Env | Default |
|---|---|---|---|
| Steady interval (s) between calls | `--llm-min-interval` | `BMF_LLM_MIN_INTERVAL` | `2.0` (~30 RPM) |
| Burst capacity (calls per interval) | `--llm-burst` | `BMF_LLM_BURST` | `1` (even drip) |
| Base 429 cooldown (s) | `--llm-rate-limit-base` | `BMF_LLM_RATE_LIMIT_BASE` | `5` |
| Max 429 cooldown cap (s) | `--llm-rate-limit-max` | `BMF_LLM_RATE_LIMIT_MAX` | `60` |

The leaky bucket is a **count-per-time** limiter, not a concurrency cap. With
the default `--llm-burst 1` it is a pure even drip — exactly one call starts
every `--llm-min-interval` seconds, evenly spaced, no bunching (5 calls in one
second then nothing is exactly what trips the limit). A burst >1 lets several
calls fire in the same second; raise it only with confirmed rate headroom.

The cooldown escalates `base * 2^(n-1)` (5, 10, 20, …) with consecutive 429s,
honours the server's `Retry-After` when it is longer, and is capped at `max`.

**If you are still hitting 429s** (you'll see `Z.AI rate-limited (429); global
cooldown …s across all workers` in the log), slow the drip and lengthen the
cooldown — burst is already 1 by default:

```bash
# Slower drip: 4s apart (15 RPM), longer cooldown
bmf analyze --llm --llm-min-interval 4.0 --llm-rate-limit-base 10

# Slow it down hard for a free tier
bmf analyze --llm --llm-min-interval 4.0 --llm-rate-limit-base 15 --llm-rate-limit-max 120
```

**If you have a higher tier and want speed**, lower the interval and the
cooldown base:

```bash
bmf analyze --llm --llm-min-interval 1.0 --llm-rate-limit-base 3
```

The leaky bucket is **decoupled from `--workers`**: cheap I/O (extraction,
enrichment) still runs at the full worker count; only LLM calls are smoothed.

## Debugging a run

- **Invalid JSON salvaged** — if you see `LLM JSON salvaged via json-repair
  (unescaped quotes/control chars fixed)`, the model returned slightly broken
  JSON and it was recovered. No action needed; this replaces the old 3-retry
  waste. If you see `LLM returned invalid JSON` (no salvage line), install the
  `[llm]` extra (`json-repair`).
- **Rate-limited** — `Z.AI rate-limited (429); global cooldown Xs` is the
  circuit breaker doing its job. Frequent ones mean raise the knobs above.
- **Where did my review go?** — `review.yaml.bak` holds the pre-run state if
  the run was interrupted; rename it back to recover.
- **A book didn't get a proposal** — likely no usable first-page text (the LLM
  is skipped) or every cascade stage missed. Try `--verify-ok` to also audit
  books the structural detectors marked OK.
- **Verbose logs** — `bmf -v analyze ...` enables debug logging.

## Choosing an LLM model

Five settings were measured on hard CZ/SK books (`scripts/llm_experiment.py`):

| Variant | ok% | out tok | reasoning | Cost |
|---|---|---|---|---|
| **glm-5.2 reasoning_effort=low (default)** | 100% | 346 | yes | 1.40 / 4.40 |
| glm-4.6 thinking=disabled | 100% | 139 | no | 0.60 / 2.20 |
| glm-4.5-air thinking=disabled | 100% | 122 | no | 0.20 / 1.10 |
| glm-4.5-flash | 100% | 96 | no | free |
| glm-4.7-flash | 100% | 147 | no | free |

Non-reasoning models use 3–4× fewer tokens but hallucinate more on CZ/SK series
and diacritics. GLM-5.2 `low` is the default; switch when you know what you're
doing.

## Configuration

Settings resolve (highest precedence first): **CLI flag → env var → `.env`
file (walked up from CWD) → built-in default**. Copy `.env.example` to `.env`
and edit. Key variables: `BMF_LIBRARY`, `BMF_CACHE`, `ZAI_API_KEY`,
`ZAI_BASE_URL` (coding plan vs PaaS — see `.env.example`), `ZAI_MODEL`,
`BMF_LLM_MIN_INTERVAL`, `BMF_LLM_BURST`, `BMF_LLM_RATE_LIMIT_BASE`,
`BMF_LLM_RATE_LIMIT_MAX`.

## Running the tests

```bash
make test                         # full suite
.venv/bin/pytest tests/test_llm_rate_limit.py -q   # one module
.venv/bin/pytest -k cooldown                       # by name
make lint                         # ruff check src tests
```

Tests are per-module (`tests/test_<module>.py`) and use no network — online
sources, the LLM, and HTTP are all stubbed/mocked.
