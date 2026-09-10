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
   necitlivé na diakritiku). Shoda autora pozná též téhož autora
   v jiném vytištěném formátu — iniciály křestních jmen („A. Buškov"
   za „Alexandr Buškov"), vypuštěná prostřední jména, skloňovaná či
   přepsaná příjmení („Strugačtí" za „Strugackij") — první křestní
   jméno ale zůstává povinné, takže příjmení zmíněné v textu nebo
   stejnopříjmenčí homonym nikdy nepotvrdí záznam.
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
| `OK` | projde všemi pravidly detektoru | způsobilá pro umísťování v `apply` (čistá cesta) |
| `VERIFIED` | OK **a** potvrzeno obsahem knihy | způsobilá pro umísťování v `apply` |
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
(`reconcile_loop`) místo jednoho drahého volání. *Rychlá* vrstva smyčky není
vázána na Z.AI: s předplatným Google Antigravity obsluhuje první pokusy
**Agent Client Protocol** agent (`BMF_ANTIGRAVITY_CMD`, např. Google
`agy_acp_server.par` — viz README „Rychlá vrstva přes Google Antigravity
(ACP)“) a Z.AI si nechá jen roli placené zálohy.

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
       │ passed  →  accept (source llm:high — eligible for the auto-verified
       │            pre-fill when the identity is content-confirmed)
       │ failed  →  return last proposal as confidence=low (an acceptable-missing
       │            book with content-confirmed identity is still auto-accepted
       │            as-is; others stay human-reviewed)
```

`verify_proposal` kontroluje název + autora proti textu první strany (fuzzy,
necitlivé na diakritiku) plus exaktní kontrolu ISBN. Při selhání vrátí
krátký důvod („the title 'X' not found in first-page text (fuzzy 0.41)"),
který se připojí k promptu dalšího pokusu. Knihy bez čitelného textu
verifikaci přeskočí a výsledek Flashe přijmou tak, jak je.

**Omezování rychlosti** — všechna volání (Flash + finální + retry) procházejí
sdíleným leaky bucketem a odpovědi HTTP 429 se rozesílají podle **sub-kódu
Z.AI** (viz
[architecture.md → Model souběžnosti](architecture.md#model-souběžnosti)):
**globální 429/1302 cooldown** pro skutečné `Rate limit reached for requests`
(jedno 1302 zaparkuje všechny workery, protože bezplatná vrstva Z.AI
kaskádově zpomalí každý model, jakmile jeden model dostane 429), krátká
retry s rozestupem intervalu pro `1305 The service may be temporarily
overloaded` (kapacita serveru — chronické u bezplatných flash modelů a NE
naše vina, takže nikdy neozbrojuje fleet cooldown; po vyčerpání rozpočtu
pokusů smyčka propadne na placený finální model a fleet-wide série po sobě
jdoucích odmítnutí pozastaví přetížený model na ~3 minuty) a přeskočení
modelu pro `1308 Usage limit reached` (kvóta vyčerpána do konce běhu).

**Tolerantní JSON** — modely GLM často emitují nevalidní JSON: pythonové
literály (`None`/`True`), koncové čárky, **neescapované dvojité uvozovky
uvnitř řetězcových hodnot** (`"reasoning": "...contains "PROLOG"..."`),
**surové řídicí znaky** (nové řádky) uvnitř řetězců, zkrácení na limitu
tokenů a **JSON zabalený do komentářů** — platný objekt následovaný
vysvětlující prózou a druhou kopií v ohraničeném bloku. `_parse_llm_json`
všechny zachraňuje: levný vestavěný sanitizér ošetří běžné případy, pak
`json-repair` (extra `[llm]`) dorovná ty těžší a ze zabalených odpovědí se
vyřízne první vybalancovaný `{...}` objekt (sledováním hloubky závorek s
respektem k řetězcům, takže to funguje i bez json-repair; výsledek json-repair
typu seznam dá svůj první dict — odpověď modelu, ne její přepověděnou
kopii), takže se téměř perfektní odpověď nikdy nevyhodí kvůli syntaktickému
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
| `keep` | jako `accept`, ale záznam se v review.yaml zachová (neodstraní se) |
| `verified: true` | trvalá značka uživatele „OK": apply ji uloží do metadata.json, analyze knihu pak přeskočí a apply ji umístí na cílovou cestu. Analyze ji předvyplní, když vlastní návrh knihu doplní (projektovaný stav po apply je detektorově čistý), `bmf normalize` ji předvyplní u nového deterministického záznamu (HIGH), jehož projekce je rovněž čistá — zbylé chybějící pole knihu drží otevřenou — nebo — u akceptovaného záznamu — když je FINÁLNÍ identita potvrzena proti obsahu A nezávislým záznamem: online zdrojem, odpovědí `llm:high` s potvrzenou identitou, jejíž autor/série prošel existenciální kontrolou, nebo samotnou content tierí (stamp accepted-missing / fix z text_meta — titul+autor vázané k textu vlastních stránek knihy; vložené OPF se nikdy nepočítá), nebo pool autorů (obsah neměl proti čemu potvrzovat — skenované PDF, chybějící titulní strana — ale autor je etablovaný autor knihovny s ≥3 knihami a titul není jméno autora ani název série; dokazuje autora, ne titul — odsouhlasený trade, znovu otevíratelné přes `--recheck-ok`). Content tier zavírá knihy, které žádný zdroj nezná, místo aby je každý běh znovu extrahoval; verified kniha bez obálky se už enricherů nedotazuje (znovu otevře `--recheck-ok`). Flash tier a nepotvrzené odpovědi se nepočítají; benigní chybějící pole (MISSING_*) zůstat smí (a i C2 s titulem shodným s názvem souboru knihy — po potvrzení identity šum, ne korupce), zbylý NEEDS_REVIEW to blokuje |

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

1. **databazeknih.cz podle ISBN** (při `--databazeknih`) — přesná shoda;
   nejlepší pro CZ/SK. Vrací žánry (široké kategorie + uživatelské štítky),
   ISBN, vydavatele, jazyk, popis, obálku. Scraping (bez API klíče),
   2 požadavky/kniha.
2. **Vlastní CZ provider podle názvu** (při `--abs-czech URL` /
   `BMF_ABS_CZECH_URL`) — vaše instance
   [audiobookshelf_czech_metadata](https://github.com/stecik/audiobookshelf_czech_metadata),
   která agreguje ~17 CZ audioknihových e-shopů za ABS custom-provider API
   `/search`. Metadata audio vydání (nakladatelství, rok, obálka, žánry,
   jazyk); bez ISBN endpointu (jen titul+autor). Keyword vyhledávání
   provideru vrací volné shody (Rozhlas/podcast řádky s autorem `?`), proto
   konfliktní autor match vždy odmítne a match bez autora vyžaduje téměř
   přesný titul (≥ 90). Když stejnou knihu nabízí
   více e-shopů, vyhrává obálka v nejvyšším rozlišení (změřená průtokovým
   přečtením hlavičky obrázku).
3. **databazeknih.cz podle názvu** (při `--databazeknih`) — fuzzy shoda
   názvu hlídá výsledek, takže se nepřipojí žánry špatné knihy; upřednostňuje
   vydání ze shodného roku.
4. **legie.info** (při `--legie`) — CZ/SK sci-fi/fantasy; povídky a série/
   vesmíry, které vyhledávání knih na databazeknih přehlíží (jen identita).
5. **OpenLibrary podle ISBN** — mezinárodní vydání; slabé pokrytí CZ
   (~10 %).
6. **Google Books podle ISBN** — často rate-limitované bez API klíče.
7. **OpenLibrary podle názvu**.

Kniha s cover diagnózou (C11 / MISSING_COVER) navíc porovná obálky obou CZ
zdrojů mezi sebou: vyhrává vyšší rozlišení a vyměňuje se pouze `cover_url` —
metadata z vítězného vyhledávání zůstávají.

## Vzory pro umísťování

`bmf apply` umístí každou aplikovanou knihu: čisté / `verified` se přesunou
na cestu postavenou z formátovacího řetězce (výchozí
`{author}/{title} ({id})`); knihy s nevyřešenými problémy jdou do
`<library>/<needfix-dir>/<original relative path>` (výchozí `needfix/`),
se zachováním struktury, abyste mohli dohledat provenienci; mrtvé záznamy
(bez knižního souboru) do `needfix/empty/`. Vyřešená kniha se při příštím
apply vrátí z needfix zpět ven.

| Pole | Příklad | Poznámky |
|---|---|---|
| `{author}` | `Karel Čapek` | první autor |
| `{author_sort}` | `Čapek, Karel` | „Příjmení, Jméno" |
| `{title}` / `{title_sort}` | `R.U.R.` | title_sort přesouvá úvodní The/A/An |
| `{id}` | `4895` | calibre_id |
| `{isbn}` | `9788072451648` | prázdné, pokud chybí |
| `{year}` / `{language}` | `1920` / `ces` | |
| `{series}` / `{series_index}` | `Ren Dhark` / `3` | prázdné, pokud není v sérii |
