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
| [docs/cs/corruption-catalog.md](docs/cs/corruption-catalog.md) | Kategorie C1–C13 s reálnými příklady |
| [AGENTS.md](AGENTS.md) | Průvodce pro AI agenty upravující tento kód (konvence, rozložení, zádrhele) |

## Stav

- [x] Skenování (`bmf scan`)
- [x] Detekce (`bmf report`) — pravidla C1–C13
- [x] Verifikace (kaskáda obsah vs metadata)
- [x] Obohacení (scraping databazeknih.cz pro CZ/SK žánry + metadata; legie.info pro sci-fi/fantasy povídky a série; OpenLibrary + Google Books jako fallback)
- [x] Analýza + YAML revize (`bmf analyze`, `bmf apply`)
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
| `bmf report` | Spustí detektorová pravidla C1–C13, zobrazí rozdělení do kategorií + ukázky |
| `bmf analyze` | Úplná pipeline (sken+detekce+extrakce+verifikace+obohacení) → vygeneruje `review.yaml` |
| `bmf apply <file>` | Aplikuje schválené změny z review.yaml (ve výchozím nastavení dry-run) |
| `bmf apply --apply <file>` | Skutečně zapíše `metadata.json` + `metadata.opf` |
| `bmf gui` | Interaktivní Tkinter editor ovládaný klávesnicí pro `review.yaml` |
| `bmf apply --apply <file>` | Zapíše `metadata.json` + `metadata.opf` A umístí knihu: čisté/`verified` → vzor cesty, nevyřešené → `needfix/`, mrtvé záznamy → `needfix/empty/` |
| `bmf organize` | *(zastaralý stub)* — umísťování bylo sloučeno do `bmf apply` |
| `bmf epubgen` | Vygeneruje chybějící soubory `.epub` pro knihy OK (z pdb/mobi/pdf/doc/txt) |
| `bmf epubgen --apply` | Skutečně vygeneruje EPUBy |
| `bmf crosscheck` | Ověří, že všechny formáty ve složce jsou tatáž kniha; vetřelce dá do karantény |
| `bmf crosscheck --apply` | Skutečně přesune nesouhlasící soubory formátů |

Společné volby: `--library PATH`, `--limit N`, `--no-cache`, `-o FILE`,
`--skip-enrich`, `--skip-verify`, `--databazeknih`, `--legie`,
`--accept-missing/--no-accept-missing` (výchozí zapnuto).

`--accept-missing` (výchozí): kniha s `MISSING_ISBN`/`MISSING_YEAR`/
`MISSING_COVER`, jejíž autor+titul byly potvrzeny proti obsahu knihy, dostane
v `review.yaml` předvyplněné `action: accept` (chybějící pole je kosmetická
vada, ne problém identity). `bmf apply` ji pak hromadně prořezá — pokud nic
nebylo získáno, jde o bezpečný no-op. Chcete-li tyto knihy ponechat k ruční
revizi, použijte `--no-accept-missing`. Knihy se souběžnou diagnózou
`NEEDS_REVIEW` (např. generovaná obálka) se do revize stejně pošlou.

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
| *(vždy zapnuto při povoleném obohacení)* | OpenLibrary | ISBN + vyhledávání podle titulu, mezinárodní vydání | Slabé pokrytí CZ (~10 %) |
| *(vždy zapnuto při povoleném obohacení)* | Google Books | Dotaz podle ISBN | Často rate-limit bez API klíče |

Pořadí dotazů při zapnutém obohacení: **databazeknih (pokud je zapnuto) →
legie.info (pokud je zapnuto) → OpenLibrary podle ISBN → Google Books podle
ISBN → OpenLibrary podle titulu**. První úspěch vyhrává.

```bash
# Enrich with CZ/SK genres only (no international fallbacks needed for a CZ library)
bmf analyze --databazeknih --limit 100 -o review.yaml

# Enable via env var instead of the flag
echo 'BMF_DATABAZEKNIH=1' >> .env
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
       │ passed  →  accept (source llm:high)
       │ failed  →  return last proposal as confidence=low (still reviewed by the human)
```

`verify_proposal` kontroluje **titul** i **autora** proti textu první strany
knihy (fuzzy, necitlivé na diakritiku), k tomu přesné porovnání ISBN. Při
neúspěchu vrátí krátké odůvodnění („titul ‚X‘ se v textu první strany knihy
nenachází (fuzzy 0,41)“), které se připojí k promptu dalšího pokusu. Knihy bez
čitelného textu (titulní strany jen s obrázkem, skenovaná PDF) verifikaci
přeskočí a výsledek Flash přijmou beze změny.

**Rate limiting**: všechna volání (Flash + finální + opakování) procházejí
dvěma sdílenými vrstvami:
1. **vyhlazovač leaky-bucket** (počet za čas, výchozí kapacita 1 = čistě
   rovnoměrný kapající tok: přesně každých `--llm-min-interval` sekund
   startuje jedno volání, rovnoměrně rozložená, bez shlukování).
   `--llm-min-interval 2.0` = vyrovnaných 30 rovnoměrně rozložených požadavků
   za minutu — přesně to chce klouzavý oknový limit Z.AI. Burst >1 dovolí v
   téže sekundě vystartovat několik volání a vyrazí dynamický RPM limit;
   zvedejte jen s ověřenou rezervou; a
2. **globální 429 cooldown** (jistič) — bezplatná úroveň Z.AI má kaskádový
   cooldown bug: když jednoho modelu dosáhne rate limit, přibrzdí se i
   ostatní (včetně placeného fallbacku). Takže když *kterýkoli* worker uvidí
   429, pozastaví se *všichni* workeři (`--llm-rate-limit-base` sekund,
   eskalace 5/10/20/…, respektuje serverové `Retry-After`, strop
   `--llm-rate-limit-max`). Jedno 429 zaparkuje celou flotilu, místo aby každý
   worker bušil dál a dostával 429. Při 429 na Flash smyčka okamžitě propadne
   na placený finální model — to finální volání ale teď na konec cooldownu
   *čeká*, místo aby taky hned dostalo 429.

Praktické pokyny v [how-to/llm.md → Ladění omezení rychlosti LLM](docs/cs/how-to/llm.md#ladění-omezení-rychlosti-llm).

**Záchrana neplatného JSON**: modely GLM často emitují lehce rozbitý JSON
(literály Pythonu `None`/`True`, koncové čárky, **neescapované uvozovky
uvnitř hodnot řetězců**, syrové řídicí znaky, zkrácení). `_parse_llm_json`
všechny tyto případy zachrání — levný vestavěný sanizátor zvládne běžné
případy, pak `json-repair` (extra `[llm]`) zachrání ty těžší — takže se
téměř dokonalá odpověď nikdy nezahodí kvůli syntaktickému přeřeknutí. Když to
nabere, uvidíte v logu `LLM JSON salvaged via json-repair …`.

Přepínače:

| Volba | CLI | Env | Výchozí |
|---|---|---|---|
| Smyčka zap/vyp | `--no-llm-loop` | `BMF_LLM_LOOP=0` | zapnuto |
| Model Flash | `--llm-model` | `BMF_LLM_MODEL` | `glm-4.7-flash` |
| Fallback model | `--llm-fallback-model` | `BMF_LLM_FALLBACK_MODEL` | `glm-5.3` |
| Interval volání (s) | `--llm-min-interval` | `BMF_LLM_MIN_INTERVAL` | `2.0` |
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
dispozici náhrada, navrhne ji z databazeknih.cz.

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

## Kategorie poškození (C1–C13)

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
potvrzená proti obsahu knihy A zároveň online zdrojem (databazeknih/legie/
OpenLibrary/Google Books — odpověď LLM se nepočítá): taková kniha se opraví
A zavře jedním apply, i když zůstávají benigní chybějící pole (ISBN/rok/
obálka, které žádný zdroj nemá). Zbylý problém NEEDS_REVIEW předvyplnění
blokuje, aby známý defekt zůstal viditelný — chybějící obálka je benigní a
může zůstat, podezřelá generovaná obálka Calibre (C11) nikoli. Odvolání:
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
  metadata, bez API klíče). OpenLibrary a Google Books zůstávají jako
  mezinárodní fallbacky, ale mají slabé pokrytí českých ISBN. API
  `obalkyknih.cz` vyžaduje knihovnický klíč (zatím neimplementováno).
- **Mojibake v obsahu EPUB**: když Calibre importovalo knihu s poškozenými
  metadaty, zapsalo to poškození i do `content.opf` EPUBu. Verifikátor to
  nedokáže odhalit porovnáváním textu (poškozený titul je přítomen jak v DB,
  tak v obsahu). Zmírňuje se už výše detektorem C4.
- **Skenovaná PDF**: žádná textová vrstva → žádný verifikační signál.
