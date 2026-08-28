[English](../../how-to/index.md) | **Čeština**

# Návody

Praktické recepty. *Proč* věci fungují najdete v [concepts.md](../concepts.md);
detaily modulů a toku dat v [architecture.md](../architecture.md); úplnou
referenci příkazů v [README](../../../README.cs.md).

Každý měnící příkaz (`apply`, `epubgen`, `crosscheck`, `strip-covers`) je
**ve výchozím nastavení dry-run** — přidejte `--apply`, chcete-li skutečně
změnit souborový systém.

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
10. [Zapnutí CZ/SK obohacení](enrichment.md)
11. [Běh s LLM fallbackem](llm.md)
12. [Výběr LLM modelu](llm-models.md)
13. [Spuštění v Kubernetes](kubernetes.md)
14. [Ladění běhu](debugging.md)
15. [Konfigurace](configuration.md)
16. [Spuštění testů](testing.md)
