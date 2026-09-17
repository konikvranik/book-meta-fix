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
   The stat-only walk needs no total (folders are discovered while
   descending), so its progress bar pulses and counts the folders checked —
   over NFS this phase alone takes tens of seconds for a few thousand books.
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
   cover audit and cover-clearing pass show one too). (There is also
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

## Series deletions: pushed beyond the scan

One change a rescan *cannot* deliver: a **deleted** series. BookScanner
never removes a series — an empty `series` list in `metadata.json` is "no
information" to it, not "remove" — so a series you dropped in bmf (apply
wrote `series: []`) would stay in the ABS database forever, no matter how
many rescans run (measured on ABS 2.36.0: 46 books kept their junk series
after a per-item rescan that demonstrably ran).

`abs-rescan` therefore also diffs the series of every matched book against
the ABS rows and clears the leftovers through the metadata-update API
(`PATCH /api/items/{id}/media` with `metadata.series: []` — the only code
path that removes a series; ABS itself then cleans up series rows left
without books). Dry-run lists the stale series, `--apply` clears them
before the rescan. Scope notes:

- Only the *deletion* case is patched. A rename or a changed volume number
  is the scan's own job — it re-reads `metadata.json` right after.
- An index containing a space (`"John Sinclair #Speciál 07"`) cannot be
  pushed at all: the ABS parser keeps such a string whole as the series
  NAME (its sequence pattern wants a single word after `#`), and a manual
  PATCH splitting it would be re-glued by the next scan. Rename the series
  or its index in bmf instead.

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
years old, `--since` does not filter it). The audit probes one stored
cover path per item — another slow NFS sweep on a big library, tracked by
its own progress bar:

- **dry-run** (default): lists the broken rows — item title, stored cover
  path, reason (not an image file / file missing / file not decodable /
  calibre-generated placeholder).
- **`--apply`**: nulls each broken row via `DELETE /api/items/{id}/cover`
  and adds the items to the rescan,
  so ABS picks a real cover again — an image file in the folder (cover.*
  preferred) or the e-book's embedded cover. A row is "broken" when its
  target has a non-image extension, when it maps into your library and the
  file no longer exists (e.g. it was renamed to `.bak` by
  `bmf strip-covers`), or when no decoder reads it (a 0-byte leftover).
  Targets outside the library folders — ABS's own uploaded/cached covers
  under `/metadata/items/…` — are not on your mount: the audit fetches
  their bytes through the API and clears the row only when the image
  carries calibre's `Generated cover` marker (a cached screenshot
  placeholder). Marker-only on purpose — a cover you uploaded through the
  ABS UI is never touched.
- **stale cache**: the server-side cover *cache* can outlive the row.
  Measured on ABS 2.36.0: a `DELETE` on an already-empty row is a no-op
  that never reaches the cache-purge branch, and a per-item rescan does
  not drop a cached cover either — a book whose every disk cover source
  was cleaned up kept serving the old generated cover forever. When the
  cached bytes are still served after the delete, `--apply` runs the
  upload+delete dance (a fixed 2×3 stub upload sets the row again so the
  following delete takes the purge branch; the stub file the upload drops
  into the book folder on store-cover-with-item libraries is removed right
  after). The audit also catches the *healthy row, junk cache* shape:
  when a mapped disk cover is a small but real thumbnail (< 400 px on the
  shorter side — where stale caches concentrate), it fetches the served
  bytes once and breaks the row when they show an unambiguous junk page
  shape (calibre marker / vendor placeholder / text page / document scan).
  A minimalist few-colour cover is an accepted real cover and stays.

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
