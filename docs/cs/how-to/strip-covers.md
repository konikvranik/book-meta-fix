[English](../../how-to/strip-covers.md) | **Čeština**

# Odstranění vygenerovaných / nevalidních obálek

```bash
bmf strip-covers                                # dry-run: vygenerované obálky, oba rozsahy (původní chování)
bmf strip-covers --apply                        # odstraní je (cover.jpg -> .bak, embedded obálky EPUB vysoupnou)
bmf strip-covers --invalid                      # dry-run: NEVALIDNÍ obálky, oba rozsahy
bmf strip-covers --invalid external --apply     # odstraní jen nevalidní soubory ve složkách knih
bmf strip-covers --generated embedded --invalid both --apply   # oba selektory, zvolené rozsahy
```

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
