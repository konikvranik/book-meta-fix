# Push changes into Audiobookshelf (abs-rescan)

**English** | [Čeština](../cs/how-to/abs-rescan.md)

```bash
bmf abs-rescan                     # dry-run: list changed books + the ABS mapping
bmf abs-rescan --apply             # trigger the per-item rescan on the ABS server
bmf abs-rescan --since 2h --apply  # narrow the window
bmf abs-rescan --fix-covers --apply # also clear broken stored cover rows (see below)
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
3. Asks ABS to re-scan them one by one (`POST /api/items/{id}/scan`). The
   per-item endpoint runs the scan synchronously and returns the result, so
   when the command finishes, the metadata is already re-read. Each POST
   waits for ABS to re-read that one item, so even in parallel a few
   thousand items take minutes — the items are scanned by a small worker
   pool (`--abs-workers`, default 4, env `BMF_ABS_WORKERS`; `1` = serial)
   and a progress bar with an ETA tracks the run (the `--fix-covers`
   cover-clearing pass shows one too). (There is also
   a batch endpoint — it answers 200 immediately and is supposed to scan in
   the background, but on a real server it was measured accepting ~1100 ids
   and processing none, so bmf does not use it.)

Books that cannot be matched (brand-new or moved to a path ABS has never
seen) have no item id to scan, so after the per-item loop `--apply` also
fires a **plain library scan** (`POST /api/libraries/{id}/scan` — the
endpoint is async server-side, the scan runs on ABS in the background and
fully reads metadata.json for items it discovers). Re-run `bmf abs-rescan`
once it finishes if any books remain unmatched. In a dry-run the old manual
hint is printed instead.

`--force-all` skips the mapping entirely and sends
`POST /api/libraries/{id}/scan?force=1` — ABS re-reads every item. Use it
when you suspect the mapping missed something; it is heavier on the server.

## `--fix-covers`: repair broken covers stored in the ABS database

The other half of the cover cleanup. ABS's database stores each item's
cover path (`media.coverPath`), and a row written by an **older ABS build**
can point at a file that is not an image at all — typically
`metadata.json` or `cover.html`. Current ABS builds neither write such
rows nor heal them: the scanner only re-picks a cover when the row is
empty or its file vanished, and a `metadata.json` neither vanishes nor
counts as an image file. So every cover-cache refresh feeds the file to
ffmpeg — the `[FfmpegHelpers] Resize Image Error … Invalid data found
when processing input` log lines.

`--fix-covers` adds a pass over **all** items (a stale row is usually
years old, `--since` does not filter it):

- **dry-run** (default): lists the broken rows — item title, stored cover
  path, reason (not an image file / file missing).
- **`--apply`**: nulls each broken row via `DELETE /api/items/{id}/cover`
  (which also purges ABS's cover cache) and adds the items to the rescan,
  so ABS picks a real cover again — an image file in the folder (cover.*
  preferred) or the e-book's embedded cover. A row is "broken" when its
  target has a non-image extension, or when it maps into your library and
  the file no longer exists (e.g. it was renamed to `.bak` by
  `bmf strip-covers`). Uploaded covers stored under ABS's own
  `/metadata/items/…` directory are left alone.

Run the file-side cleanup FIRST: `bmf strip-covers --invalid --apply`,
then `bmf abs-rescan --fix-covers --apply`. A folder that still holds an
unreadable `cover.jpg` would only get it re-picked by the rescan. The full
loop then looks like:

```bash
bmf strip-covers --invalid --apply   # 1. junk cover files on disk -> .bak
bmf analyze --apply review.yaml      # 2. refetch real covers (MISSING_COVER)
bmf abs-rescan --fix-covers --apply  # 3. clear stale DB rows + rescan
```

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
