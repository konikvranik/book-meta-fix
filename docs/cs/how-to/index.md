[English](../../how-to/index.md) | **Čeština**

# Návody

Praktické recepty. *Proč* věci fungují najdete v [concepts.md](../concepts.md);
detaily modulů a toku dat v [architecture.md](../architecture.md); úplnou
referenci příkazů v [README](../../../README.cs.md).

Každý měnící příkaz (`apply`, `epubgen`, `crosscheck`, `strip-covers`,
`normalize`) je **ve výchozím nastavení dry-run** — přidejte `--apply`,
chcete-li skutečně změnit souborový systém (`normalize --apply` naplní
review.yaml; zápis knih stále dělá `bmf apply`).

## Recepty

1. [Instalace a nastavení](setup.md)
2. [První pohled (bez zápisů)](first-look.md)
3. [Vygenerování revizního souboru (hlavní smyčka)](review-loop.md)
4. [Úprava + aplikování](edit-and-apply.md)
5. [Úprava přes GUI](gui.md)
6. [Organizace knihovny (umísťování)](organize.md) — umísťování běží uvnitř `bmf apply`
7. [Generování chybějících EPUB](epubgen.md)
8. [Křížová kontrola složek s více formáty](crosscheck.md)
9. [Odstranění vygenerovaných obálek](strip-covers.md)
10. [Neplatné soubory e-knih](invalid-files.md)
11. [Propuštění změn do Audiobookshelf](abs-rescan.md)
12. [Sjednocení pravopisů autorů, názvů žánrů a sérií](normalize.md)
13. [Sloučení duplicitních složek](merge-duplicates.md)
14. [Zapnutí CZ/SK obohacení](enrichment.md)
15. [Běh s LLM fallbackem](llm.md)
16. [Výběr LLM modelu](llm-models.md)
17. [Spuštění v Kubernetes](kubernetes.md)
18. [Ladění běhu](debugging.md)
19. [Konfigurace](configuration.md)
20. [Spuštění testů](testing.md)
