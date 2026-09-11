# How-to

**English** | [Čeština](../cs/how-to/index.md)

Practical recipes. For *why* things work see [concepts.md](../concepts.md); for
the module/data-flow detail see [architecture.md](../architecture.md); for the
full command reference see the [README](../../README.md).

Every mutating command (`apply`, `epubgen`, `crosscheck`, `strip-covers`,
`normalize`) is a **dry-run by default** — add `--apply` to actually change
the filesystem (for `normalize`, `--apply` fills review.yaml; the book
writes still happen via `bmf apply`).

## Recipes

1. [Setup](setup.md)
2. [First look (no writes)](first-look.md)
3. [Generate a review file (the main loop)](review-loop.md)
4. [Edit + apply](edit-and-apply.md)
5. [Edit via the GUI](gui.md)
6. [Organize the library (placement)](organize.md) — placement runs inside `bmf apply`
7. [Generate missing EPUBs](epubgen.md)
8. [Cross-check multi-format folders](crosscheck.md)
9. [Strip generated covers](strip-covers.md)
10. [Invalid ebook files](invalid-files.md)
11. [Delete empty book folders](delete-empty-folders.md)
12. [Push changes into Audiobookshelf](abs-rescan.md)
13. [Normalize author spellings, genre names and series names](normalize.md)
14. [Merge duplicate folders](merge-duplicates.md)
15. [Enabling CZ/SK enrichment](enrichment.md)
16. [Running with the LLM fallback](llm.md)
17. [Choosing an LLM model](llm-models.md)
18. [Running in Kubernetes](kubernetes.md)
19. [Debugging a run](debugging.md)
20. [Configuration](configuration.md)
21. [Running the tests](testing.md)
