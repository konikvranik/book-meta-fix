[English](../../how-to/abs-rescan.md) | **Čeština**

# Propuštění změn do Audiobookshelf (abs-rescan)

```bash
bmf abs-rescan                     # dry-run: vypíše změněné knihy + mapování na ABS
bmf abs-rescan --apply             # spustí per-položkový rescan na serveru ABS
bmf abs-rescan --since 2h --apply  # zúží okno
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
3. Požádá ABS o jejich rescan (`POST /api/items/batch/scan`, jedno volání;
   server skenuje na pozadí — za chvíli obnov webové UI). Starší buildy ABS
   bez batch endpointu spadnou na per-položková volání.

Knihy, které nelze namapovat (úplně nové, nebo přesunuté na cestu, kterou
ABS nikdy neviděl), se vypíší s hintem: spusť jednou v ABS obyčejný scan
knihovny, aby se naučil nové cesty, a pak spusť `bmf abs-rescan` znovu.

`--force-all` mapování úplně přeskočí a pošle
`POST /api/libraries/{id}/scan?force=1` — ABS přenačte všechny položky.
Použij, když máš podezření, že mapování něco přehlédlo; je to pro server
těžší.

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
