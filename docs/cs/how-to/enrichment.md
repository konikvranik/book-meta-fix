[English](../../how-to/enrichment.md) | **Čeština**

# Zapnutí CZ/SK obohacení

```bash
bmf analyze --databazeknih -o review.yaml            # per-run flag
# or persist it:
echo 'BMF_DATABAZEKNIH=1' >> .env
```

Pořadí vyhledávání při zapnutém obohacení (vyhrává první nalezená shoda):
databazeknih (pokud je povolen) → OpenLibrary podle ISBN → Google Books
podle ISBN → OpenLibrary podle názvu.
