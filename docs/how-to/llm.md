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
| `1305` The service may be temporarily overloaded | **Z.AI's** server capacity (chronic on the free flash models — nothing you did) | Short interval-spaced retries (no global cooldown), then fall back to the paid model; a fleet-wide streak of consecutive rejections pauses the model for ~3 min |
| `1113` Insufficient balance or no resource package | Billing check — hard when the quota is really gone, but on the coding endpoint it also fires with quota left when the account's ~5-request concurrency ceiling is blown (the fallback herd storming the paid model at once) | The in-flight cap prevents the storms; leftovers get the transient streak handling of 1305 with a short ~30 s pause. If it persists even at `--llm-max-inflight 1`, check the plan / endpoint pairing in `.env.example` |
| `1308` Usage limit reached | The model's usage quota is exhausted | The model is skipped for the rest of the run |

Three layers keep you under the 1302 limit (see
[architecture.md → Concurrency model](../architecture.md#concurrency-model)):

1. **Leaky-bucket smoother** — constant aggregate RPM.
2. **In-flight cap** — the Z.AI coding plan admits only **~5 concurrent
   requests per account** (Z.AI publishes no exact numbers: the limits are
   tier-based and dynamic per the Devpack usage policy; measured: a 12-deep
   simultaneous spike drew 7× 429/1302 while a 6-deep burst passed, and
   interactive clients — ZCode/chat — draw from the same ceiling). The bucket
   spaces call *starts* but not call *depth*: with 10 workers and
   multi-second reasoning calls the fallback herd can still exceed the
   ceiling, and the storms also surface as false `1113` "insufficient
   balance" with quota clearly left (Z.AI's own FAQ acknowledges 1113 firing
   on a purchased package). A semaphore (`--llm-max-inflight`) caps how many
   requests are running at once — workers queue locally instead of being
   rejected. Flash-family models hold a stricter sub-cap of `min(2, cap)`:
   the free pool is chronically saturated and community reports put its
   concurrency as low as 1, so a deep flash herd only feeds the 1305 storm
   while squeezing the paid fallback out of the shared slots. With a
   coding-plan `ZAI_BASE_URL`, glm-4.x flash is additionally routed to the
   **PaaS endpoint** (`ZAI_FLASH_BASE_URL`, empty = auto, `off` = disable):
   the two endpoints' concurrency ceilings are independent (measured: 6
   flash@PaaS + 6 glm-5.3@coding simultaneously left the coding side at its
   usual ~5), and flash is free there — its own pool, zero coding-plan
   credits. Paid models stay on the coding endpoint (they 1113 on PaaS — no
   cash balance), and glm-5.x flash is not served on PaaS (400/1210), so it
   stays too. The whole rate machinery (drip, cooldown, in-flight gate) is
   per endpoint too: pressure on one endpoint never throttles the other.
   The global cap
   is **adaptive**: an external client on the same plan (ZCode, chat) is
   invisible to bmf, so ceiling pressure is observed, not predicted — each
   1302 / false 1113 yields one in-flight slot (log: `in-flight cap 3 -> 2`)
   and ~20 clean responses earn it back. Under the hood all calls share one
   pooled keep-alive HTTP connection set (keep-alive 60 s outlives the
   30 s balance pauses, so no TLS re-handshake after each) with a 180 s read
   timeout — a hung call cannot squat a scarce in-flight slot for the SDK's
   600 s default.
3. **Global 429/1302 cooldown** — Z.AI's free tier cascade-throttles *every*
   model when one gets a 429, so when *any* worker sees a 1302, *all* workers
   pause.

| Knob | CLI | Env | Default |
|---|---|---|---|
| Steady interval (s) between calls | `--llm-min-interval` | `BMF_LLM_MIN_INTERVAL` | `2.0` (~30 RPM) |
| Max requests running at once | `--llm-max-inflight` | `BMF_LLM_MAX_INFLIGHT` | `3` (account ceiling ~5, flash sub-cap `min(2, ·)`) |
| Burst capacity (calls per interval) | `--llm-burst` | `BMF_LLM_BURST` | `1` (even drip) |
| Base 429 cooldown (s) | `--llm-rate-limit-base` | `BMF_LLM_RATE_LIMIT_BASE` | `5` |
| Max 429 cooldown cap (s) | `--llm-rate-limit-max` | `BMF_LLM_RATE_LIMIT_MAX` | `60` |

The leaky bucket is a **count-per-time** limiter, not a concurrency cap —
that is what `--llm-max-inflight` is for. With
the default `--llm-burst 1` it is a pure even drip — exactly one call starts
every `--llm-min-interval` seconds, evenly spaced, no bunching (5 calls in one
second then nothing is exactly what trips the limit). A burst >1 lets several
calls fire in the same second; raise it only with confirmed rate headroom.
The interval is **adaptive**: it is only a floor. Z.AI's real request ceiling
moves (four 1302s in two minutes were observed at a steady 30 RPM one
evening), so 1302/1113 stretch the drip (×1.3, capped at ~4× the floor) and
every successful response eases it back — within a minute or two it settles
at whatever the account is actually allowed right now. When every model is
paused at once you'll see a once-a-minute `every model is paused` line and
books flow through without LLM proposals until a pause expires.

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
capacity, not your request rate — slowing down will not help. The run retries
through it, falls back to the paid model, and a fleet-wide streak of
consecutive rejections parks the overloaded model for ~3 minutes
(`pausing model … for 180s`). If it dominates your evenings, switch the loop
model to a paid one:

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
