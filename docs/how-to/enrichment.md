# Enabling CZ/SK enrichment

**English** | [Čeština](../cs/how-to/enrichment.md)

```bash
bmf analyze --databazeknih -o review.yaml            # per-run flag
# or persist it:
echo 'BMF_DATABAZEKNIH=1' >> .env
```

Lookup order when enrichment is on (first hit wins): databazeknih (if enabled)
→ OpenLibrary by ISBN → Google Books by ISBN → OpenLibrary by title.
