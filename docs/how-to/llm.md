# Running with the LLM fallback

**English** | [Čeština](../cs/how-to/llm.md)

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

Z.AI returns HTTP 429 for three different conditions, and `bmf` reacts to
each differently (the sub-code is always logged):

| Sub-code | Meaning | `bmf` reaction |
|---|---|---|
| `1302` Rate limit reached for requests | **Your** request rate tripped the RPM window | Global cooldown: all workers pause, escalating `base * 2^(n-1)` (5, 10, 20, …), honouring the server's `Retry-After` when longer, capped at `max` |
| `1305` The service may be temporarily overloaded | **Z.AI's** server capacity (chronic on the free flash models — nothing you did) | Short interval-spaced retries (no global cooldown), then fall back to the paid model; repeated fully-failed calls pause the overloaded model for ~10 min |
| `1113` Insufficient balance or no resource package | Billing check — hard when the quota is really gone, but on the coding endpoint it also fires intermittently under concurrent load with quota left | Same transient handling as 1305 (retry, then time-boxed pause). If it persists, check the plan / endpoint pairing in `.env.example` |
| `1308` Usage limit reached | The model's usage quota is exhausted | The model is skipped for the rest of the run |

Two layers keep you under the 1302 limit (see
[architecture.md → Concurrency model](../architecture.md#concurrency-model)):

1. **Leaky-bucket smoother** — constant aggregate RPM.
2. **Global 429/1302 cooldown** — Z.AI's free tier cascade-throttles *every*
   model when one gets a 429, so when *any* worker sees a 1302, *all* workers
   pause.

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

**If you are still hitting 1302** (you'll see `Z.AI rate-limited (429/1302
…); global cooldown …s across all workers` in the log), slow the drip and
lengthen the cooldown — burst is already 1 by default:

```bash
# Slower drip: 4s apart (15 RPM), longer cooldown
bmf analyze --llm --llm-min-interval 4.0 --llm-rate-limit-base 10

# Slow it down hard for a free tier
bmf analyze --llm --llm-min-interval 4.0 --llm-rate-limit-base 15 --llm-rate-limit-max 120
```

**If the log is full of `429/1305` overload lines instead**, that is Z.AI's
capacity, not your request rate — slowing down will not help. The run already
retries through it and falls back to the paid model, and after repeated full
failures it pauses the overloaded model for ~10 minutes (`pausing model … for
600s`). If it dominates your evenings, switch the loop model to a paid one:

```bash
bmf analyze --llm --llm-model glm-5.3
```

**If you see `429/1308 usage limit reached`**, the model's usage quota is
gone until it resets on Z.AI's side (typically hours); `bmf` automatically
skips that model for the rest of the run.

**If you have a higher tier and want speed**, lower the interval and the
cooldown base:

```bash
bmf analyze --llm --llm-min-interval 1.0 --llm-rate-limit-base 3
```

The leaky bucket is **decoupled from `--workers`**: cheap I/O (extraction,
enrichment) still runs at the full worker count; only LLM calls are smoothed.

See also [choosing an LLM model](llm-models.md).
