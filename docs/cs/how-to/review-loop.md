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

## Výkon na NFS

Scan a pixelová analýza coverů detektoru jsou dvě pevné náklady každého
běhu analyze nad velkou knihovnou na NFS — oba jsou nyní zmírněné:

- Scan (průchod adresáři + čtení metadat ve složkách) běží v malém
  vláknovém poolu — `--scan-workers` / `BMF_SCAN_WORKERS`, výchozí 8,
  `1` = původní sériový scan. Na referenční knihovně (~5 400 knih, NFS v3)
  klesl samotný průchod ze ~38 s na pár sekund.
- Pixelová analýza coveru (pravidlo C11 na generované covery) je
  nejdražší krok detektoru na knihu. Její verdikt se cachuje v
  `bmf_cache.db` (tabulka `covers`) podle mtime+velikosti coveru, takže se
  nezměněný `cover.jpg` nikdy nedekóduje dvakrát — ani v rámci jednoho
  běhu (kde se na tentýž cover ptá až 4×), ani mezi běhy. Smažte
  `bmf_cache.db` (nebo běžte s `--no-cache`), chcete-li po výměně coverů
  vynutit plnou novou analýzu.

