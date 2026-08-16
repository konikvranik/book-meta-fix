[English](../../how-to/review-loop.md) | **Čeština**

# Vygenerování revizního souboru (hlavní smyčka)

```bash
# Offline, no network, no LLM — the safe default
bmf analyze --skip-enrich -o review.yaml --limit 1000

# Add CZ/SK genres + metadata from databazeknih.cz (opt-in scraping)
bmf analyze --databazeknih -o review.yaml --limit 1000

# Add the LLM fallback for the hardest cases (needs ZAI_API_KEY)
bmf analyze --databazeknih --llm -o review.yaml --limit 1000
```

`review.yaml` se zapisuje **inkrementálně** — pomocí `tail -f review.yaml`
sledujte, jak návrhy přibývají. Předchozí `review.yaml` se při startu
přesune na `review.yaml.bak`; po Ctrl-C zůstane `.bak` zachován, takže se
můžete obnovit. Každý příkaz spouští interní skenování přes SQLite cache
(`bmf_cache.db`), takže nikdy nemusíte nejdřív spouštět `bmf scan`.
