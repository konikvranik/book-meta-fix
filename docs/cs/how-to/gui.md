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
pro jednotlivé formáty, které načítá **celý text knihy průběžně** — kusy
přibývají, jak jsou získávány, takže pomalé čtení přes NFS nebo render
konvertorem zobrazí začátek okamžitě místo samotného „načítám…" (extrémně
velké knihy se useknou na 2 000 000 znaků s viditelnou poznámkou o zkrácení)
— s opravou dvojitého kódování (`Ctrl+G`; boxy na
kodeky umožňují ruční experimentování — „přečteno jako" je chybný kodek,
kterým byl text kdysi přečten, „skutečně je" ten skutečný a `⇄` je
prohazuje; nefungující dvojici vysvětlí nápověda, která nabízí opačný směr
jako kliknutí;
byty ztracené dřívějším dekódováním s replace (`�`) opravu neblokují a
zůstávají označené; dvouvrstvé řetězce se opravují automaticky a nápověda
je
jmenuje; vždy vykresleno jako UTF-8 — přepínač se nikdy nezaškrtává
automaticky, vidět opravený text je vaše rozhodnutí: zaškrtněte jej nebo
stiskněte `Ctrl+G`). Náhled se umísťuje responzivně: pokud je detailní
panel široký, je vpravo od formuláře; když se zúží (malé okno nebo
roztáhnutí seznamu knih jeho lištou), přesune se pod formulář. Dělicí
lišta panelu mění velikost náhledu v dané ose a v tomto skládaném režimu
scrolluje celý průběh jediná velká lišta na pravém okraji detailu —
nejdřív formulář, pak celý text knihy (kolečko myši se řetězí stejně;
samostatné lišty obou panelů se vrátí v režimu vedle sebe).
Sekce **Nalezené problémy** nad poli vypisuje všechny diagnózy (řazené
podle závažnosti): řádky jdou vybrat myší a zkopírovat (`Ctrl+C`, nebo
pravé tlačítko nabízí *zkopírovat výběr / vše*) a každý podtržený **kód**
(`C1`…`C20`, `MISSING_*`, …) je klikatelný — otevře malé okno s detailním
popisem daného typu závady (z [katalogu](../corruption-catalog.md));
text popupu jde vybrat a zkopírovat stejně, `Esc` ho zavře. Detailní
sloupec se posouvá; každá akce má zkratku `Ctrl+písmeno` (`F1` je
vypíše); `PgUp`/`PgDn` přecházejí mezi knihami a `Tab` prochází jen
upravitelná
pole — autor, název, ISBN, rok, vydavatel, jazyk, série, pořadí v
sérii, autoři, žánry (`Ctrl+A` vybere v poli vše). Seznam zobrazuje vlevo
štítek, na pravém konci řádku s autorem sérii s pořadím (současné hodnoty;
u záznamu s `accept`/`keep` navrženého autora a sérii — tedy ty, které
`apply` zapíše) a v každém řádku vpravo přilepenou miniaturu obálky.
Řádky se řadí série napřed: knihy jedné série tvoří jediný blok v pořadí
čtení (pořadí se porovnává číselně, takže #2 před #10, a zalepené
`Mark Stone #73` počítá jako 73), bloky následují názvy sérií a knihy
bez série pak podle autora a názvu. Vyžaduje Tk
bindings
(`sudo apt install python3-tk` na Debianu/Ubuntu). Úpravy se zapisují
zpět do `review.yaml` — potvrďte je příkazem `bmf apply` jako v
[úprava + aplikování](edit-and-apply.md).

## Vyhledávání v celé knihovně (`+ knihovna`)

Pole `Hledat:` normálně filtruje jen záznamy review. Zaškrtněte vedle něj
**`+ knihovna`** a tentýž dotaz projde i celou knihovnou: odpovídající
knihy, které **nejsou** v review.yaml, se přidají do seznamu (jejich
hlavička říká „není v review.yaml"; řadí se do stejného pořadí série
napřed jako vše ostatní, takže celá řada se seřadí do jediného bloku).
Právě to je
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

## Hromadná úprava (autor / série)

Seznam podporuje vícenásobný výběr: `Ctrl+klik` řádek přepíná,
`Shift+klik` vybere rozsah (řádek s fokusem má silnější zvýraznění; řádky
skryté filtrem z výběru vypadávají). `Ctrl+E` — nebo tlačítko **Hromadná
úprava** pod seznamem — otevře malý dialog, který jedním tahem nastaví
jedno pole, autora nebo sérii (se stejným našeptávačem jako samotné pole),
všem vybraným knihám; `∅` pole naopak použije jako PRÁZDNÉ (smaže ho —
stejná značka, jakou nastavuje tlačítko `∅` u pole). Hodnota padne do
bloku `proposed` každé knihy; nerozhodnutá kniha se stane `accept`
(návrh bez rozhodnutí by byl `bmf apply` přeskočen), u už rozhodnuté
knihy její akce zůstává. Pořadí série zůstává knihu od knihy — nastavuje
se jen název. Výsledky zapíše do review.yaml obvyklé `Ctrl+S`. Přirozené
doplnění hledání `+ knihovna`: najdete všechny knihy rozbité série,
vyberete je a opravíte název série jedním tahem.

## Hromadné akce (accept / verified / obálky)

Tentýž vícenásobný výběr řídí tři další hromadné příkazy — shiftová
varianta zkratky jedné knihy znamená „proveď to všem vybraným":

- **`Ctrl+Shift+A`** acceptuje všechny vybrané knihy. Na rozdíl od
  hromadné úpravy pole (která rozhoduje jen nerozhodnuté záznamy)
  explicitní výběr **přepisuje** i dřívější rozhodnutí.
- **`Ctrl+Shift+O`** přepne značku verified u všech vybraných knih —
  a zruší ji, když už ji každá vybraná kniha nese.
- **`Ctrl+Shift+M`** smaže jejich obálky. Dialog zrcadlí zaškrtávací
  políčka jedné knihy: `cover.jpg` (standardně zapnuto), jeho `.bak`
  a obálky vložené v souborech EPUB (strip přepisuje e-knihy, takže se
  nejdřív jednou zeptá se seznamem souborů). Navrhovaný `cover_url` se
  z dotčených záznamů zahodí — apply u knihy C11/MISSING_COVER navrženou
  obálku stahuje, takže ponechané URL by příští během smazání vrátilo.
  Změny záznamů zapíše obvyklé `Ctrl+S`.

## Sloučení vybraných knih (`Ctrl+J`)

Když jedno dílo leží v několika složkách (duplikát, rozdělený import),
vyberte je a stiskněte `Ctrl+J`. Dialog vybere **survivor** (standardně
fokusaný řádek — detail ho zobrazuje) a varuje, když `same_book`
nepovažuje výběr za totéž dílo (varování, ne blokace — tady rozhodujete
vy, na rozdíl od automatického slučování při umísťování).

Pod radiobuttony survivor je **mřížka po polích**: jeden řádek na
metadatové pole (název, autoři, ISBN, rok, nakladatelství, jazyk, série,
žánry, popis), jeden sloupec na vybranou knihu, jedna radiobuttonová buňka
na hodnotu. Buňka ukazuje efektivní hodnotu knihy — rozhodnutý návrh
(`accept`/`keep`) se počítá jako hodnota knihy, protože přesně to by
`apply` zapsal; nerozhodnuté a knihovní knihy ukazují uloženou hodnotu.
Vyberte, kterou stranu sloučená kniha podrží; `∅` nechá pole prázdné.
Defaulty následují survivor a poli, které survivor nemá, se přiřadí první
nalezená hodnota — potvrzení dialogu bez úprav tedy reprodukuje
automatické doplňování mezer a nic se neztratí.

Po potvrzení se soubory všech ostatních knih přesunou do složky survivor
(kolize se přejmenují s calibre id poraženého), vybrané hodnoty se zapíšou
do metadat sloučené knihy a složky poražených se odstraní i s jejich
review záznamy. Vybraná hodnota navíc REBASUJE konfliktní klíč návrhu u
survivor (zastaralý návrh analyzátoru nesmí vrátit explicitní volbu při
příštím apply); pole bez návrhu zůstávají čistá. review.yaml se uloží
**hned po** sloučení — složky poražených už neexistují a soubor musí
souhlasit s diskem (zastaralý záznam by příští apply selhal na „folder not
found"). Survivor si podrží své review rozhodnutí; `bmf apply` ho pak
dokončí jako obvykle.

## Verified (značka OK)

Vedle radiobuttonů akcí je checkbox **Verified** (přepíná `Ctrl+O`;
verified řádky mají modré ✓ pod glyfem akce a filtr akcí nabízí hodnotu
`verified`). Označuje knihu jako OK napořád: apply zapíše `verified:
true` do `metadata.json` knihy, další běhy analyze knihu úplně přeskočí
a apply ji umístí na cílovou cestu, i když nějaké problémy zůstávají.
Analyze ho předvyplní, když jeho vlastní návrh knihu kompletně doplní.
Řádek „Cílová složka" pod poli zobrazuje (jen pro čtení) návrh přesunu
C13 (`proposed.location`), pokud existuje.
