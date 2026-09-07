# Enabling CZ/SK enrichment

**English** | [Čeština](../cs/how-to/enrichment.md)

```bash
bmf analyze --databazeknih -o review.yaml            # per-run flag
# or persist it:
echo 'BMF_DATABAZEKNIH=1' >> .env
```

A self-hosted [audiobookshelf_czech_metadata](https://github.com/stecik/audiobookshelf_czech_metadata)
instance (an aggregator over ~17 CZ audiobook storefronts behind ABS's
custom-provider `/search` API — audio-edition metadata, ideal for an
audiobook library) is enabled by pointing at its base URL:

```bash
bmf analyze --abs-czech http://provider:8000 -o review.yaml
# or persist it (token only when the instance runs with auth enabled):
echo 'BMF_ABS_CZECH_URL=http://provider:8000' >> .env
echo 'BMF_ABS_CZECH_TOKEN=secret' >> .env
```

Lookup order when enrichment is on (first hit wins): databazeknih by ISBN
(if enabled) → the self-hosted CZ provider by title (if a URL is configured)
→ databazeknih by title (if enabled) → legie.info (if enabled) → OpenLibrary
by ISBN → Google Books by ISBN → OpenLibrary by title.
