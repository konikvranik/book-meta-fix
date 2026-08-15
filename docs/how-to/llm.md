# Running with the LLM fallback

```bash
bmf analyze --llm -o review.yaml                       # default loop: Flash→final
bmf analyze --llm --llm-model glm-4.6                  # cheaper, weaker CZ
bmf analyze --llm --no-llm-loop --llm-model glm-4.5-flash   # single cheap call
bmf analyze --llm --llm-reasoning-effort max           # slow/costly, hard batch
```

The LLM is a **last resort** — the deterministic stages (text mining, online
lookup) run first and the LLM only sees books they missed *and* that have
usable first-page text. Output is always a *proposal* for `review.yaml`,
never auto-applied. See [concepts.md → The LLM self-correction loop](../concepts.md#the-llm-self-correction-loop).

## Tuning the LLM rate limit

Z.AI's coding plan enforces a dynamic requests-per-minute limit; 429
`Rate limit reached for requests` (code 1302) trips when too many calls land
in a rolling window. Two layers keep you under it (see
[architecture.md → Concurrency model](../architecture.md#concurrency-model)):

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

See also [choosing an LLM model](llm-models.md).
