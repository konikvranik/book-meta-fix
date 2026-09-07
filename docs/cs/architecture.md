[English](../architecture.md) | **Čeština**

# Architektura

Tento dokument popisuje vnitřní strukturu `book-meta-fix` (`bmf`): mapu
modulů, tok dat pro jednu knihu, model souběžnosti a klíčová rozhodnutí
o návrhu. Význam kategorií poškození (*co* znamenají) najdete v
[concepts.md](concepts.md); *jak věci spouštět* v
[how-to/index.md](how-to/index.md); referenci příkazů v [README](../../README.cs.md).

## Cíl

Opravit metadata knihovny e-knih ve stylu Calibre s ~5 000 knihami, kde je
mnoho záznamů poškozených (zaměněný autor/název, název souboru jako název
knihy, mojibake, překladatelé uvedení jako autoři, vygenerované zástupné
obálky). **Zdrojem pravdy je `metadata.json`** (manifest Audiobookshelf);
při zápisu se aktualizují oba soubory, `metadata.json` i `metadata.opf`,
takže se oprava po opětovném skenování projeví v Audiobookshelf i Kavita.
Každou změnu hlídá human-in-the-loop `review.yaml`, ledaže jde o vysoce
spolehlivou automatickou opravu.

## Celkový tok dat

```
                     library/  (Calibre-style folders)
                          │
                          ▼
            ┌─────────────────────────┐
            │  readers.py + library.py│  parse metadata.json/opf + path
            │  (+ SQLite cache)       │  → BookMeta
            └────────────┬────────────┘
                         ▼
            ┌─────────────────────────┐
            │   detectors.py (C1–C11) │  classify corruption → Diagnosis
            └────────────┬────────────┘
                         ▼
            ┌─────────────────────────┐
            │  extractors.py          │  read embedded meta + first-page text
            │  (+ text_meta mining)   │  + ISBN from content → ExtractedMeta
            └────────────┬────────────┘
                         ▼
            ┌─────────────────────────┐
            │   verifier.py           │  compare DB meta vs BOOK CONTENT
            │   (do NOT trust embed)  │  → VERIFIED / NEEDS_REVIEW / UNCERTAIN
            └────────────┬────────────┘
                         ▼
   ┌─────────────────────────────────────────────┐
   │  Fix cascade (cheap first, LLM last):       │  pipeline.py
   │   1. text_meta     (offline page-text mine) │
   │   2. ISBN lookup   (OpenLibrary/Google)     │  ← enrichers.py
   │   3. title lookup  (databazeknih.cz best)   │
   │   4. embedded-OPF compare (weakest)         │
   │   5. LLM fallback  (Z.AI GLM, self-correct) │  ← llm.py
   └────────────────┬────────────────────────────┘
                    ▼  (EnrichedMeta proposal or None)
        ┌───────────────────────┐
        │   review_writer.py    │  stream proposals → review.yaml
        │   (one `---` doc/blk) │  (tail -f live; .bak carry-over)
        └───────────┬───────────┘
                    ▼  human sets action: accept/delete/keep
                    (edits the proposed values; null = field delete)
        ┌───────────────────────┐
        │   review.py (parse) → │  bmf apply
        │   writers.py          │  atomic write metadata.json + .opf (.bak)
        └───────────┬───────────┘
                    ▼  optional downstream commands
   ┌────────────────┐  ┌───────────────┐  ┌───────────────────┐
   │ mover.py       │  │ epubgen.py    │  │ crosscheck.py     │
   │ bmf epubgen    │  │ bmf crosscheck │  │ (organize: stub)  │
   │ (OK→clean path)│  │ (missing epub)│  │ (rogue formats)   │
   └────────────────┘  └───────────────┘  └───────────────────┘
```

Vertikální páteř (`scan → detect → extract → verify → fix cascade → review`)
běží uvnitř **jednoho** příkazu: `bmf analyze`. Ostatní příkazy jsou buď
pohledy jen pro čtení (`scan`, `report`), nebo navazující zapisující
příkazy (`apply`, `epubgen`, `crosscheck`; `organize` je zastaralý stub — umísťování běží uvnitř `apply`).

## Mapa modulů

Veškerý zdrojový kód leží v `src/book_meta_fix/`. Testy zrcadlí název modulu
(`tests/test_<module>.py`).

| Modul | Odpovědnost |
|---|---|
| `models.py` | Hlavní datové třídy: `BookMeta`, `Diagnosis`, `Book`; enumy `Verdict` a `Confidence`. Společný slovník, kterým mluví všechny ostatní moduly. |
| `readers.py` | Naparsuje `metadata.json` (primární) a `metadata.opf` (fallback) do `BookMeta`; dále `parse_path()` (název složky je slabý signál). |
| `library.py` | Prochází strom knihovny, aplikuje SQLite cache, pro každou složku knihy vrací `BookMeta`. Vylučuje pracovní adresáře Calibre, soubory s tečkou na začátku (dotfiles) a zámky MS-Word `~$`. |
| `detectors.py` | Pravidla C1–C11 → `Diagnosis`. `detect()` vrací shodu s nejvyšší prioritou a zbytek připojuje jako `.additional`; `detect_all()` vrací všechny shody. |
| `extractors.py` | Extrakce obsahu podle formátu: vložená metadata + text první strany + ISBN z textu. Multi-format fallback, když je primární soubor rozbitý. → `ExtractedMeta` |
| `text_meta.py` | Offline dolování textu strany (titulní strany velkými písmeny, štítky `Název:`/`Autor:`, zahazování `Neznámý`). První, bezplatná fáze kaskády oprav. |
| `encoding.py` | Detekce + oprava mojibake (oktalové escape `\376\377...` a špatně dekódované cp1250/iso-8859-2). Neobnovitelná pole označí pro LLM. |
| `isbn.py` | Extrakce/kanonizace/validace ISBN (10/13místné, s pomlčkami, s prefixem `ISBN:`, koncové `X`). |
| `verifier.py` | Porovnává DB metadata vůči **skutečnému obsahu** knihy (ne vloženým metadatům). Kaskádové signály: shoda ISBN → fuzzy název → UNCERTAIN. `verify_proposal` + `confirm_identity` hlídají návrhy LLM/enricherů. |
| `enrichers.py` | Online zdroje metadat: vlastní provider audiobookshelf_czech_metadata (opt-in přes `BMF_ABS_CZECH_URL`, agregátor CZ e-shopů), databazeknih.cz (CZ/SK, scrape), legie.info, OpenLibrary, Google Books. `Enricher` obaluje `RateLimiter` na host + SQLite cache. → `EnrichedMeta` |
| `pipeline.py` | Orchestrace: `run_pipeline()` provádí knihy skrz detect→extract→verify→fix-kaskádu, paralelizovaně přes `ThreadPoolExecutor`. `_process_book()` je stavový automat jedné knihy. |
| `llm.py` | Poskytovatel LLM Z.AI (kompatibilní s OpenAI) — opravce poslední instance. Vyhlazovač rychlosti `LeakyBucket` + globální 429 cooldown; `reconcile_loop` (Flash→feedback→final); tolerantní parsování JSON. → `ReconciledMeta` |
| `review_writer.py` | Streamovaný zapisovač `review.yaml`: fronta + zapisovací vlákno přidává jeden YAML dokument za každou dokončenou knihu (styl unixové roury). Přenos `.bak` zachovává předchozí rozhodnutí uživatele. |
| `review.py` | Parsuje `review.yaml` (multi-doc stream + legacy jeden seznam) → revizní záznamy s `action`. |
| `writers.py` | Atomické zapisovače pro `metadata.json` + `metadata.opf` (`.tmp` + `os.replace`, historie `.bak`). |
| `mover.py` | move/merge engine pro umísťování v apply: čisté/verified knihy na vzor cesty, nevyřešené do `needfix/`, mrtvé záznamy do `needfix/empty/`. |
| `epubgen.py` | `bmf epubgen`: generuje chybějící `.epub` z nejlepšího sourozeneckého formátu (calibre `ebook-convert` → `pandoc`). |
| `crosscheck.py` | `bmf crosscheck`: ověřuje, že víceformátové složky obsahují *tu samou* knihu; soubory cizích formátů karanténuje do izolovaných složek `needfix/`. |
| `covers.py` | Detekuje vygenerované (zástupné Calibre) obálky pixelovou analýzou; stahuje skutečné obálky z `cover_url` enricheru. C11 + MISSING_COVER. |
| `cli.py` | CLI přes `click`: `scan`, `report`, `analyze`, `apply`, `epubgen`, `crosscheck`, `gui` (plus zastaralý stub `organize`). Tenká vrstva nad výše uvedenými moduly. |
| `config.py` | Datová třída `Config` + loader `.env` s průchodem nahoru (walk-up). Rozlišení: CLI přepínač > proměnná prostředí > `.env` > výchozí hodnota. |

## Hlavní datové modely

```
BookMeta        what the library says now (authors, title, isbn, year, ...)
                normalized: authors=[..], isbn=digits-only-validated, year=int
                + provenance (source: json|opf|path) + encoding_repaired flags

Diagnosis       one detector's verdict on a BookMeta
                category (C1..C11 / OK / VERIFY_FAIL), reason, Confidence,
                Verdict (OK|VERIFIED|AUTO_FIXABLE|NEEDS_REVIEW|UNFIXABLE),
                proposed{...}, + .additional[] (other rules that also matched)

ExtractedMeta   what the book FILE actually contains
                embedded meta + first_page_text + broader_text + isbn_from_text
                (the independent signal the verifier trusts)

EnrichedMeta    a proposed fix from the cascade (online or LLM)
                + source ("openlibrary" / "databazeknih" / "llm:flash" / ...)
                + identity_confirmed (set when verify agrees)

ReconciledMeta  raw LLM output (title, authors, isbn, ..., confidence, reasoning)
```

Kniha protéká `BookMeta → Diagnosis → (ExtractedMeta) → (EnrichedMeta) → review`.
`Verdict` rozhoduje, kde skončí: knihy `OK/VERIFIED` jsou způsobilé pro
umísťování v apply; vše ostatní je `NEEDS_REVIEW` a skončí v `review.yaml`. `bmf apply` navíc umístí každou aplikovanou knihu (dřívější `bmf organize`): čisté/`verified` → vzor cesty, nevyřešené → `needfix/`, mrtvé záznamy → `needfix/empty/`.

## Kaskáda oprav (nejdřív levné)

`pipeline._process_book` se pro každou knihu `NEEDS_REVIEW` snaží obnovit
správná metadata v pořadí podle nákladů, takže na LLM se dostane jen jako
na poslední možnost:

1. **Offline dolování textu strany** (`text_meta`) — název/autoři/ISBN/rok/
   vydavatel vytěžené z textu první strany, který už byl extrahován pro
   verifikaci. Bez sítě.
2. **Online podle ISBN** (z textu > vložené) — OpenLibrary + Google Books.
3. **Online podle názvu + autora** — to je cesta, která dosáhne na
   **databazeknih.cz**, nejsilnější CZ/SK zdroj.
4. **Porovnání s vloženým OPF** (nejslabší — calibre mohlo OPF přepsat).
5. **LLM fallback** (`llm.reconcile_loop`) — jen když fáze 1–4 všechny
   selžou.

LLM je navíc podmíněno tím, že kniha má *použitelný* text první strany
(spořič nákladů č. 1: čistě obrázkové titulní strany se přeskočí a nikdy
se na API neposílají).

## Model souběžnosti

`run_pipeline()` zpracovává knihy ve `ThreadPoolExecutor` (fond vláken;
`--workers`, výchozí 10). Práce na jednu knihu je vázaná na I/O (extrakce
obsahu, HTTP dotazy, volání LLM), takže vlákna škálují dobře. Sdílené
objekty jsou bezpečné pro vlákna:

- **openai klient** — interní `httpx.Client` je thread-safe.
- **requests Session** (enrichery) — thread-safe pro GETy.
- **SQLite cache Enricheru** — `check_same_thread=False`, serializováno
  spojením.

Tři vrstvy drží rychlost volání LLM pod dynamickým RPM limitem Z.AI:

1. **Vyhlazovač leaky-bucket** (`LeakyBucket` v `llm.py`) — omezovač počtu
   za čas sdílený napříč všemi workery **per endpoint URL** (při flash
   splitu kapě každý endpoint — a adaptuje se — sám za sebe). S výchozím
   `--llm-burst 1` je to
   čistě rovnoměrné kapání: přesně jedno volání startuje každých
   `--llm-min-interval` (výchozí 2.0 s ≈ 30 rovnoměrně rozložených RPM),
   bez hromadění. Burst >1 by pustil několik volání ve stejné sekundě —
   přesně to, co shodí dynamický RPM limit — takže zůstává na 1, pokud
   nemáte potvrzenou rezervu.
2. **Strop souběžných volání** (`MAX_INFLIGHT_CALLS` / `--llm-max-inflight`,
   výchozí 3, plus přísnější sub-strop `FLASH_INFLIGHT_CALLS = min(2, ·)` pro
   flash-modely) — coding plan připouští jen ~5 souběžných požadavků na
   *účet* (Z.AI přesná čísla nezveřejňuje: limity jsou tierové a dynamické
   podle usage policy Devpacku; změřeno: 12hluboký spike dostal 7× 429/1302,
   zatímco 6hluboký burst prošel, a interaktivní klienti — ZCode/chat —
   čerpají ze stejného stropu). Bucket rozestupuje *starty* volání, ne jejich
   *hloubku*: s 10 workery a vícesekundovými reasoning voláními fallback
   stádo (flash padne → všichni se najednou přelijí na placený model) strop
   překročilo a bouře se projevovaly i jako **falešné 1113** „insufficient
   balance" při jasně zbývající kvótě (vlastní FAQ Z.AI přiznává 1113 na
   koupeném balíčku). Globální strop je `_InflightGate` — semafor s
   za běhu měnitelnou kapacitou — protože externí klienty na stejném plánu
   nelze vidět: každé 1302/falešné 1113 uvolní jeden slot (podlaha 1) a
   `INFLIGHT_RECOVER_SUCCESSES` čistých 200 si ho vrátí; je to protějšek
   adaptivního dripu v dimenze hloubky. Semafor se drží jen okolo samotného
   HTTP požadavku (čekání na cooldown a acquire bucketu zůstávají venku,
   jinak by zaparkovaná flotila deadlockla na slotech místo čekání na
   hodinách); flash volání získávají flash semafor uvnitř globálního (fixní
   pořadí), takže flash vlna nemůže vytlačit placený fallback z globálních
   slotů — workeři tedy čekají lokálně ve frontě, místo aby je Z.AI odmítal.
   HTTP vrstva je jeden sdílený pooled `httpx.Client` pro celý běh:
   keep-alive 60 s (přežije 30s balance pauzy — httpx výchozích 5 s by po
   každé pauze vynutilo nový TLS handshake), pool velikosti cap+2 a konečný
   read timeout (180 s, vstřícné pro reasoning s max_tokens=8000), aby
   zavěšené volání nemohlo obsadit vzácný slot po SDK výchozích 600 s.
3. **Globální 429/1302 cooldown** (jistič) — bezplatná vrstva Z.AI má kaskádovou
   chybu: když *jeden* model dostane 429, ostatní (včetně placeného
   fallbacku) se také zpomalí. Takže když *jakýkoli* worker uvidí 1302
   `Rate limit reached for requests`, nastaví se sdílený deadline cooldownu,
   na který před svým dalším voláním čekají *všechna* vlákna
   (`--llm-rate-limit-base` výchozí 5 s, eskalující 5/10/20/…, respektující
   `Retry-After` serveru, se stropem `--llm-rate-limit-max` výchozí 60 s).
   Jedno 429 zaparkuje celou flotilu, místo aby každý worker dál bušil a
   dostával další 429. Rozlišení sub-kódu je důležité, protože Z.AI vrací
   429 i pro **1305** "The service may be temporarily overloaded" (kapacita
   serveru, chronické u bezplatných flash modelů): ta se krátce retrajuje,
   s rozestupem intervalu a s rozpočtem pokusů, BEZ ozbrojení fleet cooldownu
   — její zaměňování za 1302 dřív měnilo přechodná přetížení v trvalé
   60s lockouty — a pak se propadne na placený model. Fleet-wide série
   po sobě jdoucích odmítnutí 1305/1113 na jednom modelu (resetovaná
   každým 200) jej pozastaví — ~3 min u kapacitních vln 1305
   (`OVERLOAD_PAUSE_SEC`), ~30 s u burstů 1113 (`BALANCE_PAUSE_SEC`, které
   obvykle postihují poslední funkční model) — takže nasycený
   bezplatný pool stojí pár zkušebních requestů místo toho, aby kniha za
   knihou žral sdílený RPM drip. Drip samotný je adaptivní (1302/1113 ho
   roztahují, úspěchy stahují zpět k podlaze) a jsou-li jednou
   pozastaveny všechny modely, je LLM fáze tichým no-opem s jednou
   minutovou idle hláškou. **1308** "Usage limit
   reached" vypne jen dotčený model do konce běhu. Klient openai se staví s
   `max_retries=0`, aby každé 429 vyplavalo až sem ke klasifikaci, místo aby ho
   tiše pohltila SDK retry mimo bucket.

Praktické rady najdete v
[how-to/llm.md → Ladění omezení rychlosti LLM](how-to/llm.md#ladění-omezení-rychlosti-llm).

## Ukládání do cache

Databáze SQLite (`bmf_cache.db`, `--no-cache` pro obejití) cachuje dvě věci:

- **průchod knihovnou** — opětovné parsování `metadata.json`/`.opf` pro
  nezměněné složky se přeskočí (podle mtime), takže opakované běhy
  `report`/`analyze` jsou rychlé.
- **dotazy enricherů** — cachují se kladné *i* záporné výsledky. Záporný
  záznam (`__NOT_FOUND__`) vyprší po `BMF_ENRICH_NEGATIVE_TTL` (výchozí
  7 dní), takže přechodné selhání nebo identita z doby před opravou se
  zkusí znovu.

## Souborové formáty

`extractors.py` zpracovává `.epub`, `.pdf`, `.pdb`, `.mobi`, `.prc`, `.doc`,
`.txt`, `.rtf`, `.html` a komiksové archivy `.cbz/.cbr/.cb7` (přes
`ComicInfo.xml`). `epubgen.py` umí vygenerovat chybějící `.epub` z
kteréhokoli textového sourozeneckého formátu. Volitelné externí nástroje
rozšiřují pokrytí: `pdftotext`/`pdfinfo` (poppler),
`ebook-convert`/`ebook-meta` (calibre), `pandoc`, `tesseract` (OCR pro
skenovaná PDF / čistě obrázkové titulní strany).

## Atomárnost a bezpečnost

- **Zápisy** (`writers.py`) používají `.tmp` + `os.replace()` s historií
  `.bak` — pád uprostřed zápisu nikdy nezanechá napůl zapsaný manifest.
- **`review.yaml`** (`review_writer.py`) se přidává po jednom dokumentu;
  Ctrl-C je bezpečné — vše dosud zapsané už je na disku a `.bak` pořízený
  na začátku se při přerušení běhu zachová, takže předchozí rozhodnutí
  přežijí.
- **Každý příkaz je ve výchozím nastavení dry-run** (`apply`, `epubgen`,
  `epubgen`, `crosscheck`); k mutaci souborového systému je potřeba
  `--apply`.
