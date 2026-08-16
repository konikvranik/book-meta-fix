[English](../../how-to/organize.md) | **Čeština**

# Organizace knihovny

```bash
bmf organize                       # dry-run: OK→clean path, broken→needfix/
bmf organize --apply
bmf organize --pattern "{author_sort}/{title} ({id})" --apply
bmf organize --needfix-dir "_problems" --apply
```

Knihy OK/VERIFIED se přesunou podle vzoru; rozbité knihy se přesunou do
`<knihovna>/<needfix-dir>/<původní relativní cesta>` (provenience
zachována).
