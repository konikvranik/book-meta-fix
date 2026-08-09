# book-meta-fix (bmf)

Detect and fix metadata of ebooks in a Calibre-style library.

Designed for a ~5,000-book library where Calibre mis-classified many records
(swapped author/title, filename-as-title, encoding corruption, translators
listed as authors). **Source of truth is `metadata.json`** (Audiobookshelf
manifest); on write, both `metadata.json` and `metadata.opf` are updated so
Audiobookshelf and Kavita pick up the fixes on rescan.

## Status

- [x] Phase 1 — Scan (`bmf scan`)
- [ ] Phase 2 — Detect (`bmf detect`) — C1–C10 rules
- [ ] Phase 3 — Verify (`bmf verify`) — content vs metadata
- [ ] Phase 4 — Enrich (obalkyknih / Google Books / OpenLibrary)
- [ ] Phase 5 — Write + YAML review (`bmf apply`)
- [ ] Phase 6 — LLM reconciliation (Z.AI, for C1/C4/C5)
- [ ] Phase 7 — Tests + docs

## Quick start

```bash
make dev-install        # create .venv, install package + dev deps
make scan               # scan default library (/mnt/share_nfs/Shared eBooks)
make scan LIBRARY=/path # scan a different library
```

## Library layout expected

```
<library>/
├── <Author>/
│   └── <Title> (<calibre_id>)/
│       ├── metadata.json     # primary source (Audiobookshelf manifest)
│       ├── metadata.opf      # fallback source (Calibre OPF 2.0)
│       ├── <Title> - <Author>.epub
│       ├── <Title> - <Author>.pdb
│       └── cover.jpg
```

Excluded automatically: `temp_calibre/`, `calibre-*/`, `~$*` (Word lock files),
dotfiles.

## Configuration

Settings resolve from (highest precedence first):
1. CLI flags (`--library`, ...)
2. Environment variables (`BMF_LIBRARY`, `ZAI_API_KEY`, ...)
3. Built-in defaults

| Variable | Default | Purpose |
|---|---|---|
| `BMF_LIBRARY` | `/mnt/share_nfs/Shared eBooks` | Library root |
| `BMF_CACHE` | `bmf_cache.db` | SQLite cache path |
| `BMF_REVIEW` | `review.yaml` | Review file path |
| `ZAI_API_KEY` | — | Z.AI API key (LLM, optional) |
| `ZAI_MODEL` | `glm-4.6` | Z.AI model |

## Optional external tools

- **`pdftotext` / `pdfinfo`** (poppler-utils) — PDF content & metadata extraction
- **`ebook-meta`** (calibre) — fallback extractor for mobi/pdb/rtf

The tool works without them, but with reduced format coverage.
