[English](../../how-to/merge-duplicates.md) | **Čeština**

# Sloučení duplicitních složek (`bmf merge`)

```bash
bmf merge                        # dry-run: vypíše duplicitní clustery (nic nezapisuje)
bmf merge --apply                # zapíše návrhy `action: merge` (C19) do review.yaml
bmf gui                          # zkontrolujte dávku (filtr podle stavu merge)
bmf apply                        # včlení schválené duplicity do jejich přeživších
bmf abs-rescan                   # složky přeživších se na disku změnily — dotlačte do ABS
```

## Co se detekuje

Tutéž kniha ve **dvou složkách** — typicky dvojitý import: každý calibre
import si vytvoří vlastní id, výchozí vzor `{author}/{title} ({id})` dá
každému vlastní cestu a nikdy nekolidují (takže apply sloučení při kolizi
cíle se k nim nemůže dostat). Cluster je:

- shodný **zfoldovaný první autor + zfoldovaný název** (diakritika, velikost
  písmen a interpunkce zfoldována — *Babička* odpovídá *babicka*), **nebo**
- stejné **platné ISBN** na obou stranách (což chytne i duplicitu, které se
  v jedné složce pokazil název).

Složky, u kterých **oba roky existují a liší se**, jsou různá vydání —
nenavrhnou se, pokud už nesou shodné ISBN (shodné ISBN určuje též vydání i
tehdy, když jeden záznam nese špatný rok). Mrtvé záznamy (bez knižních
souborů) se přeskakují; ty vlastní EMPTY_BOOK. Fuzzy vrstva záměrně chybí:
překlepaný název C19 nevidí (opravte ho přes `bmf normalize` a dvojice se
objeví při dalším běhu).

**Přeživší** každého clusteru se volí deterministicky: vyhrává záznam s
platným ISBN, jinak nejnižší calibre_id. Každý další člen dostane review
záznam, jehož `proposed.merge_into` jmenuje složku přeživšího.

## Žebříček bezpečnosti

1. **Dry-run ve výchozím nastavení**: první běh jen vypíše clustery;
   `--apply` pouze naplní review.yaml — `merge` se složkami nehýbe.
2. **Konzervativní předvyplnění**: jen **ISBN-potvrzené** duplicity (obě
   strany nesou stejné platné ISBN) přijdou předvyplněné `action: merge`;
   přesná shoda autora+názvu bez ISBN důkazu zůstává pending na vaše
   rozhodnutí.
3. **Rozhodnuté záznamy se nikdy nepřepisují**: opakovaný
   `bmf merge --apply` přeskakuje knihy, o kterých jste už rozhodli.
4. **Re-check při apply**: `bmf apply` čte obě složky čerstvě a znovu
   spustí `same_book` — když se metadata od návrhu změnila, sloučení se
   přeskočí a záznam zůstane v review (spusťte `bmf merge` znovu).
   Přeživší se svým vlastním decidovaným `delete` na celou složku se
   nedotkne.
5. **Žádný soubor se nikdy nepřepisuje**: kolize jmen u přeživšího se
   přejmenují s id poraženého (`Book (id123).epub`), identické bajty se
   přeskakují, obálka přeživšího vyhrává. Metadata se sloučí pole po poli
   **přeživší-první** (duplicita jen doplňuje mezery).
6. **Snapshot**: `metadata.json`/`metadata.opf` + obálka poraženého (jediné,
   co sloučení ničí) jedou do společného `deletion_snapshot_<stamp>.tar.gz`.

`analyze --merge` napojuje tentýž sweep na konec běhu, nad knihami, které
analyze už prošel — bez druhého (na NFS pomalého) průchodu knihovnou.

## Audiobookshelf

Sloučení změní složky přeživších na disku (přibyly soubory) a odstraní
složky poražených — spusťte po sobě `bmf abs-rescan`, aby ABS dotčené
přeživší znovu nascannoval.
