[English](../../how-to/enrichment.md) | **Čeština**

# Zapnutí CZ/SK obohacení

```bash
bmf analyze --databazeknih -o review.yaml            # per-run flag
# or persist it:
echo 'BMF_DATABAZEKNIH=1' >> .env
```

Vlastní instance [audiobookshelf_czech_metadata](https://github.com/stecik/audiobookshelf_czech_metadata)
(agregátor přes ~17 CZ audioknihových e-shopů za ABS custom-provider API
`/search` — metadata audio vydání, ideální pro audioknihovou knihovnu) se
zapne nastavením její base URL:

```bash
bmf analyze --abs-czech http://provider:8000 -o review.yaml
# or persist it (token only when the instance runs with auth enabled):
echo 'BMF_ABS_CZECH_URL=http://provider:8000' >> .env
echo 'BMF_ABS_CZECH_TOKEN=secret' >> .env
```

Pořadí vyhledávání při zapnutém obohacení (vyhrává první nalezená shoda):
databazeknih podle ISBN (pokud je povolen) → vlastní CZ provider podle názvu
(pokud je nastaveno URL) → databazeknih podle názvu (pokud je povolen) →
legie.info (pokud je povolen) → OpenLibrary podle ISBN → Google Books
podle ISBN → OpenLibrary podle názvu.
