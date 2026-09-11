# Smazání prázdných složek knih (`bmf clean --empty`)

[English](../../how-to/delete-empty-folders.md) | **Čeština**

```bash
bmf clean --empty                        # dry-run: vypíše mrtvé záznamy (nic se nezapisuje)
bmf clean --empty --apply                # zapíše návrhy `action: delete` do review.yaml
bmf gui                                  # filtr seznamu podle stavu delete, kontrola dávky
bmf apply --apply                        # smaže odsouhlasené složky (záloha tar.gz)
```

## Co se detekuje

Za mrtvý záznam (EMPTY_BOOK) se považuje složka, která obsahuje POUZE metadata —
`metadata.json`, `metadata.opf`, `cover.jpg` a jejich zálohy `.bak`/`.tmp`.
Žádný soubor e-knihy, žádný podadresář, žádný jiný soubor: zatoulaný
dokument nebo nerozpoznaný formát složku diskvalifikuje (uvidí ji jiné
pravidlo).

Detekce je lokálně slepá: mrtvé záznamy se hledají kdekoli ve stromu —
včetně `needfix/empty/` (zaparkované dřívějším apply) i kterékoli složky,
kterou starší umísťování zavedlo do `needfix/`.

## Výchozí chování: karanténa, ne mazání

bmf ve výchozím nastavení mrtvý záznam nikdy nemaže. `bmf analyze` ho
oflaguje jako EMPTY_BOOK s předvyplněným `action: accept` a `bmf apply`
složku zaparkuje do `needfix/empty/` (relativní cesta se zachová, metadata
zůstávají nedotčená pro případnou pozdější ruční záchranu). `clean --empty`
je opt-in cesta, jak místo toho navrhnout smazání — úplné znění pravidla
viz [corruption-catalog.md](../corruption-catalog.md#empty_book--mrtvý-záznam-knižní-soubor-chybí).

## Žebřík bezpečnosti

1. **Opt-in**: `--empty` je ve výchozím stavu vypnuto; obyčejný `bmf clean`
   nic nenanavrhne.
2. **Dry-run ve výchozím stavu**: první běh jen reportuje; `--apply` pouze
   zapíše návrhy do review.yaml — v `clean` nedochází k žádnému mazání.
3. **Review-gated**: nové položky přichází předvyplněné `action: delete`;
   v GUI si je vyfiltrujete podle stavu *delete* (nebo kategorie
   EMPTY_BOOK) jako jednu dávku a pak
   - **Ctrl+Shift+D** na výběru hromadně smaže rozhodnutí zpět na pending
     (veto pro složky, které mají přežít),
   - **Ctrl+Shift+R** odstraní vybrané knihy z review.yaml úplně.
4. **Překontrola**: při zápisu návrhů (`--apply`) se fakt prázdnosti
   složky u každé knihy znovu ověří — složka, která od dry-run skenu
   získala reálný soubor, se přeskočí.
5. **Snapshot**: vše, co `bmf apply` smaže, skončí v
   `deletion_snapshot_<stamp>.tar.gz` vedle knihovny.

## Pořadí: pouštějte PO analyze

`bmf analyze` přestaví stále pending položku EMPTY_BOOK od nuly — zpět na
předvyplněný `accept`. Pořádejte si tedy takto:

```bash
bmf clean --empty --apply    # zapsat návrhy na smazání
bmf gui                      # kontrola + veto (mezitím NEPOUŠTĚJTE analyze)
bmf apply --apply            # smazat
```

ROZHODNUTÁ položka pozdější analyze přežije nedotčená — včetně accept,
který analyze pro EMPTY_BOOK předvyplňuje (ty se přeskakují, nikdy
nepřepisují). Změníte-li u rozhodnuté názor, smažte jí rozhodnutí v GUI
(**Ctrl+Shift+D**) a pusťte `clean --empty --apply` znovu.

## Audiobookshelf

Po tom, co `bmf apply` odstraní složky, spusťte `bmf abs-rescan`, aby ABS
zaregistroval chybějící položky.
