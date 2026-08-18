[English](../../how-to/edit-and-apply.md) | **Čeština**

# Úprava + aplikování

```bash
$EDITOR review.yaml               # set action: accept|delete|keep per entry
bmf apply review.yaml             # dry-run preview (včetně plánovaných přesunů)
bmf apply --apply review.yaml     # zápis metadat + UMÍSTĚNÍ každé aplikované knihy
```

Akce: `accept` (aplikuje `proposed`), `delete` (odstraní složku knihy,
se zálohou tar.gz), `keep` (aplikuje `proposed`, ale položku v review.yaml
zachová — a nic víc; knihu nezmrazuje).

`proposed` je jediná plocha úprav: hodnoty upravujte přímo a přebijte tím
analyzátor, a nastavte hodnotu na `null`, chcete-li dané pole při
aplikování SMAZAT (chybná hodnota, když ta správná není známa). `proposed`
rozhodnuté položky (včetně vašich úprav) se do dalšího běhu `analyze`
přenese doslovně; nerozhodnuté položky dostanou čerstvý návrh.

## Příznak `verified`

Položka může nést i `verified: true` (checkbox v GUI / `Ctrl+O`;
nezávisle na akci, takže „accept + verified" knihu v jednom průchodu
opraví A zavře). Apply ho zapíše do `metadata.json` knihy (nikdy do OPF
zrcadla — Calibre ho nikdy neuvidí); další běhy `analyze` knihu úplně
přeskočí a apply ji umístí na cílovou cestu, i když nějaké problémy
zůstávají nevyřešitelné. Analyze ho předvyplní, když jeho vlastní návrh
knihu kompletně doplní (projektovaný stav po apply je detektory čistý) —
opravená kniha se do review už nikdy nevrátí. Ukvapené OK odvoláte
příkazem `bmf analyze --recheck-ok`.

## Umísťování (dřívější `bmf organize`)

Po zapsání metadat položky apply rozhodne, kam složka patří — čisté /
`verified` knihy se přesunou na cestu podle vzoru, nevyřešené do
`needfix/`, mrtvé záznamy (bez knižního souboru) do `needfix/empty/`.
Viz [Organizace knihovny](organize.md) pro pole vzoru a řešení kolizí.
`--no-place` přesouvání úplně vypne; `--pattern` / `--needfix-dir`
(nebo `BMF_PATTERN` / `BMF_NEEDFIX_DIR`) přebijí cíle.

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
