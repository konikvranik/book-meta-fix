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

Analyze přeskočí knihy, jejichž metadata.json nese `verified: true` (trvalá
značka uživatele „OK“; viz [edit-and-apply.md](edit-and-apply.md)), a sám
předvyplní `verified: true`, když jeho vlastní návrh knihu kompletně
doplní — opravené knihy se tak do review nevracejí. Zároveň kontroluje
UMÍSTĚNÍ každé knihy (C13): zdravá, ale špatně umístěná kniha dostane
předvyplněný `action: accept` s návrhem přesunu. `--no-check-location`
kontrolu umístění vypne; `--recheck-ok` smaže příznaky verified.

`review.yaml` se zapisuje **inkrementálně** — pomocí `tail -f review.yaml`
sledujte, jak návrhy přibývají. Předchozí `review.yaml` se při startu
přesune na `review.yaml.bak`; po Ctrl-C zůstane `.bak` zachován, takže se
můžete obnovit. Každý příkaz spouští interní skenování přes SQLite cache
(`bmf_cache.db`), takže nikdy nemusíte nejdřív spouštět `bmf scan`.
