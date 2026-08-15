# How-to

Practical recipes. For *why* things work see [concepts.md](../concepts.md); for
the module/data-flow detail see [architecture.md](../architecture.md); for the
full command reference see the [README](../../README.md).

Every mutating command (`apply`, `organize`, `epubgen`, `crosscheck`) is a
**dry-run by default** — add `--apply` to actually change the filesystem.

## Recipes

1. [Setup](setup.md)
2. [First look (no writes)](first-look.md)
3. [Generate a review file (the main loop)](review-loop.md)
4. [Edit + apply](edit-and-apply.md)
5. [Edit via the GUI](gui.md)
6. [Organize the library](organize.md)
7. [Generate missing EPUBs](epubgen.md)
8. [Cross-check multi-format folders](crosscheck.md)
9. [Enabling CZ/SK enrichment](enrichment.md)
10. [Running with the LLM fallback](llm.md)
11. [Choosing an LLM model](llm-models.md)
12. [Running in Kubernetes](kubernetes.md)
13. [Debugging a run](debugging.md)
14. [Configuration](configuration.md)
15. [Running the tests](testing.md)
