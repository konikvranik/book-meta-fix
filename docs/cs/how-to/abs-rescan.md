[English](../../how-to/abs-rescan.md) | **Čeština**

# Propuštění změn do Audiobookshelf (abs-rescan)

```bash
bmf abs-rescan                     # dry-run: vypíše změněné knihy + mapování na ABS
bmf abs-rescan --apply             # spustí per-položkový rescan na serveru ABS
bmf abs-rescan --since 2h --apply  # zúží okno
bmf abs-rescan --fix-covers --apply # navíc vyčistí rozbité uložené obálky (viz níže)
bmf abs-rescan --force-all --apply # únikovka: force-rescan celé knihovny
```

Proč to existuje: Audiobookshelf si drží **vlastní databázi** a
`metadata.json`/`metadata.opf` znovu čte jen při skenu položky — a obyčejný
„Scan library" *přeskočí každou složku, kterou považuje za nezměněnou*
(mtime brána; když má ABS knihovnu přimontovanou přes NFS, jeho atributová
cache může zamaskovat i čerstvé mtime). Po `bmf apply --apply` jsou tedy
opravy na disku, ale ABS stále ukazuje starý název/autora/sérii, dokud se
položky znovu naskenují. `bmf abs-rescan` tuto mezeru zavírá přes ABS API,
který položku přenačte bezpodmínečně.

## Jednorázové nastavení

```bash
# .env — nastav ZÁKLADNÍ url, ne endpoint
BMF_ABS_URL=http://abs.lan:13378
BMF_ABS_TOKEN=<admin API token>    # webové UI ABS: Nastavení -> Uživatelé -> API klíč
# BMF_ABS_LIBRARY=Knihy            # jen když server hostí více knihoven knih
```

Token musí patřit **adminovi** — scan endpointy vše ostatní odmítnou s 403.

## Co dělá

1. Najde složky knih v knihovně, jejichž soubory se změnily v okně `--since`
   (výchozí 24 h) — bere max mtime souborů, takže počítá i vyměněný
   `cover.jpg`.
2. Namapuje každou složku na položku ABS knihovny: přesná cesta → `relPath`
   (nejčastější případ: téže úložiště přimontované pod jinými prefixy) →
   jednoznačná shoda jména složky (pokryje knihy přesunuté placementem).
3. Požádá ABS o jejich rescan po jednom (`POST /api/items/{id}/scan`).
   Per-položkový endpoint běží synchronně a vrací výsledek — když příkaz
   doběhne, metadata jsou už přenačtená. (Batch endpoint existuje taky —
   odpoví okamžitě 200 a měl by skenovat na pozadí, ale na reálném serveru
   byl naceněn tak, že ~1100 id přijal a nezpracoval žádné, takže ho bmf
   nepoužívá.)

Knihy, které nelze namapovat (úplně nové, nebo přesunuté na cestu, kterou
ABS nikdy neviděl), se vypíší s hintem: spusť jednou v ABS obyčejný scan
knihovny, aby se naučil nové cesty, a pak spusť `bmf abs-rescan` znovu.

`--force-all` mapování úplně přeskočí a pošle
`POST /api/libraries/{id}/scan?force=1` — ABS přenačte všechny položky.
Použij, když máš podezření, že mapování něco přehlédlo; je to pro server
těžší.

## `--fix-covers`: oprava rozbitých obálek v databázi ABS

Druhá půlka čištění obálek. Databáze ABS si u každé položky ukládá cestu
k obálce (`media.coverPath`) a řádek zapsaný **starší verzí ABS** může
ukazovat na soubor, který vůbec není obrázek — typicky `metadata.json`
nebo `cover.html`. Současná verze ABS takové řádky ani nezapisuje, ani
neléčí: scanner obálku znovu volí, jen když je řádek prázdný nebo soubor
zmizel — a `metadata.json` nezmizí a obrázkem není. Takže při každém
obnovení cover cache ffmpeg dostane ten soubor k resizu — logové řádky
`[FfmpegHelpers] Resize Image Error … Invalid data found when processing
input`.

`--fix-covers` přidává pass přes **všechny** položky (zastaralý řádek je
obvykle roky starý, `--since` ho nefiltruje):

- **dry-run** (výchozí): vypíše rozbité řádky — název položky, uloženou
  cestu obálky, důvod (není obrázkový soubor / soubor chybí).
- **`--apply`**: každý rozbitý řádek vynuluje přes
  `DELETE /api/items/{id}/cover` (to zároveň pročistí cover cache ABS) a
  přidá položky do rescanu, takže ABS zvolí skutečnou obálku znovu —
  obrázkový soubor ve složce (preferuje cover.*) nebo embedded obálku
  e-knihy. Řádek je „rozbitý“, když cíl nemá obrázkovou příponu, nebo když
  se mapuje do knihovny a soubor už neexistuje (třeba ho
  `bmf strip-covers` přejmenoval na `.bak`). Nahrané obálky uložené ve
  vlastním adresáři ABS (`/metadata/items/…`) se nechávají být.

Nejdřív vyčisti stranu souborů: `bmf strip-covers --invalid --apply` a
pak až `bmf abs-rescan --fix-covers --apply`. Složka, ve které pořád leží
nečitelný `cover.jpg`, by si ho při rescenu vybrala jen znovu. Celý kruh
pak vypadá takto:

```bash
bmf strip-covers --invalid --apply   # 1. nevalidní soubory obálek na disku -> .bak
bmf analyze --apply review.yaml      # 2. dotáhni skutečné obálky (MISSING_COVER)
bmf abs-rescan --fix-covers --apply  # 3. vynuluj zastaralé řádky DB + rescan
```

## Typický workflow

```bash
bmf analyze            # vygeneruje review.yaml
bmf gui                # rozhodni položky
bmf apply --apply review.yaml
bmf abs-rescan --apply # protlak zapsaných změn do ABS
```

Pozor na opačný směr: úprava provedená **ve webovém UI ABS** přepíše
`metadata.json` z databáze ABS — pak znovu spusť bmf nástroje, nebo se oba
zdroje pravdy rozejdou.
