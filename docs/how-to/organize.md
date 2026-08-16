# Organize the library

**English** | [Čeština](../cs/how-to/organize.md)

```bash
bmf organize                       # dry-run: OK→clean path, broken→needfix/
bmf organize --apply
bmf organize --pattern "{author_sort}/{title} ({id})" --apply
bmf organize --needfix-dir "_problems" --apply
```

OK/VERIFIED books move to the pattern; broken books move to
`<library>/<needfix-dir>/<original relative path>` (provenance preserved).
