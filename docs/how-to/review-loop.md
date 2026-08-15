# Generate a review file (the main loop)

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
