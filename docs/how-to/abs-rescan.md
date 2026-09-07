# Push changes into Audiobookshelf (abs-rescan)

**English** | [Čeština](../cs/how-to/abs-rescan.md)

```bash
bmf abs-rescan                     # dry-run: list changed books + the ABS mapping
bmf abs-rescan --apply             # trigger the per-item rescan on the ABS server
bmf abs-rescan --since 2h --apply  # narrow the window
bmf abs-rescan --force-all --apply # escape hatch: force-rescan the whole library
```

Why this exists: Audiobookshelf keeps its **own database** and only re-reads
`metadata.json`/`metadata.opf` when it scans a library item — and a plain
"Scan library" *skips every folder it considers unchanged* (mtime gate; when
ABS mounts the library over NFS, its attribute cache can mask even a fresh
mtime). So after `bmf apply --apply` the fixes sit on disk but ABS keeps
showing the old title/author/series until the items are re-scanned.
`bmf abs-rescan` closes that gap with the ABS API, which re-reads an item
unconditionally.

## One-time setup

```bash
# .env — set the BASE url, not an endpoint
BMF_ABS_URL=http://abs.lan:13378
BMF_ABS_TOKEN=<admin API token>    # ABS web UI: Settings -> Users -> API key
# BMF_ABS_LIBRARY=Books            # only when the server hosts several book libraries
```

The token must belong to an **admin** user — the scan endpoints reject
anything else with 403.

## What it does

1. Finds book folders in your library whose files changed within `--since`
   (default 24 h) — the max file mtime, so a swapped `cover.jpg` counts too.
2. Maps each folder to an ABS library item: exact path → `relPath` (the usual
   case: the same storage mounted under different prefixes) → a unique
   folder-name match (covers books moved by apply's placement).
3. Asks ABS to re-scan them (`POST /api/items/batch/scan`, one call; the
   server scans in the background — refresh the web UI in a moment).
   Older ABS builds without the batch endpoint fall back to per-item calls.

Books that cannot be matched (brand-new or moved to a path ABS has never
seen) are listed with a hint: run one plain library scan in ABS so it learns
the new paths, then re-run `bmf abs-rescan`.

`--force-all` skips the mapping entirely and sends
`POST /api/libraries/{id}/scan?force=1` — ABS re-reads every item. Use it
when you suspect the mapping missed something; it is heavier on the server.

## Typical workflow

```bash
bmf analyze            # generate review.yaml
bmf gui                # decide the entries
bmf apply --apply review.yaml
bmf abs-rescan --apply # push the written changes into ABS
```

Note the flip side: an edit made **in the ABS web UI** rewrites
`metadata.json` from the ABS database — re-run `bmf` tooling afterwards, or
the two sources of truth drift apart.
