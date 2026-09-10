[English](../../how-to/strip-covers.md) | **Čeština**

# Odstranění vygenerovaných / nevalidních obálek

```bash
bmf strip-covers                                # dry-run: vygenerované obálky, oba rozsahy (původní chování)
bmf strip-covers --apply                        # odstraní je (cover.jpg -> .bak, embedded obálky EPUB vysoupnou)
bmf strip-covers --invalid                      # dry-run: NEVALIDNÍ obálky, oba rozsahy
bmf strip-covers --invalid external --apply     # odstraní jen nevalidní soubory ve složkách knih
bmf strip-covers --generated embedded --invalid both --apply   # oba selektory, zvolené rozsahy
```

> `bmf strip-covers` je zavržený alias; aktuální domov všeho toho je
> `bmf clean --covers` (s rozsahy `--generated` / `--invalid` a prahem
> `--min-size` níže).

Dva selektory, každý jako příznak s volitelným rozsahem. Příznak bez
hodnoty znamená `both`; volitelně přijme hodnotu `external` (volné soubory
ve složce knihy) nebo `embedded` (obálky uvnitř EPUB). Bez kterékoli volby
dělá příkaz to, co dřív — vygenerované obálky, oba rozsahy.

## `--generated` — automaticky vygenerované placeholdery (pixelová analýza C11)

- **soubor `cover.jpg`** — přejmenuje se na `cover.jpg.bak` (vratné;
  existující `.bak` se přepíše), nikdy se natvrdo nemaže.
- **embedded obálka EPUB** — najde se přes OPF wiring a když je
  vygenerovaná, chirurgicky se vysoupne (přepis zipu + OPF; e-kniha sama
  zůstává).
- **ostatní formáty (MOBI/AZW3/PRC, PDF)** — záměrně se netknou: jejich
  obálky žijí v binárních EXTH hlavičkách bez bezpečné cesty ven.

Po zápisovém běhu další `bmf analyze` uvidí `MISSING_COVER` (chybí
`cover.jpg`) a doplní skutečnou obálku — z URL enricheru, nebo extrakcí
skutečné embedded obálky z e-knihy. To je zamýšlený follow-up workflow:
vysoupejte placeholdery, pipeline dočte skutečné obálky.

## `--invalid` — soubory, které vůbec nejsou čitelné obrázky

To jsou obálky ze známých ABS logů `[FfmpegHelpers] Resize Image Error …
Invalid data found when processing input`: ABS volí obálku položky čistě
podle přípony souboru — preferuje `cover.*`, jinak vezme první
`png/jpg/jpeg/webp` ve složce — takže neobrázkový soubor s obrázkovou
příponou (HTML chybová stránka uložená jako `.jpg`, zkrácený download)
nebo s názvem `cover.*` (`cover.html`) se stane obálkou a ffmpeg na něm
zařve.

- **Externí**: každý soubor ve složce s obrázkovou příponou
  (`jpg/jpeg/png/webp` — přesně množina, kterou ABS považuje za obrázky)
  nebo s názvem `cover.*`, který žádný dekodér obrázků nepřečte, se
  přejmenuje na `<název>.bak` (stejná vratná konvence). Validní obrázky —
  včetně vygenerovaných, o které se stará C11 pass — nechá na svém
  selektoru.
- **Embedded**: EPUB, jehož přes OPF navěšená obálka není dekódovatelná
  jako obrázek, se vysoupne stejnou zip + OPF operací jako obálky
  vygenerované.

Jednu ABS záležitost tento příkaz záměrně neřeší: když ffmpeg dostane
`metadata.json` (jako v logu výše), je taková složka mrtvý záznam
(EMPTY_BOOK — e-kniha už není) a `metadata.json` je manifest, který bmf
musí zachovat; zastaralá cesta k obálce žije v databázi ABS samotné a
vyčistí se na straně ABS (plný rescan knihovny), ne mazáním souborů.

## `--min-size N` — skutečné obálky, které jsou prostě malé (bmf clean)

```bash
bmf clean --min-size 300            # dry-run: vypíše obálky pod 300 px na kratší straně
bmf clean --min-size 300 --apply    # přejmenuje je na .bak a knihy znovu otevře do review
```

Kvalitativní selektor pro obálky, které jsou sice skutečné obrázky, ale
drobné — typicky náhledy, které bmf kdysi stáhl z databazeknih. Obálka je
malá, když její KRATŠÍ strana nedosahuje N pixelů (`120x180` selže na 300,
`300x450` ne).

- **Pouze externí**: soubory obálek ve složce (stejná kandidátní množina
  jako `--invalid`: obrázkové přípony a `cover.*`) se přejmenují na
  `<název>.bak`. EMBEDDED obálka uvnitř EPUB se záměrně ponechává — je
  zálohou pro případ, kdy žádný zdroj nic většího nemá, a malá skutečná
  záloha je lepší než žádná.
- **Kniha se vrací do review**: s `--apply` se knize s odstraněnou malou
  obálkou zruší i příznak `verified` — jinak by `bmf analyze` (který
  verified knihy přeskočí) nikdy nevydal `MISSING_COVER` a žádná nová
  obálka by se nestáhla.
- **Nová obálka preferuje větší**: při dalším `bmf analyze` + `bmf apply`
  enrichery porovnají CZ zdroje podle velikosti obrázku
  (`Enricher.upgrade_cover` čte jen hlavičky obrázků, těla nestahuje) a
  vezmou striktně větší obálku. Má-li každý zdroj jen stejný malý obrázek,
  vrátí se původní (`.bak` zůstává).
- Práh se bere z `BMF_COVER_MIN_SIZE` (env / `.env`), když příznak dán
  není; explicitní `--no-covers` má nad env výchozí hodnotou přednost.
