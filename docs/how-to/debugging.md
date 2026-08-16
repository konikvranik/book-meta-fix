# Debugging a run

**English** | [Čeština](../cs/how-to/debugging.md)

- **Invalid JSON salvaged** — if you see `LLM JSON salvaged via json-repair
  (unescaped quotes/control chars fixed)`, the model returned slightly broken
  JSON and it was recovered. No action needed; this replaces the old 3-retry
  waste. If you see `LLM returned invalid JSON` (no salvage line), install the
  `[llm]` extra (`json-repair`).
- **Rate-limited** — `Z.AI rate-limited (429); global cooldown Xs` is the
  circuit breaker doing its job. Frequent ones mean raise the knobs in
  [tuning the LLM rate limit](llm.md#tuning-the-llm-rate-limit).
- **Where did my review go?** — `review.yaml.bak` holds the pre-run state if
  the run was interrupted; rename it back to recover.
- **A book didn't get a proposal** — likely no usable first-page text (the LLM
  is skipped) or every cascade stage missed. Try `--verify-ok` to also audit
  books the structural detectors marked OK.
- **Verbose logs** — `bmf -v analyze ...` enables debug logging.
