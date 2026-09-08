[English](../../how-to/invalid-files.md) | **Čeština**

# Neplatné soubory e-knih (`bmf clean --files`)

```bash
bmf clean --files                        # dry-run: jen výpis nezachovatelných souborů (nic se nezapisuje)
bmf clean --files --apply                # zápis návrhů `action: delete` (C17) do review.yaml
bmf gui                                  # filtr seznamu podle stavu delete, kontrola celé dávky
bmf apply                                # smazání schválených souborů (s překontrolou, tar.gz snapshot)
```

## Co se detekuje

K smazání se soubor navrhne jen tehdy, když jeho **obsah** není
rozpoznatelný jako žádný formát knihy: 0 bajtů, binární šum bez signatury
(ZIP / `%PDF-` / `BOOKMOBI` / `Rar!` / struktura PalmDB / čitelný text),
čitelný ZIP bez knižního obsahu, nebo zkrácený ZIP se ztraceným centrálním
adresářem.

Detekce je záměrně **obsahová, ne podle přípony**: platná kniha uložená
pod špatnou příponou (EPUB pojmenované `.pdf`) je rozpoznána podle obsahu
a nikdy se nenavrhuje — nanejvýš se objeví v informativních poznámkách o
špatných příponách (bez zásahu). Ani formáty, jejichž platné varianty bmf
nedokáže spolehlivě rozpoznat (`.prc`, `.pdb`, `.lit`, …), se neflagují.
A když soubor čistě přečte calibre `ebook-meta`, není neplatný bez ohledu
na výsledky sond.

Mezery jsou záměrné (bezpečnost před úplností): zkrácené PDF začíná
`%PDF-` dál, textový odpad (HTML chybová stránka uložená jako `.epub`)
se pořád jeví jako zachovatelný text — nic potenciálně zachraňitelného
se nenavrhuje.

## Žebříček bezpečnosti

1. **Opt-in**: `--files` je výchoze vypnuté; obyčejný `bmf clean` nikdy
   nesonduje.
2. **Dry-run ve výchozím stavu**: první běh jen vypíše; `--apply` pouze
   zapíše návrhy do review.yaml — v `clean` se nemaže nic.
3. **Schválení člověkem**: nové položky přichází s předvyplněným
   `action: delete` (a `proposed.delete_files`); v GUI seznam vyfiltrujete
   podle stavu *delete* a vidíte je jako jednu dávku, pak
   - **Ctrl+Shift+D** na výběr hromadně vrací rozhodnutí na pending
     (veto pro knihy, které mají zůstat),
   - **Ctrl+Shift+R** odebere vybrané knihy z review.yaml úplně.
4. **Překontrola při aplikaci**: `bmf apply` přesonduje každý soubor těsně
   před smazáním — soubor, který se mezitím stal platným (nebo zmizel),
   se přeskočí a zaznamená. Mažou se jen pojmenované SOUBORY; složka a
   zdravé formáty v ní zůstávají.
5. **Snapshot**: vše skutečně smazané končí v
   `deletion_snapshot_<razítko>.tar.gz` vedle knihovny.

Smazání jediného knižního souboru ve složce kaskáduje: další běh nahlásí
EMPTY_BOOK a přesune složku do `needfix/empty/`.

## Audiobookshelf

Po `bmf apply`, které soubory smazalo, spusťte `bmf abs-rescan`, aby ABS
přeskenoval dotčené složky (per-item sken zohlední změněnou sadu souborů).
