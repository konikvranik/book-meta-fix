[English](../../how-to/organize.md) | **Čeština**

# Organizace knihovny (umísťování)

Umísťování je součástí příkazu `bmf apply` — dřívější `bmf organize`
(který při každém běhu znovu klasifikoval celou knihovnu, detektory +
čtení obsahu pro identitní bránu) je jen zastaralý stub.

```bash
bmf analyze                       # označí špatně umístěné knihy (C13)
                                  # s předvyplněným action: accept
bmf gui                           # případná kontrola (checkbox verified, úpravy)
bmf apply                         # dry-run: ukáže plánované přesuny
bmf apply --apply                 # zapíše metadata A umístí každou knihu

bmf apply --apply --pattern "{author_sort}/{title} ({id})"
bmf apply --apply --needfix-dir "_problems"
bmf apply --apply --no-place      # jen metadata, bez přesunů
```

Routing se počítá z FINÁLNÍCH metadat (čistě cestovní matematika — bez
čtení obsahu, ta práce patří analyze). Rozhodnutí u položky přebíjí
zbytkové námitky detektorů:

| stav knihy po apply | cíl |
|---|---|
| rozhodnuto — accept nebo keep (bez ohledu na zbývající námitky detektorů) | cesta podle vzoru, výchozí `{author}/{title} ({id})` |
| mrtvý záznam — žádný knižní soubor (EMPTY_BOOK) | `<knihovna>/<needfix-dir>/empty/<původní relativní cesta>` |

Accepted kniha se proto vždy vrací ZPĚT z `needfix/` na svou cílovou cestu
— skutečná námitka se prostě znovu ozve při příštím analyze a kniha se
vrátí do review; trvale ji zavírá jen příznak `verified`. Mrtvý záznam
zůstává pod `needfix/empty/` — bez knižního souboru není co umísťovat.

Pole vzoru: `{author}`, `{author_sort}`, `{title}`, `{title_sort}`, `{id}`,
`{isbn}`, `{year}`, `{language}`, `{series}`, `{series_index}`.
Výchozí hodnoty jednou nastav přes `BMF_PATTERN` / `BMF_NEEDFIX_DIR`
v `.env`.

## Řešení kolizí (slučování duplicit)

Když se dvě knihy vyřeší na stejnou cílovou cestu (časté u vzoru bez
`{id}` nebo u duplicitních `calibre_id`), apply nepřidává slepě ` (dup N)`.
Rozpozná, zda jde o **stejnou knihu**, a jedná podle toho:

- **Stejná kniha** (ISBN souhlasí, **nebo** název + autor fuzzy-sedí a rok
  neprotiřečí) ⇒ **sloučeno** do jedné složky: všechny formáty zkombinovány,
  metadata sloučena po polích (záznam odsouhlasené knihy je báze; okupant
  doplní mezery, autoři/tagy se sjednotí). Složka poraženého zmizí.
- **Různé knihy** na stejné cestě ⇒ každá se **disambiguje** místo sloučení:
  podle **roku** (`Title (2026)/`), když se roky liší, jinak podle
  **id** (`Title (id123)/`).
- ` (dup N)` zůstává jen jako poslední záchrana.

Ve výchozím stavu dry-run; sloučení a přesuny proběhnou jen s `--apply`.
