[English](../../how-to/edit-and-apply.md) | **Čeština**

# Úprava + aplikování

```bash
$EDITOR review.yaml               # set action: accept|delete|keep per entry
bmf apply review.yaml             # dry-run preview
bmf apply --apply review.yaml     # actually write metadata.json + metadata.opf
```

Akce: `accept` (aplikuje `proposed`), `delete` (odstraní složku knihy,
se zálohou tar.gz), `keep` (aplikuje `proposed`, ale položku zachová —
`analyze` ji příště přeskočí; nastavte zpět na `pending`, chcete-li
rozhodnout znovu).

`proposed` je jediná plocha úprav: hodnoty upravujte přímo a přebijte tím
analyzátor, a nastavte hodnotu na `null`, chcete-li dané pole při
aplikování SMAZAT (chybná hodnota, když ta správná není známa). `proposed`
rozhodnuté položky (včetně vašich úprav) se do dalšího běhu `analyze`
přenese doslovně; nerozhodnuté položky dostanou čerstvý návrh.

Každé získané pole se dostane až ke knize: název/autor(y), ISBN, rok,
vydavatel, jazyk, série + pořadí v sérii (uloženo jako ABS seznam
`[{"name", "index"}]`, zrcadleno do `metadata.opf` jako
`calibre:series`/`calibre:series_index`), žánry (a tagy — obojí jako OPF
`<dc:subject>`) a popis/anotace z databazeknih / Google Books /
OpenLibrary. Návrh, který nese jen polovinu série, si ponechá aktuální
druhou polovinu; `null`/vyprázdněný název série ji vymaže. U C1 (zaměněný
autor/název) navrhuje záměnu samotný analyzátor — přijměte ji nebo hodnoty
upravte.

Staré soubory review.yaml (blok `edited:`, `action: edit|reject|swap`) se
při načtení migrují: `edited` se sloučí přes `proposed`, `edit` se změní
na `accept` a `reject`/`swap` se vrátí na pending.

Místo ruční úpravy YAML můžete použít [GUI editor](gui.md).
