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
11. [Smazání prázdných složek knih](delete-empty-folders.md)
12. [Propuštění změn do Audiobookshelf](abs-rescan.md)
13. [Sjednocení pravopisů autorů, názvů žánrů a sérií](normalize.md)
14. [Sloučení duplicitních složek](merge-duplicates.md)
15. [Zapnutí CZ/SK obohacení](enrichment.md)
16. [Běh s LLM fallbackem](llm.md)
17. [Výběr LLM modelu](llm-models.md)
18. [Spuštění v Kubernetes](kubernetes.md)
19. [Ladění běhu](debugging.md)
20. [Konfigurace](configuration.md)
21. [Spuštění testů](testing.md)
