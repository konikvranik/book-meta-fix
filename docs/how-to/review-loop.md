# Generate a review file (the main loop)

**English** | [Čeština](../cs/how-to/review-loop.md)

```bash
# Offline, no network, no LLM — the safe default
bmf analyze --skip-enrich -o review.yaml --limit 1000

# Add CZ/SK genres + metadata from databazeknih.cz (opt-in scraping)
bmf analyze --databazeknih -o review.yaml --limit 1000

# Add the LLM fallback for the hardest cases (needs ZAI_API_KEY)
bmf analyze --databazeknih --llm -o review.yaml --limit 1000
```

Analyze skips books whose metadata.json carries `verified: true` (the
persistent user-OK mark; see [edit-and-apply.md](edit-and-apply.md)) and
pre-fills `verified: true` itself when its own proposal completes a book,
so fixed books never come back. It also checks each book's LOCATION
(C13): a misplaced-but-healthy book gets a pre-filled `action: accept`
move proposal. `--no-check-location` skips that check; `--recheck-ok`
clears the verified flags.

`review.yaml` is written **incrementally** — `tail -f review.yaml` to watch
proposals arrive. Prior `review.yaml` is moved to `review.yaml.bak` on start;
on Ctrl-C the `.bak` is kept so you can recover. Every command runs an internal
scan via the SQLite cache (`bmf_cache.db`), so you never need to run `bmf scan`
first.

## Performance on NFS

The scan and the detector's cover analysis are the two fixed costs of every
analyze run over a large library on NFS, and both are mitigated:

- The scan (tree walk + per-folder metadata reads) runs in a small thread
  pool — `--scan-workers` / `BMF_SCAN_WORKERS`, default 8, `1` = the old
  serial scan. On the reference library (~5,400 books, NFS v3) the walk
  alone dropped from ~38 s to a few seconds.
- Cover pixel analysis (the C11 generated-cover rule) is the most expensive
  detector step per book. Its verdict is cached in `bmf_cache.db`
  (`covers` table) keyed by the cover's mtime+size, so an unchanged
  `cover.jpg` is never decoded twice — not within one run (where the same
  cover is examined up to 4×), nor across runs. Delete `bmf_cache.db` (or
  run with `--no-cache`) to force a full re-analysis after swapping covers
  in place.

