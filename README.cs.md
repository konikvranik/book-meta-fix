[English](README.md) | **Čeština**

# book-meta-fix (bmf)

Detekuje a opravuje metadata e-knih v knihovně ve stylu Calibre.

Navrženo pro knihovnu o ~5 000 knihách, ve které Calibre řadu záznamů chybně
klasifikovalo (záměna autora a titulu, název souboru místo titulu, poškozené
kódování, překladatelé uvedení jako autoři). **Zdrojem pravdy je
`metadata.json`** (manifest Audiobookshelf); při zápisu se aktualizují
`metadata.json` i `metadata.opf`, takže Audiobookshelf a Kavita opravy převezmou
při opětovném prohledání.

## Dokumentace

| Dokument | Co pokrývá |
|---|---|
| [docs/cs/architecture.md](docs/cs/architecture.md) | Mapa modulů, tok dat, model souběžnosti, cache, atomicita |
| [docs/cs/concepts.md](docs/cs/concepts.md) | Skupiny verdiktů, filozofie verifikace, kaskáda oprav, smyčka LLM, formát review.yaml |
| [docs/cs/how-to/](docs/cs/how-to/index.md) | Návody krok za krokem (spustit dávku, vyladit rate limit, ladit, …) |
| [docs/cs/corruption-catalog.md](docs/cs/corruption-catalog.md) | Kategorie C1–C17 s reálnými příklady |
| [AGENTS.md](AGENTS.md) | Průvodce pro AI agenty upravující tento kód (konvence, rozložení, zádrhele) |

## Stav

- [x] Skenování (`bmf scan`)
- [x] Detekce (`bmf report`) — pravidla C1–C14
- [x] Verifikace (kaskáda obsah vs metadata)
- [x] Obohacení (scraping databazeknih.cz pro CZ/SK žánry + metadata; legie.info pro sci-fi/fantasy povídky a série; vlastní instance audiobookshelf_czech_metadata agregující ~17 CZ audioknihových e-shopů; OpenLibrary + Google Books jako fallback)
- [x] Analýza + YAML revize (`bmf analyze`, `bmf apply`)
- [x] Normalizace napříč knihovnou (`bmf normalize`) — C15 varianty jmen autorů + C16 varianty žánrů/tagů, lidsky schvalované přes review.yaml
- [x] Umísťování (`bmf apply`) — čisté/verified knihy na vzor cesty, nevyřešené do needfix/ (organize sloučeno)
- [x] Generování EPUB (`bmf epubgen`)
- [x] Konzistence napříč formáty (`bmf crosscheck`) — karanténa formátů, jejichž obsah odporuje metadatům
- [ ] LLM rekonciliace (Z.AI, pro C1/C4/C5) — čeká na `ZAI_API_KEY`
- [x] Testy (445 úspěšných) + dokumentace

## Rychlý start

```bash
cd ~/priv/git/book-meta-fix
make dev-install                  # create .venv, install package + dev deps

# 1. See what's wrong (statistics only, no writes)
bmf report --limit 500

# 2. Generate a review file (this also scans; no separate `bmf scan` needed)
bmf analyze --skip-enrich -o review.yaml --limit 1000

#    Optional: enrich with CZ/SK genres from databazeknih.cz (no API key,
#    2 HTTP requests per book, opt-in scraping). Adds genres + metadata to
#    the proposed block.
bmf analyze --databazeknih -o review.yaml --limit 1000

#    Optional: query a self-hosted audiobookshelf_czech_metadata instance
#    (aggregates ~17 CZ audiobook storefronts; audio-edition metadata).
bmf analyze --abs-czech http://provider:8000 -o review.yaml --limit 1000

# 3. Edit review.yaml — set `action: accept|delete|keep` per entry
$EDITOR review.yaml
#    (or use the keyboard-driven GUI: `bmf gui --review review.yaml`)

# 4. Preview the changes (dry-run, no writes)
bmf apply review.yaml

# 5. Apply for real: zapíše metadata A umístí každou aplikovanou knihu —
#    čisté/verified na vzor cesty, nevyřešené do needfix/
bmf apply --apply review.yaml

# 6. Generate missing EPUBs for OK books
bmf epubgen                        # dry-run
bmf epubgen --apply
```

> **Poznámka:** každý příkaz, který potřebuje metadata knih (`report`,
> `analyze`, `apply`, `epubgen`), spustí interní skenování přes
> `scan_library()`. Není potřeba nejdřív spouštět `bmf scan` — jeho jediným
> účelem je vypsat souhrnné statistiky. Skenování používá SQLite cache
> (`bmf_cache.db`), takže opakované běhy jsou rychlé; předejte `--no-cache`,
> chcete-li vynutit úplné nové parsování.

### Průběžné streamování `review.yaml` (živé výsledky)

`bmf analyze` zapisuje `review.yaml` **přírůstkově** — jakmile je zpracování
knihy hotové, její záznam se připojí na konec souboru, ve stylu unixové roury.
Můžete si spustit `tail -f review.yaml` a sledovat, jak přibývají návrhy,
zatímco běh pokračuje. Při startu se existující `review.yaml` přesune na
`review.yaml.bak` (rozhodnutí uživatele z předchozího běhu se tím zachovají);
po čistém dokončení se `.bak` smaže. Pokud se běh přeruší (Ctrl-C, pád), `.bak`
zůstane zachován, abyste mohli obnovit stav před během.

- **Ctrl-C je bezpečné**: dosud sesbírané výsledky už jsou v souboru a
  `finish()` převezme všechny dřívější záznamy, kterých běh nedosáhl (např.
  s `--limit`). Nic, co uživatel dříve rozhodl, se tiše nezahodí.
- **Formát**: více dokumentové YAML (`---` na záznam). `bmf apply` čte novou
  podobu s více dokumenty i zastaralou podobu s jediným seznamem.

## Příkazy

| Příkaz | Co dělá |
|---|---|
| `bmf scan` | Prochází knihovnu, parsuje metadata, vypisuje souhrnné statistiky |
| `bmf report` | Spustí detektorová pravidla C1–C14, zobrazí rozdělení do kategorií + ukázky |
| `bmf analyze` | Úplná pipeline (sken+detekce+extrakce+verifikace+obohacení) → vygeneruje `review.yaml` |
| `bmf apply <file>` | Aplikuje schválené změny z review.yaml (ve výchozím nastavení dry-run) |
| `bmf apply --apply <file>` | Skutečně zapíše `metadata.json` + `metadata.opf` |
| `bmf gui` | Interaktivní Tkinter editor ovládaný klávesnicí pro `review.yaml` |
| `bmf normalize` | Průchod celou knihovnou: naklastruje varianty jmen autorů (C15 — iniciály vs celá jména, diakritika, tituly, anonymní zápisy, prohozené pořadí) a sjednotí žánry/tagy na české názvy (C16 — duplicity velikost písmen/diakritika/pořadí slov, aliasy EN→CZ). Dry-run: vypíše clustery |
| `bmf normalize --apply` | Naplní review.yaml návrhy C15/C16 (deterministické s předvyplněným `accept`, úsudkové zůstanou pending); `--authors`/`--genres`/`--tags` zúží rozsah. Zápis na disk dělá až `bmf apply`; přejmenování autora přesouvá složky, takže práci zakončete `bmf abs-rescan` |
| `bmf apply --apply <file>` | Zapíše `metadata.json` + `metadata.opf` A umístí knihu: čisté/`verified` → vzor cesty, nevyřešené → `needfix/`, mrtvé záznamy → `needfix/empty/` |
| `bmf organize` | *(zastaralý stub)* — umísťování bylo sloučeno do `bmf apply` |
| `bmf epubgen` | Vygeneruje chybějící soubory `.epub` pro knihy OK (z pdb/mobi/pdf/doc/txt) |
| `bmf epubgen --apply` | Skutečně vygeneruje EPUBy |
| `bmf crosscheck` | Ověří, že všechny formáty ve složce jsou tatáž kniha; vetřelce dá do karantény |
| `bmf crosscheck --apply` | Skutečně přesune nesouhlasící soubory formátů |
| `bmf clean` | Sjednocené čištění knihovny: odstranění nevalidních/generovaných obálek a audit nepotvrzených `verified` příznaků (dry-run) |
| `bmf clean --apply` | Skutečně přejmenuje vadné obálky na `.bak`, odstřihne embedded placeholder obálky a odebere příznak `verified` u knih bez ISBN, jejichž autor/série neexistuje online |
| `bmf clean --apply --clear-all-verified` | Navíc bezpodmínečně vymaže příznak `verified` ze VŠECH knih (vrátí je všechny do review) |
| `bmf strip-covers` | Odstraní vygenerované obálky (dry-run: jen vypíše postižené knihy) |
| `bmf strip-covers --apply` | Skutečně je odstraní: `cover.jpg` → `.bak` a vysoupne embedded obálky EPUB |
| `bmf strip-covers --invalid` | Místo toho odstraní NEVALIDNÍ obálky: soubory s obrázkovou příponou / `cover.*`, které žádný dekodér nepřečte (příčina chyb „Invalid data found“ u ffmpeg v ABS) |
| `bmf strip-covers --generated embedded --apply` | Každý selektor (`--generated`, `--invalid`) přijme volitelný rozsah: `external`, `embedded`, samotný příznak = both |
| `bmf abs-rescan` | Donutí Audiobookshelf znovu načíst metadata nedávno změněných knih (dry-run: vypíše mapování) |
| `bmf abs-rescan --apply` | Skutečně spustí per-položkový rescan ABS (batch API); `--since 2h` zúží okno, `--force-all` přenačte celou knihovnu |
| `bmf abs-rescan --fix-covers --apply` | Navíc vynuluje rozbité řádky obálek v databázi ABS (coverPath na neobrázkový/chybějící soubor — chyby ffmpeg „Invalid data found“) a tyto položky rescanuje |

Společné volby: `--library PATH`, `--limit N`, `--no-cache`, `-o FILE`,
`--skip-enrich`, `--skip-verify`, `--databazeknih`, `--legie`,
`--abs-czech URL`, `--accept-missing/--no-accept-missing` (výchozí zapnuto).

`--accept-missing` (výchozí): kniha s `MISSING_ISBN`/`MISSING_YEAR`/
`MISSING_COVER`, jejíž autor+titul byly potvrzeny proti obsahu knihy, dostane
v `review.yaml` předvyplněné `action: accept` (chybějící pole je kosmetická
vada, ne problém identity). `bmf apply` ji pak hromadně prořezá — pokud nic
nebylo získáno, jde o bezpečný no-op. I odpověď LLM, která neprošla
verifikací (`source: llm:low`), se počítá jako „nic získáno“: její
nedůvěryhodný návrh se zahodí a kniha se akceptuje as-is. Chcete-li tyto
knihy ponechat k ruční revizi, použijte `--no-accept-missing`. Knihy se
souběžnou diagnózou `NEEDS_REVIEW` (např. generovaná obálka) se do revize
stejně pošlou.

## Interaktivní editor (`bmf gui`)

`bmf gui` je na klávesnici postavený Tkinter editor, který `review.yaml`
prochází po jedné knize, místo ruční editace YAML. Edituje tatáž pole
`action` / `edited` / `notes` — vlastní metadata se stejně jako dřív zapisují
poté příkazem `bmf apply`.

```bash
bmf analyze -o review.yaml            # generate first (as above)
bmf gui --review review.yaml          # open the editor
bmf apply review.yaml                 # commit the decisions (dry-run first)
```

**Předpoklad:** Tk bindings. Na Debianu/Ubuntu nainstalujte `python3-tk`
(`sudo apt install python3-tk`). Žádný extra pip balíček není potřeba —
miniatury obálek řeší Pillow (už je závislostí).

**Co u knihy zobrazuje:** pole *current* jen ke čtení vedle editovatelných
polí *target* (jakékoli jednotlivé pole přenesete `Ctrl+L`), záměna
autor↔title jednou klávesou (`Ctrl+W`), zobrazení bloku *proposed* jen ke
čtení, cesta ke složce knihy jako klikatelný odkaz, který ji otevře ve
správci souborů (dvojklik na řádku seznamu udělá totéž), náhledy obálek —
aktuální / `.bak` / doporučená, plus obálka VLOŽENÁ v každém souboru formátu —
každá se svým zaškrtávacím políčkem na obálce a kliknutí na samotnou obálku
jej zaškrtne (`Ctrl+M` poté ze souborů e-knih odstraní zaškrtnuté vložené
obálky, přičemž samotné soubory zůstávají na místě — užitečné na vymetení
neplatných zástupných obálek calibre; jen EPUB), a zobrazení obsahu po
jednotlivých formátech s opravou dvojitého kódování (`Ctrl+G`, pro texty
rozbité nadbytečným překódováním cp1250→utf8 — nebo pár kodeků vyberte ručně:
„přečteno jako“ = chybný kodek, kterým byl text kdysi přečten, „skutečně je“ =
skutečné kódování bajtů, téměř vždy utf-8; `⇄` je prohodí). Pár, který nelze
spustit (např. utf-8→cp1250, jehož nedefinované bajty se zaseknou na běžných
českých znacích), je vysvětlen v nápovědě, která nabízí obrácený směr na jedno
kliknutí; bajty ztracené dřívějším decode s replace (zobrazené jako `�`)
opravu neblokují — zůstávají označené na svém místě. Dvouvrstvá řetězení
(skvělejší reálný vzorek: cp1250 text špatně přečtený jako cp1251, znovu
uložený v utf-8, zase špatně přečtený jako cp1250) se opraví automaticky a
nápověda je pojmenuje. Výsledek se vždy vykreslí jako UTF-8 — ale přepínač
`↻ Překódovat` sám NIKDY není automaticky zaškrtnutý: detekce poškození jen
přednastaví pár kodeků a nápovědu, jestli opravený text uvidíte, rozhodujete
vy (zaškrtněte / stiskněte `Ctrl+G`). Tahem za úchyt pod náhledem obsahu
změníte jeho výšku (dvojklik resetuje). Celý sloupec s detailem roluje;
najetí myší na miniaturu v seznamu zobrazí větší obálku. Samotný seznam je
vykreslen na canvasu — popisek je vždy vlevo, miniatura obálky těsně u pravého
okraje řádku (Treeview z ttk umí obrázky po řádcích zobrazit jen v krajním
levém sloupci).

**Všechno je navázáno na `Ctrl+písmeno`** (samotná písmena se dál píší do
polí): `PgUp`/`PgDn` přesun mezi knihami, `Tab` cyklí jen editovatelná pole
(nikdy tlačítka ani popisky jen ke čtení), `Ctrl+A` vybere v poli vše, fokus
při změně knihy zůstává na témž poli. Akce: `Ctrl+Enter` accept, `Ctrl+D`
delete, `Ctrl+K` keep, `Ctrl+G` překódovat obsah, `Ctrl+S` uložit. Plný
přehled zkratek zobrazí `F1`.

**Hromadná úprava (`Ctrl+E`).** Seznam podporuje vícenásobný výběr
(`Ctrl+klik` řádek přepíná, `Shift+klik` vybere rozsah); `Ctrl+E` otevře
malý dialog, který jedním tahem nastaví autora nebo sérii — případně s `∅`
pole smaže — všem vybraným knihám. Hodnota padne do bloku `proposed` každé
knihy (nerozhodnutá kniha se stane `accept`), pořadí série zůstává knihu
od knihy a výsledky zapíše obvyklé `Ctrl+S`. Přirozené doplnění hledání
`+ knihovna`: natáhnete všechny knihy rozbité série, vyberete je a opravíte
název série jedním tahem.

**Hromadné akce (`Ctrl+Shift+A/O/M`).** Tentýž vícenásobný výběr řídí tři
další hromadné příkazy — shiftová varianta zkratky jedné knihy znamená
„proveď to všem vybraným": `Ctrl+Shift+A` acceptuje všechny vybrané knihy
(explicitní výběr přepisuje i dřívější rozhodnutí), `Ctrl+Shift+O`
přepne značku verified u všech (a zruší ji, když už ji každá vybraná
kniha nese) a `Ctrl+Shift+M` smaže jejich obálky (dialog zrcadlí
zaškrtávací políčka jedné knihy: `cover.jpg`, její `.bak`, vložené obálky
EPUB; navrhovaný `cover_url` se zahodí, aby příští apply znovu nestáhl,
co jste právě odstranili).

**Sloučení duplicit (`Ctrl+J`).** Vyberte dvě a více knih, které jsou totéž
dílo v několika složkách, a stiskněte `Ctrl+J`. Dialog vybere survivor
(standardně fokusaný řádek) a varuje, když je `same_book` nepovažuje za
totéž dílo; mřížka po polích pak zobrazí každé metadatové pole každé
vybrané knihy (rozhodnutý návrh se počítá jako hodnota té knihy) — jedna
radiobuttonová buňka vybírá, kterou stranu sloučená kniha podrží, ∅ znamená
„nechat pole prázdné". Nedotčené defaulty reprodukují automatické
doplňování mezer (hodnota survivor, jinak první nalezená). Vybrané hodnoty
se zapíšou přímo do sloučené knihy a přebasují konfliktní návrh, takže je
zastaralý návrh analyzátoru nemůže vrátit při příštím apply. Soubory
všech ostatních knih se přesunou do složky survivor, jejich složky se
odstraní a jejich review záznamy vypadnou — review.yaml se uloží hned po
sloučení, takže soubor souhlasí s diskem. Jde o explicitní, uživatelem
řízený protějšek automatického slučování, které apply provede, když se dvě
složky srazí na stejném cílovém místě.

**Vyhledávání v celé knihovně (`+ knihovna`).** Pole `Hledat:` filtruje
záznamy review; zaškrtněte vedle něj `+ knihovna` a tentýž dotaz projde
i celou knihovnou — odpovídající knihy, které NEJSOU v review.yaml, se
přidají do seznamu (seřazené podle autora, jejich hlavička říká „není v
review.yaml"). Právě to je workflow pro sérii, o které víte, že je
rozbitá (v Audiobookshelf uvidíte špatná metadata u knih „Mark Stone" →
vyhledáte sérii, zaškrtnete `+ knihovna` a zobrazí se všechny knihy Mark
Stone, ať už jsou v review, nebo ne — připravené k úpravě). Nová kniha se
chová úplně stejně jako záznam review (pole, obálky, zobrazení obsahu,
akce); jediný rozdíl: `Ctrl+S` ji zapíše do `review.yaml` až ve chvíli,
kdy ji rozhodnete nebo upravíte (akce, značka verified, návrh lišící se
od current), takže nedotčené knihy soubor nezaplaví. Přírustky v seznamu
zůstávají po celou session — úprava knihy, uložení ani odškrtnutí pole je
neodstraní — a opakované hledání je idempotentní (žádná kniha se nevypíše
dvakrát). `bmf apply` je pak zpracuje jako každý jiný záznam.

Fulltext index celé knihovny se staví background sweep hned při otevření
editoru (postup ve stavovém řádku; na NFS necelá minuta pro ~5 tisíc knih
— čtení složek je paralelní a tentýž sweep plní i našeptávače
autora/série). Každé hledání `+ knihovna` je pak okamžitý filtr v
paměti: žádný sweep knihovny při hledání, žádné čekání; dotaz zadaný
před dostavěním indexu se odpoví automaticky, jakmile index dopadne.
Index porovnává stejná pole jako vyhledávací box — autora, název, sérii,
cestu ke složce — plus pole, která záznamy review nikdy nenesou (anotaci,
nakladatele, tagy), takže kniha s rozbitými metadaty odpoví, i když sérii
zmíňuje jen název složky nebo anotace (typické pro série pod pseudonymem
typu Mark Stone, kde většina knih leží pod složkami skutečných autorů).
Kniha bez uuid ji dostane vytvořenou při indexování (stejná líná
identita jako při skenu).

**Akce `keep`** aplikuje návrh stejně jako `accept`, ale záznam v `review.yaml`
**zůstává** (neprořezává se) a `bmf analyze` knihu při příštím běhu
**přeskočí** — hodí se pro záznam, se kterým jste hotovi, ale chcete ho mít na
očích. Chcete-li u držené knihy rozhodnout znovu, nastavte její akci zpět na
`pending` (`Ctrl+0`) a spusťte `analyze` znovu.

## Zdroje obohacení

Online dotazy na metadata jsou **ve výchozím stavu vypnuté** (výchozí pro
`analyze` je `--skip-enrich`). Zapnete je přepínači níže; výsledky se ukládají
do cache `bmf_cache.db`, takže opakované běhy znovu nezatěžují síť.

| Přepínač | Zdroj | Silné stránky | Poznámky |
|---|---|---|---|
| `--databazeknih` | databazeknih.cz | **Nejlepší pro CZ/SK**. Vrací žánry (široké kategorie + uživatelské štítky), ISBN, nakladatelství, jazyk, popis, obálku. | Scraping (bez API klíče). 2 požadavky/kniha. Fuzzy shoda titulu výsledek hlídá, takže se nepřiřadí žánry jiné knihy. |
| `--legie` | legie.info | **Nejlepší pro CZ/SK sci-fi/fantasy**. Indexuje povídky („povídky“) a sérii/vesmír, do nichž dílo patří, což vyhledávání knih na databazeknih přehlíží. Silný pro identitu (titul + autor + původní titul). | Scraping (bez API klíče). Bez ISBN/roku/nakladatele (jen identita). Zkouší se po databazeknih. |
| `--abs-czech URL` | vlastní [audiobookshelf_czech_metadata](https://github.com/stecik/audiobookshelf_czech_metadata) | **Metadata AUDIO vydání pro CZ/SK.** Vaše vlastní instance agreguje ~17 CZ audioknihových e-shopů (Alza, Audiolibrix, Audioteka, Kosmas, Radioteka, Rozhlas, …) za ABS custom-provider API `/search` — nakladatelství/rok/obálku/žánry *audio* vydání, ideální pro audioknihovou knihovnu. Rychlé (bmf nescrapuje třetí strany). | Opt-in přes base URL (`BMF_ABS_CZECH_URL`; token `BMF_ABS_CZECH_TOKEN` pro instance s `AUDIOBOOKSHELF_AUTH_TOKEN`). Bez ISBN endpointu — jen titul+autor. Narrator/duration bmf nemodeluje a zahazuje. Odolné vůči junku: keyword vyhledávání provideru (a jeho Rozhlas/podcast řádky s autorem `?`) vrací volné shody, proto se konfliktní autor vždy odmítá a match bez autora vyžaduje téměř přesný titul (≥ 90). Zkouší se po databazeknih-podle-ISBN, před jeho hledáním podle titulu. |
| *(vždy zapnuto při povoleném obohacení)* | OpenLibrary | ISBN + vyhledávání podle titulu, mezinárodní vydání | Slabé pokrytí CZ (~10 %) |
| *(vždy zapnuto při povoleném obohacení)* | Google Books | Dotaz podle ISBN | Často rate-limit bez API klíče |

Pořadí dotazů při zapnutém obohacení: **databazeknih podle ISBN (pokud je
zapnuto) → vlastní CZ provider podle titulu (pokud je nastaveno URL) →
databazeknih podle titulu (pokud je zapnuto) → legie.info (pokud je zapnuto)
→ OpenLibrary podle ISBN → Google Books podle
ISBN → OpenLibrary podle titulu**. První úspěch vyhrává.

**Preference rozlišení obálky**: když provider vrátí stejnou knihu vícekrát
(více e-shopů ji nabízí) nebo ji znají oba CZ zdroje, bmf naměří rozlišení
kandidátských obálek průtokovým přečtením hlavičky obrázku (tělo se nikdy
nestahuje) a ponechá největší — mezizdrojové porovnání běží jen pro knihy s
cover diagnózou (C11 / MISSING_COVER) a vyměňuje se pouze `cover_url`;
metadata z vítězného vyhledávání zůstávají.

```bash
# Enrich with CZ/SK genres only (no international fallbacks needed for a CZ library)
bmf analyze --databazeknih --limit 100 -o review.yaml

# Enable via env var instead of the flag
echo 'BMF_DATABAZEKNIH=1' >> .env

# Point at your self-hosted audiobookshelf_czech_metadata instance (base URL,
# not the /search endpoint; token only when deployed with auth enabled)
echo 'BMF_ABS_CZECH_URL=http://provider:8000' >> .env
```

## Jak opravná pipeline vybírá návrh

U každé knihy NEEDS_REVIEW se `bmf analyze` snaží obnovit správná metadata v
pořadí **od nejlevnějšího**, takže na LLM dojde řada až jako na poslední
východisko:

1. **Offline — dolování textu stránek** (`text_meta`): přečte text první
   strany knihy (už vytěžený pro verifikaci) a pomocí CZ/SK heuristik z něj
   vytěží titul / autory / ISBN / rok / nakladatelství — souvislé úseky
   verzálkami na titulní straně, explicitní štítky `Název:` / `Autor:` /
   `Nakladatelství:`, zahazování zástupného `Neznámý`, odstraňování prosáknutého
   CSS. Bez sítě. Na vzorku 30 knih tak najde titul pro ~37 % a jakékoli pole
   pro ~47 % knih NEEDS_REVIEW.
2. **Online podle ISBN** (`extracted.isbn_from_text` > vložené ISBN):
   OpenLibrary + Google Books.
3. **Online podle titulu + autora** (z textu > vložené > DB): právě touto
   cestou se dosáhne na **databazeknih.cz** — nejsilnější CZ/SK zdroj.
4. **Porovnání s vloženým OPF** (nejslabší; calibre mohlo OPF přepsat).
5. **LLM fallback** — jen když 1–4 všechno minou.

Model LLM fallback a jeho ovládání uvažování jsou konfigurovatelné; viz níže.

### Volba modelu LLM

Jako fallback `bmf analyze --llm` používá GLM API od Z.AI. Pět nastavení modelů
bylo změřeno na vzorku náročných CZ/SK knih (`scripts/llm_experiment.py`):

| Varianta | ok % | vstupní tokeny | výstupní tokeny | uvažování | reálný čas (s) | Cena ($/1M vstup/výstup) |
|---|---|---|---|---|---|---|
| **glm-5.2 reasoning_effort=low (změřeno; výchozím je nyní glm-5.3)** | 100 % | 1529 | 346 | ano | 6.7 | 1.40 / 4.40 |
| glm-4.6 thinking=disabled | 100 % | 1522 | 139 | ne | 3.0 | 0.60 / 2.20 |
| glm-4.5-air thinking=disabled | 100 % | 1522 | 122 | ne | 6.5 | 0.20 / 1.10 |
| glm-4.5-flash | 100 % | 1527 | 96 | ne | 7.6 | zdarma |
| glm-4.7-flash | 100 % | 1522 | 147 | ne | 3.4 | zdarma |

Modely bez uvažování spotřebují 3–4× méně výstupních tokenů, ale u CZ/SK sérií
častěji halucinují (vrátí titul jiné knihy téhož autora, zahodí diakritiku,
vymyslí autory). **GLM-5.3 s `reasoning_effort=low` je výchozí fallback** (a
zároveň výchozí model jednoho volání při vypnuté smyčce) — kvalitu si udrží a
oproti výchozímu nastavení modelu ušetří ~60 % tokenů uvažování. První pokus
smyčky má ve výchozím nastavení bezplatný `glm-4.7-flash`. Na jiný model
přepínejte, jen když víte, co děláte:

```bash
# Cheapest, accepts lower CZ/SK quality (good when the LLM is a rare fallback)
bmf analyze --llm --llm-model glm-4.5-flash

# GLM-4.6 non-reasoning: cheaper than 5.2, better than flash on CZ
bmf analyze --llm --llm-model glm-4.6   # thinking=disabled is the default for 4.x

# More reasoning (slow, costly) for a hard batch
bmf analyze --llm --llm-reasoning-effort max
```

| Volba | CLI | Env | Platí pro |
|---|---|---|---|
| Model smyčky | `--llm-model` | `BMF_LLM_MODEL` | první pokus smyčky (výchozí `glm-4.7-flash`; fallback model, když je smyčka vypnutá) |
| Fallback model | `--llm-fallback-model` | `BMF_LLM_FALLBACK_MODEL` | výchozí `glm-5.3`; také výchozí model jednoho volání při vypnuté smyčce |
| Úroveň uvažování | `--llm-reasoning-effort` | `ZAI_REASONING_EFFORT` | GLM-5.x (výchozí `low`) |
| Přepínač thinking | `--llm-thinking` | `ZAI_THINKING` | GLM-4.x (výchozí `disabled`) |

Zastaralé `ZAI_MODEL` / `ZAI_FLASH_MODEL` / `ZAI_FINAL_MODEL` se pořád ještě
čtou (mapují se na fallback / model smyčky / fallback), ale zapisují do logu
varování o zastaralosti — přejděte na názvy `BMF_LLM_LOOP_*`.

Zopakujte experiment sami, jak se nabídka Z.AI vyvíjí:

```bash
.venv/bin/python scripts/llm_experiment.py --limit 10
```

### Rychlá vrstva přes Google Antigravity (ACP)

Když máte **předplatné Google Antigravity**, jeho modely Gemini mohou skrze
oficiální **Agent Client Protocol** agenta obsluhovat *rychlou* vrstvu smyčky
— místo chronicky přetíženého bezplatného bazénu glm-flash. Celá smyčka může
zůstat na předplatném (rychlá kontrola na gemini-flash s nízkým effortem,
kvalitní záloha na gemini-flash-high), nebo kvalitní fázi předat Z.AI, pokud
existuje i `ZAI_API_KEY` — viz volba zálohy níže.

bmf mluví ACP v1 přímo (JSON-RPC 2.0, jedna zpráva na řádek přes stdio
agentního procesu — bez nové závislosti): spustí agenta, pro každou knihu
zakládá **čerstvou relaci** (relace si uchovává historii, takže opakovaná by
vmíchala důkazy jedné knihy do druhé), odmítá nástroje i přístup k souborům
(důkazy už jsou v promptu) a odpověď parsuje stejnou JSON záchrannou, jakou
používá provider Z.AI.

Nastavení — krátká cesta (agenta si bmf spravuje sám):

1. Optujte se a spusťte; příkaz, který agy potřebuje, si při prvním použití
   oficiálního agenta sám stáhne a dále ho drží aktuálního (při každém běhu
   kontrola registru, při novější verzi aktualizace; offline běh používá
   nainstalovaného):

   ```bash
   export BMF_LLM_PROVIDER=antigravity   # nebo: BMF_ANTIGRAVITY_CMD=auto
   bmf analyze --llm
   ```

   První běh streamuje ~700 MB (2,0 GB rozbalené) do
   `~/.cache/book-meta-fix/acp` — s progress barem uvnitř normálního baru
   analyze; `XDG_CACHE_HOME` a `BMF_ACP_CACHE_DIR` umístění přesouvají.
   Uchovávají se oba členy archivu: binárka `agy_acp_server` a její sourozenec
   `localharness_external` (agent harness resolvoval už při zakládání
   session, takže instalace bez něj shodí každou session — spustitelné bity
   se nastaví samy) a aktualizace vymění oba atomicky (rozbitý download
   nikdy nezničí funkční verzi). Prostý auto režim (bez explicitního opt-in
   do agy) existující cache převezme, ale sám nikdy nestahuje.

2. Jednou se přihlaste interaktivně (např. v Zed nebo v IDE Antigravity) —
   bmf běží bez terminálové relace a OAuth flow neumí. Agent nabízí přihlášení
   Google (osobní), Gemini Enterprise a Gemini API klíč; přihlášení se
   ukládá a bmfův `authenticate` si poradí s vypršením sám. Když chybí,
   bmf to řekne a zobrazí stderr agenta (tam přistávají přihlašovací URL).

Ruční cesta — vlastní binárka, žádná cache, žádné auto-update:

```bash
# aktuální verzovaný odkaz ze strojově čitelného registru (zdroj editorů):
curl -s https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json \
  | python3 -c 'import json,sys; a=[x for x in json.load(sys.stdin)["agents"] if x["id"]=="antigravity-acp"][0]; print(a["distribution"]["binary"]["linux-x86_64"]["archive"])'
# k 2026-09: .../agy-acp-server-agy_acp_server_1.1.1-linux-x86_64.zip —
# uvnitř je agy_acp_server.par = server samotný (~1,9 GB soběstačný ELF;
# chmod +x, když archivátor zahodí bit). localharness_external ponechte
# VEDLE něj (také chmod +x) — agent ho potřebuje už při zakládání session,
# i když bmf nástroje odmítá; alternativně na něj nasměrujte
# ANTIGRAVITY_HARNESS_PATH. Spouštěcí spec registru přidává argument --uid=:
export BMF_ANTIGRAVITY_CMD="/opt/agy/agy_acp_server.par --uid="
bmf analyze --llm
```

Stejně tak funguje jakýkoli ACP agent — např. `BMF_ANTIGRAVITY_CMD="gemini
--acp"` (ACP režim Gemini CLI, samostatné Google přihlášení). Výběr provideru
(`BMF_LLM_PROVIDER` / `--llm-provider`): **auto** (výchozí) použije ACP agenta,
když je nastaven nebo už nakešován; **`zai`** ACP nikdy nedotkne;
**`antigravity`/`acp`/`agy`** větev ACP vynutí (agenta spravuje samostatně —
včetně downloadu); **`off`** fázi LLM vypne.

**Obě fáze smyčky** mají výchozí na předplatném: rychlá kontrola běží na
**gemini-flash (low effort)** a kvalitní záloha na **druhém ACP poolu s
gemini-flash na high effort** (názvy modelů se párují po rodině proti vlastnímu
seznamu modelů agenta, takže přežívají výměny generací — na změřeném agentu
1.1.1 `gemini-flash-low` vyřeší `gemini-3.8-flash-low` a `gemini-flash-high`
`gemini-3.8-flash-high`, oba odpovídají za ~2 s). Pro záměrně NENÍ výchozí:
`gemini-pro` se spáruje s Pro (High) agentem, který nad jednou záložní knihou
dumá 16–37 s a ve změřeném běhu zachránil 0 z 50 LLM knih (flash projde
verifikátorem v 96 % případů) — kvalitní branou je verifikátor, ne tier
modelu; kdo chce pro, nastaví `BMF_ANTIGRAVITY_FALLBACK_MODEL=gemini-pro`.
`BMF_ANTIGRAVITY_FALLBACK=glm` naopak předá kvalitní fázi smyčce
flash+placené Z.AI (potřebuje `ZAI_API_KEY`; Z.AI rate machinery zůstává
nedotčená) — s nastaveným klíčem zůstává celá smyčka na Antigravity, dokud
neřeknete jinak.

| Volba | CLI | Env | Význam |
|---|---|---|---|
| Příkaz agenta | `--antigravity-cmd` | `BMF_ANTIGRAVITY_CMD` (alias `BMF_ACP_COMMAND`) | explicitní cesta (+ `--uid=`) = ruční správa; `auto`/prázdné = samosprávná cache (download + auto-update; viz Nastavení) |
| Umístění cache | — | `BMF_ACP_CACHE_DIR` (nebo `XDG_CACHE_HOME`) | kde bydlí samosprávný agent (výchozí `~/.cache/book-meta-fix/acp`) |
| Model rychlé vrstvy | `--antigravity-model` | `BMF_ANTIGRAVITY_MODEL` (alias `BMF_ACP_MODEL`) | párovaný po rodině; výchozí `gemini-flash-low` (nejnovější flash na low effort — rychlost před dumováním), prázdné = výchozí volba agenta |
| Provider zálohy | `--antigravity-fallback` | `BMF_ANTIGRAVITY_FALLBACK` (alias `BMF_ACP_FALLBACK`) | `agy` (výchozí — druhý ACP pool na fallback modelu) nebo `glm` (smyčka flash+placené Z.AI; potřebuje klíč) |
| Model zálohy | `--antigravity-fallback-model` | `BMF_ANTIGRAVITY_FALLBACK_MODEL` (alias `BMF_ACP_FALLBACK_MODEL`) | jen pro zálohu agy; výchozí `gemini-flash-high`, párováno po rodině (`gemini-pro` = 16–37 s dumování, 0 záchran změřeno) |
| Timeout promptu | — | `BMF_ACP_TIMEOUT` | zaseknutý tah se po tolika sekundách zruší (`session/cancel`, výchozí 300) |
| Souběžní agenti | — | `BMF_ACP_MAX_INFLIGHT` | souběžné agentní procesy FAST poolu (výchozí 4; jeden agent ≈ 320 MB RSS, změřeno) |
| Souběžní fallback | — | `BMF_ANTIGRAVITY_FALLBACK_MAX_INFLIGHT` (alias `BMF_ACP_FALLBACK_MAX_INFLIGHT`) | velikost agy kvalitního poolu, výchozí 1 — serializovaná dráha, na které knihy čekají ve frontě (záloha je výjimečná cesta a prompt turny pod zátěží serveru špičkují 30–100 s, takže paralelní fallback sloty nedávají mnoho) |
| Slušnost kapání | — | `BMF_ACP_MIN_INTERVAL` | minimální sekundy mezi starty promptů (výchozí 0) |

Tři po sobě jdoucí transportní selhání (smazaná binárka, vypršené přihlášení)
odloží rychlou vrstvu pro zbytek běhu — další knihy jdou rovnou do Z.AI zálohy,
místo aby za každou knihu znovu platily cenu spawnu/timeoutu.

Agent je autonomní IDE agent, ne čistý completion endpoint, takže ho bmf drží
na krátkém vodítku: každý prompt začíná direktivou zakazující nástroje (samo
od sebe agent kolem otázky rozjede nástrojovou kaskádu — vypisuje obsah
session adresáře a produmává ~30 modelovými koly na knihu, čímž ze sekund
dělá desítky sekund), session se zakládají v prázdném scratch adresáři místo
v knihovně a agentní procesy se po několika promptech recyklují (protokol
nabízí jen session/list a resume, žádné delete — a každá session drží
~150 MB harness procesu do konce životnosti serveru). Odpověď flash přiletí
za ~2 s.

### Samoopravná smyčka LLM

Když deterministické fáze (offline dolování textu, online dotaz) minou, spustí
LLM fallback místo jednoho drahého volání **samoopravnou smyčku** (ve výchozím
nastavení zapnutou):

```
 1. GLM-4.5-Flash (free, thinking off)  →  verify_proposal(title, author vs first-page text)
       │ passed  →  accept (source llm:flash)              [the common case — 0 USD]
       │ failed  →  inject feedback into the next attempt
       ▼
 2. GLM-4.5-Flash with feedback  (max 2 Flash attempts)   [still 0 USD]
       │ passed  →  accept (source llm:loop)
       │ failed / 429  →  fall through
       ▼
 3. GLM-5.2 reasoning_effort=low  (paid, high quality)    [only the hard cases]
       │ passed  →  accept (source llm:high — also eligible for the auto-verified
       │            pre-fill when the identity is content-confirmed)
       │ failed  →  return last proposal as confidence=low (an acceptable-missing
       │            book with content-confirmed identity is still auto-accepted
       │            as-is; others stay for review by the human)
```

`verify_proposal` kontroluje **titul** i **autora** proti textu první strany
knihy (fuzzy, necitlivé na diakritiku), k tomu přesné porovnání ISBN. Při
neúspěchu vrátí krátké odůvodnění („titul ‚X‘ se v textu první strany knihy
nenachází (fuzzy 0,41)“), které se připojí k promptu dalšího pokusu. Knihy bez
čitelného textu (titulní strany jen s obrázkem, skenovaná PDF) verifikaci
přeskočí a výsledek Flash přijmou beze změny.

**Rate limiting**: všechna volání (Flash + finální + opakování) procházejí
sdíleným leaky bucketem s **adaptivním** intervalem (nastavená hodnota je
jen podlaha: 1302/1113 drip roztáhnou, úspěchy ho stáhnou zpět) **a** tvrdým
stropem souběžnosti, a odpovědi HTTP 429 se rozesílají podle **sub-kódu
Z.AI** (všechny tři přicházejí jako 429, ale znamenají opačné věci):
1. **vyhlazovač leaky-bucket** (počet za čas, výchozí kapacita 1 = čistě
   rovnoměrný kapající tok: přesně každých `--llm-min-interval` sekund
   startuje jedno volání, rovnoměrně rozložená, bez shlukování).
   `--llm-min-interval 2.0` = vyrovnaných 30 rovnoměrně rozložených požadavků
   za minutu — přesně to chce klouzavý oknový limit Z.AI. Burst >1 dovolí v
   téže sekundě vystartovat několik volání a vyrazí dynamický RPM limit;
   zvedejte jen s ověřenou rezervou; a
2. **strop souběžných volání** (`--llm-max-inflight`, výchozí 3) — bucket
   rozestupuje *starty* volání, ne jejich *hloubku*: s 10 workery a
   vícesekundovými reasoning voláními fallback stádo (flash padne → všichni
   se najednou přelijí na placený model) překročil ~5 souběžných požadavků
   na účet, které coding plan připouští (Z.AI přesná čísla nezveřejňuje —
   limity jsou tierové a dynamické podle jejich usage policy; změřeno:
   12hluboký špičkový spike dostal 7× 429/1302, zatímco 6hluboký burst
   prošel, a interaktivní klienti — ZCode/chat — čerpají ze stejného stropu),
   a bouře se projevovaly i jako **falešné 1113** „insufficient balance"
   při zbývající kvótě (potvrzuje i FAQ Z.AI). Workeři teď čekají na
   semaforu, místo aby je Z.AI odmítal. Flash-modely dostávají přísnější
   sub-strop (`min(2, cap)`): bezplatný pool je chronicky nasycený a
   komunita udává jeho souběžnost i na 1. Při coding-plánovém `ZAI_BASE_URL`
   se glm-4.x flash navíc routuje na **PaaS endpoint** (`ZAI_FLASH_BASE_URL`,
   prázdné = auto): stropy souběžnosti obou endpointů jsou nezávislé
   (změřeno) a flash je tam zdarma — vlastní pool a nula kreditů z plánu.
   Globální strop je navíc
   **adaptivní** — externí klient na stejném plánu (ZCode, chat) je pro bmf
   neviditelný, takže když Z.AI signalizuje tlak na strop (1302 / falešné
   1113), bmf uvolní jeden in-flight slot a po ~20 čistých odpovědích si ho
   zpět vydělá; všechna volání sdílejí jednu sadu pooled keep-alive HTTP
   spojení (žádné nové TLS handshake po pauze) s konečným read timeoutem,
   aby zavěšené volání nemohlo obsadit slot donekonečna; a
3. **globální 429/1302 cooldown** (jistič) — `Rate limit reached for
   requests` znamená, že **naše** rychlost vyrazila RPM okno (a bezplatná
   úroveň Z.AI navíc kaskádově přibrzdí i ostatní modely), takže když
   *kterýkoli* worker uvidí 1302, pozastaví se *všichni* workeři
   (`--llm-rate-limit-base` sekund, eskalace 5/10/20/…, respektuje serverové
   `Retry-After`, strop `--llm-rate-limit-max`); a
4. **řízení 429/1305 overload** — `The service may be temporarily overloaded`
   je kapacita **serveru** Z.AI (chronicky časté u bezplatných flash modelů,
   rychlostí se to neovlivní): volání se krátce zopakuje (s rozestupem
   intervalu, s rozpočtem pokusů) *bez* nasazení globálního cooldownu a pak
   se přepne na placený model; fleet-wide série po sobě jdoucích odmítnutí
   pozastaví přetížený model na ~3 minuty, takže každý rate slot jde modelu,
   který opravdu odpovídá. 429/1113 "Insufficient balance" (krátké bursty
   na coding endpointu navzdory zbývající kvótě) dostává stejné zacházení
   s kratší pauzou ~30 s.
   (429/1308 `Usage limit reached` přeskočí model do konce běhu.)

Praktické pokyny v [how-to/llm.md → Ladění omezení rychlosti LLM](docs/cs/how-to/llm.md#ladění-omezení-rychlosti-llm).

**Záchrana neplatného JSON**: modely GLM často emitují lehce rozbitý JSON
(literály Pythonu `None`/`True`, koncové čárky, **neescapované uvozovky
uvnitř hodnot řetězců**, syrové řídicí znaky, zkrácení, **JSON zabalený do
komentářů** — platný objekt následovaný vysvětlující prózou a druhou kopií
v ohraničeném bloku). `_parse_llm_json` všechny tyto případy zachrání —
levný vestavěný sanizátor zvládne běžné případy, `json-repair`
(extra `[llm]`) zachrání ty těžší a z odpovědí zabalených do komentářů se
vyřízne první vybalancovaný objekt — takže se téměř dokonalá odpověď nikdy
nezahodí kvůli syntaktickému přeřeknutí. Když to nabere, uvidíte v logu
`LLM JSON salvaged via json-repair …` nebo `LLM JSON extracted from
commentary-wrapped response …`.

Přepínače:

| Volba | CLI | Env | Výchozí |
|---|---|---|---|
| Smyčka zap/vyp | `--no-llm-loop` | `BMF_LLM_LOOP=0` | zapnuto |
| Model Flash | `--llm-model` | `BMF_LLM_MODEL` | `glm-4.7-flash` |
| Fallback model | `--llm-fallback-model` | `BMF_LLM_FALLBACK_MODEL` | `glm-5.3` |
| Interval volání (s) | `--llm-min-interval` | `BMF_LLM_MIN_INTERVAL` | `2.0` |
| Max souběžných volání | `--llm-max-inflight` | `BMF_LLM_MAX_INFLIGHT` | `3` |
| Kapacita burst | `--llm-burst` | `BMF_LLM_BURST` | `1` (rovnoměrný odkap) |
| Základní 429 cooldown (s) | `--llm-rate-limit-base` | `BMF_LLM_RATE_LIMIT_BASE` | `5` |
| Maximální 429 cooldown (s) | `--llm-rate-limit-max` | `BMF_LLM_RATE_LIMIT_MAX` | `60` |

```bash
# Single fast cheap call, no loop (e.g. for a quick test run)
bmf analyze --llm --no-llm-loop --llm-model glm-4.5-flash

# Stricter rate matching for a free plan (1 call/burst, 4s apart, longer cooldown)
bmf analyze --llm --llm-burst 1 --llm-min-interval 4.0 --llm-rate-limit-base 10
```

## Náhrada obálek

Výchozí calibre funkce „Generate cover“ vytváří zástupný obrázek (jednolitý
podklad + vykreslený text titulu/autora) přesně o velikosti 1200×1600. Pipeline
je detekuje pixelovou analýzou — **bez jakéhokoli LLM** — a když je k
dispozici náhrada, navrhne ji z online zdroje (vlastní CZ provider, když je
nastaven, jinak databazeknih.cz).

**Detekce** (`covers.py` + `rule_generated_cover`): tři signály, každý přidává
spolehlivost; obálka je klasifikována jako generovaná při spolehlivosti ≥ 0,5:

| Signál | Váha | Co znamená |
|--------|--------|---------------|
| Rozměry == 1200×1600 | +0.5 | podpis výchozí šablony Calibre |
| Málo unikátních barev (< ~50 při 64barečné kvantizaci) | +0.3 | jednolitý podklad + text |
| Dominantní barva pokrývá > 60 % pixelů | +0.2 | plochý podklad |

**Kategorie:**
- `C11` — detekována generovaná obálka (NEEDS_REVIEW). Náhrada se navrhne,
  pokud je k dispozici `cover_url`.
- `MISSING_COVER` — chybí úplně přiložený `cover.jpg` (AUTO_FIXABLE).

**Průběh** (stejné jako u návrhů metadat — žádný samostatný příkaz):

```
bmf analyze --databazeknih           # detect C11/MISSING_COVER, fetch cover_url
# → review.yaml entry with action: accept (auto-set when databazeknih matched)
bmf apply review.yaml --apply        # downloads cover_url → cover.jpg (with .bak)
```

**Náklady:** nula tokenů LLM. Detekce je pixelová matematika Pillow
(~5 ms/kniha). Stažení je jeden HTTP požadavek na nahrazenou obálku,
rate limit 1 s na hostitele.


```
<library>/
├── <Author>/
│   └── <Title> (<calibre_id>)/
│       ├── metadata.json     # primary source (Audiobookshelf manifest)
│       ├── metadata.opf      # fallback source (Calibre OPF 2.0)
│       ├── <Title> - <Author>.epub
│       ├── <Title> - <Author>.pdb
│       └── cover.jpg
└── needfix/                  # nevyřešené knihy sem umísťuje `bmf apply`
    └── empty/                # mrtvé záznamy (žádný knižní soubor)
    └── <Author>/...          #   (preserving the original relative subpath)
```

Ze skenování se automaticky vynechávají: `temp_calibre/`, `calibre-*/`,
`needfix/`, `~$*` (zámky Wordu), soubory s tečkou na začátku (dotfiles).

## Konfigurace

Nastavení se vyhodnocuje z těchto zdrojů (v pořadí od nejvyšší přednosti):

1. **Přepínače CLI** — `--library`, `--pattern`, …
2. **Proměnné prostředí procesu** — `BMF_LIBRARY`, `ZAI_API_KEY`, …
3. **Soubor `.env`** — hledá se procházením nahoru od CWD: `./.env`,
   `../.env`, `../../.env`, … (vyhrává první existující soubor; hodnoty se
   načítají jako výchozí, takže skutečné proměnné prostředí stále vyhrávají).
   Zkopírujte `.env.example` do `.env`:
   ```bash
   cp .env.example .env
   $EDITOR .env
   ```
4. **Vestavěné výchozí hodnoty**

| Proměnná | Výchozí | Účel |
|---|---|---|
| `BMF_LIBRARY` | `~/Books` | kořen knihovny |
| `BMF_CACHE` | `bmf_cache.db` | cesta SQLite cache |
| `BMF_REVIEW` | `review.yaml` | výchozí cesta souboru revize |
| `BMF_LANGUAGE` | *(auto)* | Jazyk rozhraní — `cs` nebo `en`. Automaticky detekován z locale uživatele (`cs*` → čeština, cokoli jiného → angličtina). Lze nastavit i pro jednotlivý běh: `bmf --lang cs report` |
| `BMF_ABS_URL` | — | Základní URL serveru Audiobookshelf pro `bmf abs-rescan` (např. `http://abs.lan:13378`; prázdné = příkaz není k dispozici) |
| `BMF_ABS_TOKEN` | — | **Admin** API token Audiobookshelf (Nastavení → Uživatelé → API klíč — scan endpointy odmítají ne-admin tokeny) |
| `BMF_ABS_LIBRARY` | *(auto)* | Název nebo id ABS knihovny, když server hostí více knihoven knih |
| `BMF_ABS_WORKERS` | `4` | Paralelní per-položková scan volání pro `bmf abs-rescan --apply` (`1` = sériově) |
| `BMF_SCAN_WORKERS` | `8` | Paralelní vlákna pro scan knihovny (průchod adresáři + čtení metadat ve složkách; limituje latence NFS). Lze i `bmf analyze/scan --scan-workers`. `1` = původní sériový scan |
| `ZAI_API_KEY` | — | API klíč Z.AI (LLM, volitelné — fáze 7) |
| `ZAI_BASE_URL` | `https://api.z.ai/api/paas/v4/` | základní URL Z.AI |
| `BMF_LLM_MODEL` | `glm-4.7-flash` | model prvního pokusu smyčky LLM (fallback model, když je smyčka vypnutá) |
| `BMF_LLM_FALLBACK_MODEL` | `glm-5.3` | placený fallback model LLM |

### Lokalizace (cs / en)

Zprávy CLI, nápověda voleb, editor `bmf gui` a komentář v hlavičce
`review.yaml` jsou lokalizované přes gettext. **Zdrojové řetězce (msgidy) jsou
anglické**; angličtina je zároveň fallback, když překlad neexistuje. Čeština
žije v `src/book_meta_fix/locales/cs/LC_MESSAGES/bmf.po` (zkompilované `.mo`
je v repozitáři, takže obyčejná instalace nikdy nepotřebuje pybabel).

Rozlišení jazyka (od nejvyšší): přepínač CLI `--lang` → `BMF_LANGUAGE`
(env/`.env`) → automatická detekce locale. Poznámka: nápovědné texty click se
sestavují při importu, takže `--lang` přepíná jen zprávy za běhu — pro plně
český výstup `--help` použijte `BMF_LANGUAGE`.

Po změně překládaných řetězců:

```bash
make i18n-extract   # update .po from source (needs pybabel)
$EDITOR src/book_meta_fix/locales/cs/LC_MESSAGES/bmf.po
make i18n-compile   # .po -> .mo
```

## Kategorie poškození (C1–C17)

Úplný katalog s reálnými příklady najdete v
[`docs/cs/corruption-catalog.md`](docs/cs/corruption-catalog.md). Souhrn:

| Kód | Popis | Typický verdikt |
|---|---|---|
| C1 | záměna autora/titulu | NEEDS_REVIEW |
| C2 | název souboru použitý jako titul (ztracena diakritika) | NEEDS_REVIEW |
| C3 | série/knihovna/nakladatelství použité jako autor | NEEDS_REVIEW |
| C4 | metadata mají neopravitelné mojibake | NEEDS_REVIEW (LLM) |
| C5 | doslovný zástupný záznam („author“/„title“) | AUTO_FIXABLE (smazání) |
| C6 | duplikát zámku souboru MS Wordu (`~$`) | AUTO_FIXABLE (smazání) |
| C7 | slepení autoři („byX...andY“) | NEEDS_REVIEW |
| C8 | překladatel chybně uveden jako autor | NEEDS_REVIEW |
| C9 | anonym (většinou falešný — skutečný anonym je na whitelistu) | NEEDS_REVIEW |
| C10 | dlouhý seznam více autorů (antologie vs. tým překladatelů) | NEEDS_REVIEW |
| C11 | generovaná obálka (zástupná z Calibre) detekovaná pixelovou analýzou | NEEDS_REVIEW |
| C12 | znečištění autora (ztracená kapitalizace, úvodní `_`/`*`) | NEEDS_REVIEW |
| C13 | nesouhlas umístění (složka ≠ vzorová cílová cesta) | AUTO_FIXABLE (přesun) |
| C14 | pořadí série zalepené v názvu série (`Mark Stone #73`) | AUTO_FIXABLE (rozdělení) |
| C15 | varianty jména autora / prohozené pořadí — úroveň knihovny, emituje jen `bmf normalize` | AUTO_FIXABLE / NEEDS_REVIEW |
| C16 | varianty názvů žánrů/tagů (velikost písmen, pořadí slov, EN/CZ, pravopis) — úroveň knihovny, emituje jen `bmf normalize` | AUTO_FIXABLE |
| C17 | neplatný soubor e-knihy (obsah neodpovídá žádnému formátu knihy) — emituje jen `bmf clean --files` | NEEDS_REVIEW (návrh smazání) |
| — | EMPTY_BOOK (jen metadata/zálohy/obálka — knižní soubor chybí) | AUTO_FIXABLE (`needfix/empty/`) |
| — | MISSING_ISBN / MISSING_YEAR | AUTO_FIXABLE (obohacení) |
| — | MISSING_COVER (chybí přiložený `cover.jpg`) | AUTO_FIXABLE (stažení) |

## Formát YAML revize

```yaml
- id: 4895
  path: "Karel Capek/_apek_Karel-RURe_n_ (4895)"
  diagnosis:
    category: C2
    reason: "title == primary file stem"
    confidence: HIGH
  current:                # what's in the DB now
    author: Karel Capek
    title: _apek_Karel-RURe_n_
    year: 2012
    language: ces
  proposed:               # our suggested fix (from content/online)
    title: R.U.R.
    author: Karel Čapek
    isbn: '9788072451648'
    year: 1920
    source: embedded+openlibrary
  action: accept          # ← you fill this in
```

**Akce:**
- `accept` — aplikuje `proposed` (hodnoty upravte, chcete-li přebít analyzátor;
  hodnota `null` dané pole při aplikování smaže)
- `delete` — odstraní složku knihy (C6 ~$ zámek Wordu; se zálohou tar.gz)
- `keep` — jako `accept`, ale záznam zůstává (neprořezává se) v tomto souboru

**Příznak verified** (`verified: true`, nezávisle na akci — checkbox v GUI /
`Ctrl+O`): apply ho uloží do `metadata.json` knihy, další běhy `analyze`
knihu úplně přeskočí a apply ji umístí na cílovou cestu, i když nějaké
problémy zůstávají. Analyze ho předvyplní, když jeho vlastní návrh knihu
kompletně doplní (projektovaný stav po apply je detektory čistý) — opravená
kniha se do review už nikdy nevrátí. Předvyplní se i u akceptovaného záznamu,
jehož FINÁLNÍ identita (titul/autor po aplikování návrhu, případně ISBN) je
potvrzená proti obsahu knihy A zároveň buď online zdrojem (databazeknih/
legie/vlastní CZ provider/OpenLibrary/Google Books), nebo odpovědí `llm:high`
s potvrzenou identitou, jejíž autor/série prošel existenciální kontrolou
(flash tier a nepotvrzené odpovědi se nepočítají): taková kniha se opraví
A zavře jedním apply, i když zůstávají benigní chybějící pole (ISBN/rok/
obálka, které žádný zdroj nemá). Zbylý problém NEEDS_REVIEW předvyplnění
blokuje, aby známý defekt zůstal viditelný — chybějící obálka je benigní a
může zůstat, podezřelá generovaná obálka Calibre (C11) nikoli. Jeden signál
C2 se kredituje: titul shodný s (nikdy nepřejmenovaným) názvem souboru knihy
je po potvrzení identity šum, ne korupce. Odvolání:
`bmf analyze --recheck-ok`.

Staré soubory review.yaml s blokem `edited:` nebo `action: edit|reject|swap`
se při načtení migrují (`edited` se sloučí přes `proposed`, `edit` se stane
`accept`, `reject`/`swap` se vrátí na pending).

## Jak funguje verifikace

Klíčový poznatek je verifikátor: **vložená metadata EPUB/PDF se NEpovažují za
potvrzení**, protože Calibre zapsalo (případně chybná) metadata z DB v době
importu zpět do souboru. Záznam může potvrdit jen **nezávislé signály ze
skutečného textu knihy**:

1. **ISBN naskenované z textu obsahu** (strana s copyrightem) — nejsilnější
   signál
2. **Fuzzy shoda titulu s textem první strany** (rapidfuzz)
3. **UNCERTAIN**, pokud jsou k dispozici jen vložená metadata (žádný čitelný
   text)

## Vzory pro umísťování (apply)

`bmf apply` nezapisuje jen metadata — po aplikaci položky knihu také
**umístí**: čisté / `verified` knihy se přesunou na cestu složenou z
formátovacího řetězce (výchozí `{author}/{title} ({id})`), knihy s
nevyřešenými problémy do `needfix/` a mrtvé záznamy (žádný knižní soubor)
do `needfix/empty/`. Rozhodnutí se odvozuje z FINÁLNÍCH metadat pomocí
čistě metadatových detektorů — bez čtení obsahu, takže apply zůstává
rychlé. Dřívější `bmf organize` (který při každém běhu znovu klasifikoval
celou knihovnu) je zastaralý stub; špatně umístěné knihy označí analyze
pomocí kontroly umístění C13 (s předvyplněným `action: accept`).

Dostupná pole:

| Pole | Příklad | Poznámky |
|---|---|---|
| `{author}` | `Karel Čapek` | první autor |
| `{author_sort}` | `Čapek, Karel` | „Příjmení, Jméno“ |
| `{title}` | `R.U.R.` | |
| `{title_sort}` | `R.U.R.` | článek na začátku přesunut (The/A/An) |
| `{id}` | `4895` | calibre_id |
| `{isbn}` | `9788072451648` | prázdné, pokud chybí |
| `{year}` | `1920` | prázdné, pokud chybí |
| `{language}` | `ces` | |
| `{series}` | `Ren Dhark` | prázdné, pokud není součástí série |
| `{series_index}` | `3` | |

Příklady:
```bash
bmf apply --apply review.yaml --pattern "{author_sort}/{title} ({id})"
bmf apply --apply review.yaml --pattern "{author}/{series}/{title}" --needfix-dir "_problems"
# (nebo BMF_PATTERN / BMF_NEEDFIX_DIR v .env; --no-place přesuny úplně vypne)
```

Rozbité knihy míří do `<library>/<needfix-dir>/<původní relativní cesta>`
(výchozí `needfix/`) se zachováním původní struktury složek, abyste mohli
dohledat, odkud pocházejí.

### Řešení kolizí (sloučení duplicitních knih)

Když dvě knihy OK vyřeší na tutéž cílovou cestu (běžné u vzoru bez `{id}`
nebo u duplicitních `calibre_id`), `organize` už slepě nepřidává ` (dup N)`.
Detekuje, zda jde o **tu samou knihu**, a podle toho jedná:

- **Stejná kniha** (ISBN souhlasí, **nebo** titul + autor se fuzzy shodují a
  rok neprokazuje rozpor) ⇒ **sloučeny** do jedné složky: všechny soubory
  formátů kombinovány, metadata sloučena po polích (základem je záznam s ISBN,
  remíza → nižší id; chybějící pole se doplní z druhého, autoři/štítky se
  sjednotí). Složka poraženého se odstraní; `calibre_id` vítěze určuje
  sloučenou cestu.
- **Různé knihy** na stejné cestě ⇒ každá se **rozliší**, nikoli sloučí:
  podle **roku** (`Title (2026)/`), když se roky liší, jinak podle **id**
  (`Title (id123)/` — prefix `id` ho vizuálně odlišuje od roku). Příponu
  dostanou pro konzistenci všechny kolidující knihy.
- ` (dup N)` přežívá jen jako poslední záchrana (např. dvě různé knihy
  se stejným `calibre_id` pod vzorem `{id}`, kde přípona s id nepomůže).

Ve výchozím nastavení dry-run; slučování běží jen s `--apply`. Souhrn po běhu
ukazuje tabulku sloučení („Merges“ — který poražený se sloučil do kterého
vítěze), takže výsledek lze auditovat.

```bash
bmf apply --apply review.yaml --pattern "{author}/{title}"   # merge dups, disambiguate editions
```

## Konzistence napříč formáty (`bmf crosscheck`)

Složka knihy často obsahuje několik formátů téhož titulu (`.epub`, `.pdf`,
`.pdb`, `.prc`, `.txt`, `.doc`, …). Někdy se do složky zamíchala jiná kniha —
vyměněný soubor, nebo Calibre sloučilo dva záznamy. `bmf crosscheck` ověří, že
**každý formát ve složce je ta kniha, kterou deklarují metadata**, a ty, které
ne, dá do karantény.

```bash
bmf crosscheck                  # dry-run: report rogues, move nothing
bmf crosscheck --apply          # move each rogue into its own needfix folder
```

**Jak rozhoduje.** Pro každou složku se ≥2 formáty se každý soubor formátu
extrahuje a jeho obsah porovná s metadaty složky. Verdikt pro každý formát je
AGREES / DISAGREES / UNCERTAIN, jen pomocí **signálů vytěžených z textu**
(základní pravidlo projektu: vložená metadata EPUB/PDF jsou neinformativní,
protože Calibre zapsalo metadata z DB v době importu zpět do souboru):

1. **ISBN** naskenované z textu stránek vs ISBN z DB — shoda ⇒ AGREES, rozdíl
   ⇒ DISAGREES (nejsilnější signál).
2. **Titul** — titul z DB se fuzzy hledá v textu první strany (partial_ratio,
   tatáž kontrola, kterou používá `verify`). ≥ `--threshold` (0,8) ⇒ AGREES,
   < `--weak-threshold` (0,5) ⇒ DISAGREES, mezi tím ⇒ UNCERTAIN.

Rozhodnutí pro složku:

| Rozhodnutí | Kdy | Akce |
|---|---|---|
| `clean` | žádné DISAGREES | nic se nepřesouvá |
| `quarantine` | ≥1 AGREES **a** ≥1 DISAGREES | soubory DISAGREES jsou vetřelci → přesunuty |
| `ambiguous` | DISAGREES, ale žádné AGREES | **nepřesouvá se** — samotná metadata mohou být špatná (nic je nepodporuje); zkontrolovat ručně |
| `skipped` | méně než 2 formáty | není co křížově kontrolovat |

**Cesta karantény.** Každý vetřelec se přesune do své **vlastní plně
izolované** složky, takže dva vetřelci z téže knihy se nikdy nesloučí (mohou
to být různé chybné knihy):

```
<library>/needfix/crosscheck/<Author> - <Title> (<id>) - <filename>/<filename>
```

Kolize připojí k názvu složky ` (dup N)` (tatáž konvence, kterou používá
`organize` — nikdy neslučovat, nikdy nepřepisovat). Záznam složky knihy v
cache se při skutečném přesunu zneplatní, takže ji příští skenování znovu
parsuje.

**Omezení.** Ukotveno jen v metadatech (formát je „správný“, když souhlasí s
metadaty). Formáty bez extrahovatelného textu (PDF jen s obrázky, komiksy bez
`ComicInfo.xml`/OCR) jsou UNCERTAIN a nikdy se automaticky do karantény
nedávají. Párové neshody, které metadata nedokáží rozhodnout, se nahlásí, ale
nevyřeší automaticky.

**Přiložené soubory `.mbp`.** Anotační soubory Mobipocket (`.mbp`, záložky
pozice čtení ze starého Mobipocket Readeru) jsou rozpoznány jako soubory
formátů. Nejsou to knihy — nesou však záznamy UTF-16 `AUTH`/`TITL` zapsané
čtecím zařízením, kterých se calibre nikdy nedotklo. Ve složkách, kde se
skutečný soubor knihy ztratil (64 v této knihovně, z toho několik bez knihy),
je `.mbp` poslední zbývající důkaz identity a jeho autor/titul se do revize
dostanou jako návrhy. `.mbp` se nikdy nestane primárním formátem, když
existuje skutečná kniha (je poslední v preferenci formátů), a je vyloučeno ze
zdrojů `epubgen`.

## Volitelné externí nástroje

- **`pdftotext` / `pdfinfo`** (poppler-utils) — extrakce obsahu a metadat PDF
- **`ebook-convert`** + **`ebook-meta`** (calibre) — generování EPUB z
  pdb/mobi/doc a fallback extrakce metadat
- **`pandoc`** — fallback generování EPUB z txt/doc/rtf/html

Nástroj funguje i bez nich, ale s omezeným pokrytím formátů.

## Známá omezení

- **Online obohacení pro CZ/SK knihy**: použijte `--databazeknih` pro
  vyhledávání zaměřené na CZ/SK přes scraping databazeknih.cz (žánry +
  metadata, bez API klíče), nebo `--abs-czech URL` / `BMF_ABS_CZECH_URL` pro
  vlastní instanci [audiobookshelf_czech_metadata](https://github.com/stecik/audiobookshelf_czech_metadata)
  (agreguje ~17 CZ audioknihových e-shopů; metadata audio vydání). OpenLibrary
  a Google Books zůstávají jako
  mezinárodní fallbacky, ale mají slabé pokrytí českých ISBN. API
  `obalkyknih.cz` vyžaduje knihovnický klíč (zatím neimplementováno).
- **Mojibake v obsahu EPUB**: když Calibre importovalo knihu s poškozenými
  metadaty, zapsalo to poškození i do `content.opf` EPUBu. Verifikátor to
  nedokáže odhalit porovnáváním textu (poškozený titul je přítomen jak v DB,
  tak v obsahu). Zmírňuje se už výše detektorem C4.
- **Skenovaná PDF**: žádná textová vrstva → žádný verifikační signál.
