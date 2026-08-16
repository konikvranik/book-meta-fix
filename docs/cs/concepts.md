[English](../concepts.md) | **Čeština**

# Koncepty

Mentální model za `book-meta-fix`. Doplňuje
[architecture.md](architecture.md) (jak je to postavené) a
[how-to/index.md](how-to/index.md) (jak to spustit). Referenční výčet
poškození podle kategorií s reálnými příklady žije v
[corruption-catalog.md](corruption-catalog.md).

## Hlavní sázka: nevěřit vloženým metadatům

Calibre při importu **zapíše (případně chybná) DB metadata zpět do souboru
e-knihy**. Název/autor deklarovaný uvnitř `content.opf` EPUBu nebo v Info
slovníku PDF tedy *není nezávislý důkaz* — může jen odrážet poškození,
které se snažíme opravit. Záznam umí potvrdit jen signály, které pocházejí
ze **skutečného textu knihy**:

1. **ISBN naskenované z textu obsahu** (copyrightová strana) — nejsilnější.
2. **Fuzzy shoda názvu/autora s textem první strany** (rapidfuzz,
   necitlivé na diakritiku).
3. **UNCERTAIN**, pokud jsou k dispozici jen vložená metadata (žádný
   čitelný text).

Proto jak verifikátor (`verifier.py`), tak sebekorekční smyčka LLM
vycházejí z `first_page_text`, a proto jsou knihy bez čitelného textu
(čistě obrázkové titulní strany, skenovaná PDF) těžké — není proti čemu
potvrzovat.

## Zdroj pravdy

`metadata.json` (manifest Audiobookshelf) je **zdrojem pravdy**;
`metadata.opf` (Calibre OPF 2.0) je fallback uchovávaný kvůli kompatibilitě
s Kavita/Calibre. Při zápisu se aktualizují **obojí** atomicky
(`writers.py`), takže opětovné skenování zvedne opravu všude. Čtečky
preferují `metadata.json`, fallbackují na `.opf` a cestu ke složce
používají jako slabý signál poslední instance.

## Kategorie verdiktů

Po detekci + verifikaci každá kniha skončí v jednom `Verdict` (`models.py`),
který rozhoduje, co se s ní stane:

| Verdikt | Význam | Kam jde |
|---|---|---|
| `OK` | projde všemi pravidly detektoru | způsobilá pro `organize` (čistá cesta) |
| `VERIFIED` | OK **a** potvrzeno obsahem knihy | způsobilá pro `organize` |
| `AUTO_FIXABLE` | oprava s vysokou spolehlivostí, bezpečná pro automatické aplikování | `review.yaml` s předvyplněným `action: accept`, nebo aplikováno automaticky (smazání C5, zámek C6, obohacení MISSING_ISBN/YEAR/COVER) |
| `NEEDS_REVIEW` | nejisté — musí rozhodnout člověk | `review.yaml`, akci nastavujete vy |
| `UNFIXABLE` | nelze vyřešit bez ručního zásahu | `review.yaml` (hodnoty `proposed` opravte ručně) |

## Kategorie poškození (C1–C11)

Krátké shrnutí — plný katalog s reálnými příklady a zdůvodněním každého
pravidla najdete v [corruption-catalog.md](corruption-catalog.md).

| Kód | Popis | Typický verdikt |
|---|---|---|
| C1 | zaměněný autor/název | NEEDS_REVIEW (`swap`) |
| C2 | název souboru použitý jako název (ztracená diakritika) | NEEDS_REVIEW |
| C3 | série/knihovna/vydavatel použitý jako autor | NEEDS_REVIEW |
| C4 | neopravitelné mojibake | NEEDS_REVIEW (LLM) |
| C5 | doslovný zástupný záznam („author"/„title") | AUTO_FIXABLE (smazání) |
| C6 | duplikát zámku MS-Word (`~$`) | AUTO_FIXABLE (smazání) |
| C7 | slepení autoři („byX...andY") | NEEDS_REVIEW |
| C8 | překladatel označený jako autor | NEEDS_REVIEW |
| C9 | anonym (většinou falešný; skutečný anonym je na whitelistu) | NEEDS_REVIEW |
| C10 | dlouhý seznam autorů (antologie vs. překladatelský tým) | NEEDS_REVIEW |
| C11 | vygenerovaná obálka (zástupná od Calibre), pixelovou analýzou | NEEDS_REVIEW |
| — | MISSING_ISBN / MISSING_YEAR | AUTO_FIXABLE (obohacení) |
| — | MISSING_COVER (chybí sidecar `cover.jpg`) | AUTO_FIXABLE (stažení) |

`detect()` vrací shodu s **nejvyšší prioritou** jako primární diagnózu a
ostatní shody připojuje do `.additional`, takže jedna kniha může nést
několik problémů najednou (např. C2 + C11).

## Kaskáda oprav (nejdřív levné, LLM nakonec)

Pro každou knihu NEEDS_REVIEW `bmf analyze` obnovuje správná metadata v
pořadí podle nákladů. Vyhrává první fáze, která dá užitečný, verifikovaný
návrh:

1. **Offline dolování textu strany** (`text_meta`) — vytěží z textu první
   strany běhy titulních stran velkými písmeny, štítky
   `Název:`/`Autor:`/`Nakladatelství:`, ISBN, rok, vydavatele. Bez sítě.
2. **Online podle ISBN** (z textu > vložené) — OpenLibrary + Google Books.
3. **Online podle názvu + autora** — to je cesta, která dosáhne na
   **databazeknih.cz**, nejsilnější CZ/SK zdroj (žánry + metadata).
4. **Porovnání s vloženým OPF** (nejslabší — calibre mohlo OPF přepsat).
5. **LLM fallback** (`llm.reconcile_loop`) — jen když fáze 1–4 všechny
   selžou.

Návrh je přijat jen pokud projde `confirm_identity` (název + autor
fuzzy-sedí na text první strany, nebo ISBN souhlasí). Knihy bez použitelného
textu první strany LLM celkově přeskočí (nemá smysl utrácet tokeny za nic).

## Sebekorekční smyčka LLM

Když deterministické fáze selžou, LLM fallback spustí sebekorekční smyčku
(`reconcile_loop`) místo jednoho drahého volání:

```
 1. GLM-4.x Flash (free, thinking off)  →  verify_proposal(title, author vs first-page text)
       │ passed  →  accept (source llm:flash)              [the common case — 0 cost]
       │ failed  →  inject feedback into the next attempt
       ▼
 2. GLM Flash with feedback  (max 2 Flash attempts)        [still 0 cost]
       │ passed  →  accept (source llm:loop)
       │ failed / 429  →  fall through
       ▼
 3. GLM-5.2 reasoning_effort=low  (paid, high quality)     [only the hard cases]
       │ passed  →  accept (source llm:high)
       │ failed  →  return last proposal as confidence=low (still human-reviewed)
```

`verify_proposal` kontroluje název + autora proti textu první strany (fuzzy,
necitlivé na diakritiku) plus exaktní kontrolu ISBN. Při selhání vrátí
krátký důvod („the title 'X' not found in first-page text (fuzzy 0.41)"),
který se připojí k promptu dalšího pokusu. Knihy bez čitelného textu
verifikaci přeskočí a výsledek Flashe přijmou tak, jak je.

**Omezování rychlosti** — všechna volání (Flash + finální + retry) procházejí
dvěma sdílenými vrstvami (viz
[architecture.md → Model souběžnosti](architecture.md#model-souběžnosti)):
vyhlazovačem leaky-bucket (konstantní agregované RPM) a **globálním 429
cooldownem** (jedno 429 zaparkuje všechny workery, protože bezplatná vrstva
Z.AI kaskádově zpomalí každý model, jakmile jeden model dostane 429). Při
429 od Flashe smyčka okamžitě propadne na placený finální model, místo aby
pálila další pokusy bezplatné vrstvy — ale finální volání teď nejdřív
*počká* na cooldown, místo aby taky okamžitě dostalo 429.

**Tolerantní JSON** — modely GLM často emitují nevalidní JSON: pythonové
literály (`None`/`True`), koncové čárky, **neescapované dvojité uvozovky
uvnitř řetězcových hodnot** (`"reasoning": "...contains "PROLOG"..."`),
**surové řídicí znaky** (nové řádky) uvnitř řetězců a zkrácení na limitu
tokenů. `_parse_llm_json` všechny zachraňuje: levný vestavěný sanitizér
ošetří běžné případy a pak `json-repair` (extra `[llm]`) dorovná ty těžké,
takže se téměř perfektní odpověď nikdy nevyhodí kvůli syntaktickému
preklepu.

## Workflow review.yaml

Revizní soubor je primární mechanismus **human-in-the-loop**. `bmf analyze`
ho zapisuje **inkrementálně** — jakmile kniha skončí, její záznam se
připojí (`review_writer.py`, jeden YAML dokument `---` na knihu, styl
unixové roury). Můžete spustit `tail -f review.yaml` a sledovat, jak návrhy
přicházejí.

```yaml
---
- id: 4895
  path: "Karel Capek/_apek_Karel-RURe_n_ (4895)"
  diagnosis:
    category: C2
    reason: "title == primary file stem"
    confidence: HIGH
  current:                  # what's in the DB now
    author: Karel Capek
    title: _apek_Karel-RURe_n_
  proposed:                 # our suggested fix — edit to override, null deletes
    title: R.U.R.
    author: Karel Čapek
    isbn: '9788072451648'
    source: embedded+openlibrary
  action: accept            # ← you fill this in
```

**Akce** (na jeden záznam):

| Akce | Účinek |
|---|---|
| `accept` | aplikuje `proposed` (hodnoty upravte, čímž přebijete analyzátor; hodnota `null` dané pole smaže) |
| `delete` | odstraní složku knihy (zámek Wordu `~$` z C6; zálohováno do tar.gz) |
| `keep` | jako `accept`, ale záznam se zachová (neodstraní se); při dalším analyze se přeskočí |

Na začátku se existující `review.yaml` přesune na `review.yaml.bak`
(předchozí rozhodnutí se zachovají); při čistém dokončení se `.bak` smaže;
při přerušení se ponechá, abyste mohli obnovit. `bmf apply` čte jak
multi-doc formu, tak legacy formu jediného seznamu.

## Náhrada obálky

Calibre „Generate cover" produkuje zástupnou obálku (jednolité pozadí +
vykreslený text názvu/autora) přesně o rozměrech 1200×1600. `covers.py` je
detekuje **pixelovou analýzou — bez LLM** — a navrhne náhradu z
databazeknih.cz, když je k dispozici `cover_url`.

Detekční signály (každý přidává spolehlivost; vygenerovaná při ≥ 0.5):

| Signál | Váha |
|---|---|
| Rozměry == 1200×1600 (signature šablony Calibre) | +0.5 |
| Málo unikátních barev (< ~50 při 64barečné kvantizaci) | +0.3 |
| Dominantní barva pokrývá > 60 % pixelů (ploché pozadí) | +0.2 |

- **C11** — detekována vygenerovaná obálka → NEEDS_REVIEW, náhrada se
  navrhne, když je k dispozici `cover_url`.
- **MISSING_COVER** — žádný sidecar `cover.jpg` vůbec → AUTO_FIXABLE.

Náklady: nula tokenů LLM (detekce ~5 ms/kniha; jeden HTTP požadavek na
nahrazenou obálku, rate-limit 1 s/host).

## Zdroje obohacení

Online dotazy jsou **ve výchozím nastavení vypnuté** (`--skip-enrich` je
výchozí pro `analyze`). Výsledky se cachují v `bmf_cache.db`. Pořadí dotazů
při zapnutém obohacení — **první zásah vyhrává**:

1. **databazeknih.cz** (při `--databazeknih`) — nejlepší pro CZ/SK. Vrací
   žánry (široké kategorie + uživatelské štítky), ISBN, vydavatele, jazyk,
   popis, obálku. Scraping (bez API klíče), 2 požadavky/kniha, fuzzy shoda
   názvu hlídá výsledek, takže se nepřipojí žánry špatné knihy.
2. **OpenLibrary podle ISBN** — mezinárodní vydání; slabé pokrytí CZ
   (~10 %).
3. **Google Books podle ISBN** — často rate-limitované bez API klíče.
4. **OpenLibrary podle názvu**.

## Vzory pro organize

`bmf organize` přesouvá knihy OK/VERIFIED na cestu postavenou z
formátovacího řetězce (výchozí `{author}/{title} ({id})`). Rozbité knihy
jdou do `<library>/<needfix-dir>/<original relative path>` (výchozí
`needfix/`), se zachováním struktury, abyste mohli dohledat provenienci.

| Pole | Příklad | Poznámky |
|---|---|---|
| `{author}` | `Karel Čapek` | první autor |
| `{author_sort}` | `Čapek, Karel` | „Příjmení, Jméno" |
| `{title}` / `{title_sort}` | `R.U.R.` | title_sort přesouvá úvodní The/A/An |
| `{id}` | `4895` | calibre_id |
| `{isbn}` | `9788072451648` | prázdné, pokud chybí |
| `{year}` / `{language}` | `1920` / `ces` | |
| `{series}` / `{series_index}` | `Ren Dhark` / `3` | prázdné, pokud není v sérii |
