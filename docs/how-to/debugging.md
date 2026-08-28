# Debugging a run

**English** | [Čeština](../cs/how-to/debugging.md)

- **Invalid JSON salvaged** — if you see `LLM JSON salvaged via json-repair
  (unescaped quotes/control chars fixed)`, the model returned slightly broken
  JSON and it was recovered. No action needed; this replaces the old 3-retry
  waste. If you see `LLM returned invalid JSON` (no salvage line), install the
  `[llm]` extra (`json-repair`).
- **Rate-limited** — `Z.AI rate-limited (429/1302 …); global cooldown Xs` is
  the circuit breaker doing its job; frequent ones mean raise the knobs in
  [tuning the LLM rate limit](llm.md#tuning-the-llm-rate-limit).
- **Overloaded** — `Z.AI service overloaded (429/1305); retrying …` means
  Z.AI's servers (usually the free flash model) are out of capacity — not
  your request rate. Slowing down will not help; the run retries through it
  and falls back to the paid model. `pausing model … for 180s` after a
  fleet-wide streak of rejections is the same story: that pool is saturated,
  so the run parks it briefly and every rate slot goes to the model that
  answers. Frequent evenings? See
  [tuning the LLM rate limit](llm.md#tuning-the-llm-rate-limit).
- **Usage-limited** — `Z.AI usage limit reached (429/1308 …)` disables that
  model for the rest of the run; the quota resets on Z.AI's side (hours).
- **Balance-rejected** — `Z.AI insufficient balance (429/1113 …)` means the
  billing check said no. Usually a short burst on the coding endpoint (even
  with quota left) that tracks flash-storm account throttling; it retries
  through and briefly (~30 s) pauses the model. If it persists, check your
  plan balance and the `ZAI_BASE_URL` pairing note in `.env.example`.
- **LLM idle** — `every model is paused right now` means no model is
  available (flash in a 1305 wave, the fallback in an 1113 burst): books
  flow through WITHOUT LLM proposals until a pause expires (~30 s–3 min).
  If it repeats constantly during peak hours, run `--no-llm-loop` (straight
  to the paid model) or come back off-peak.
- **Where did my review go?** — `review.yaml.bak` holds the pre-run state if
  the run was interrupted; rename it back to recover.
- **A book didn't get a proposal** — likely no usable first-page text (the LLM
  is skipped) or every cascade stage missed. Try `--verify-ok` to also audit
  books the structural detectors marked OK.
- **Verbose logs** — `bmf -v analyze ...` enables debug logging.
