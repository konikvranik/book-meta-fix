[English](../../how-to/gui.md) | **Čeština**

# Úprava přes GUI (volitelné)

Místo ruční úpravy YAML použijte editor ovládaný klávesnicí:

```bash
bmf gui --review review.yaml
```

Zobrazuje aktuální pole jen pro čtení vedle upravitelných cílů, záměnu
autor↔název jednou klávesou, složku knihy jako klikatelný odkaz „otevřít
ve správci souborů" (dvojklik na řádek seznamu dělá totéž), náhledy obálek
(aktuální / `.bak` / doporučená, plus
obálka vložená v každém souboru formátu — `Ctrl+M` odstraní zaškrtnuté
vložené obálky z e-knih, které zůstávají na místě; jen EPUB) a zobrazení
obsahu
pro jednotlivé formáty s opravou dvojitého kódování (`Ctrl+G`; boxy na
kodeky umožňují ruční experimentování — „přečteno jako" je chybný kodek,
kterým byl text kdysi přečten, „skutečně je" ten skutečný a `⇄` je
prohazuje; nefungující dvojici vysvětlí nápověda, která nabízí opačný směr
jako kliknutí;
byty ztracené dřívějším dekódováním s replace (`�`) opravu neblokují a
zůstávají označené; dvouvrstvé řetězce se opravují automaticky a nápověda
je
jmenuje; vždy vykresleno jako UTF-8 — přepínač se nikdy nezaškrtává
automaticky, vidět opravený text je vaše rozhodnutí: zaškrtněte jej nebo
stiskněte `Ctrl+G`). Táhněte úchyp pod náhledem obsahu pro svislou změnu
velikosti (dvojklik vrací výchozí). Detailní
sloupec se posouvá; každá akce má zkratku `Ctrl+písmeno` (`F1` je
vypíše); `PgUp`/`PgDn` přecházejí mezi knihami a `Tab` prochází jen
upravitelná
pole — autor, název, ISBN, rok, vydavatel, jazyk, série, pořadí v
sérii, autoři, žánry (`Ctrl+A` vybere v poli vše). Seznam zobrazuje vlevo
štítek, na pravém konci řádku s autorem sérii s pořadím (současné hodnoty;
u záznamu s `accept`/`keep` navrženou sérii — tedy tu, kterou `apply`
zapíše) a v každém řádku vpravo přilepenou miniaturu obálky. Vyžaduje Tk
bindings
(`sudo apt install python3-tk` na Debianu/Ubuntu). Úpravy se zapisují
zpět do `review.yaml` — potvrďte je příkazem `bmf apply` jako v
[úprava + aplikování](edit-and-apply.md).

## Vyhledávání v celé knihovně (`+ knihovna`)

Pole `Hledat:` normálně filtruje jen záznamy review. Zaškrtněte vedle něj
**`+ knihovna`** a tentýž dotaz projde i celou knihovnou: odpovídající
knihy, které **nejsou** v review.yaml, se přidají do seznamu (seřazené
podle autora, jejich hlavička říká „není v review.yaml"). Právě to je
workflow pro sérii, o které víte, že je rozbitá — v Audiobookshelf uvidíte
špatná metadata u knih „Mark Stone", vyhledáte sérii, zaškrtnete
`+ knihovna` a zobrazí se všechny knihy Mark Stone, ať už jsou v review,
nebo ne, připravené k úpravě.

Nová kniha se chová úplně stejně jako záznam review (pole, obálky,
zobrazení obsahu, akce). Jediný rozdíl: `Ctrl+S` ji zapíše do review.yaml
až ve chvíli, kdy ji **rozhodnete nebo upravíte** (akce, značka verified,
návrh lišící se od aktuálních hodnot) — nedotčené knihy soubor nezaplaví.
Přírustky v seznamu zůstávají po celou session: úprava knihy, uložení ani
odškrtnutí pole je neodstraní a opakované hledání je idempotentní (žádná
kniha se nevypíše dvakrát). `bmf apply` pak uložené zpracuje jako každý
jiný záznam.

Kniha, jejíž pořadí série je zalepené v názvu (`Mark Stone #73`), přijde
s předvyplněným C14 rozdělením v cílových polích (holá série + pořadí) —
se stejným návrhem, jaký by vyslal `bmf analyze` — takže oprava je jedno
`Ctrl+Enter` na knihu místo přepisování. Samotný nedotčený předvyplněný
návrh knihu do review.yaml nezapisuje; hromadnou opravu dělá analyze se
předvyplněným accept.

Fulltext index knihovny se staví background sweep hned při otevření
editoru (postup ve stavovém řádku; na NFS necelá minuta pro ~5 tisíc
knih, čtení složek paralelně — tentýž sweep plní i našeptávače
autora/série). Každé hledání `+ knihovna` je pak okamžitý filtr v paměti,
bez čekání; dotaz zadaný před dostavěním indexu se odpoví automaticky,
jakmile index dopadne. Index porovnává stejná pole jako vyhledávací box
— autora, název, sérii, cestu ke složce — plus pole, která záznamy review
nikdy nenesou (anotaci, nakladatele, tagy), takže kniha s rozbitými
metadaty odpoví, i když sérii zmíňuje jen název složky nebo anotace
(typické pro série pod pseudonymem typu Mark Stone, kde většina knih
leží pod složkami skutečných autorů). Kniha, která dosud nemá uuid, ji
dostane vytvořenou při indexování (stejná líná identita jako při skenu),
protože celý review workflow je klíčovaný uuid.

## Verified (značka OK)

Vedle radiobuttonů akcí je checkbox **Verified** (přepíná `Ctrl+O`;
verified řádky mají modré ✓ pod glyfem akce a filtr akcí nabízí hodnotu
`verified`). Označuje knihu jako OK napořád: apply zapíše `verified:
true` do `metadata.json` knihy, další běhy analyze knihu úplně přeskočí
a apply ji umístí na cílovou cestu, i když nějaké problémy zůstávají.
Analyze ho předvyplní, když jeho vlastní návrh knihu kompletně doplní.
Řádek „Cílová složka" pod poli zobrazuje (jen pro čtení) návrh přesunu
C13 (`proposed.location`), pokud existuje.
