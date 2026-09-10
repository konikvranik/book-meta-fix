[English](../corruption-catalog.md) | **Čeština**

# Katalog poškození (C1–C18)

Odvozeno empiricky z CZ/SK calibre knihovny s ~5 440 knihami. Každá kategorie
má reálné příklady (calibre_id, author_folder, title) nalezené během
počáteční studie.

## C1 — Zaměněný autor/název

*Název* knihy se stal složkou *autora*; skutečná jména autorů se stala názvy
knih. Často jde o sérii, kde zdroj uváděl `<author>=series, <title>=contributor`.

| id | složka autora | název | poznámka |
|----|---|---|---|
| 95 | `NŘm Barik da` | `Jan Drda` | skutečný autor = „Jan Drda" |
| 111 | `Schindler v Seznam` | `Thomas Keneally` | Keneally napsal Schindlerovu archu |
| 4357 | `uzivatelska prirucka 31D30588` | `Peugeot 406 - uzivatelská příručka` | záměna |

**Verdikt:** NEEDS_REVIEW (LLM nebo ruční oprava)
**Opravná akce:** `accept` — analyzátor sám navrhne záměnu do `proposed`
(title ← author, author ← title), když nenajde lepší zdroj; v případě
potřeby hodnoty před přijetím upravte

Při `analyze` je pravidlo navíc **pool-armed**: `run_pipeline` postaví
pool známých autorů z celé knihovny (stejné klastrování jako `bmf
normalize`), takže záznam, jehož POLE NÁZVU se rozřeší na známého autora
knihovny, spustí C1 s HIGH jistotou — buď jako *variabilní pár* (název i
autor jsou táž osoba ve dvou pravopisech, např. název `Anatolij Dněprov` /
autor `A. Dněprov` — skutečný název se ze záznamu ztratil), nebo jako
*klasická záměna* (pole autora nese skutečný název; když je samo dalším
známým autorem, reason značí nejednoznačnost — biografické území). Tier
opravy (`_try_known_author_swap`) pak vezme autora z kanonického tvaru
poolu a název z VLASTNÍHO textu knihy, svázané přes `confirm_identity`;
klasická záměna se věří jen tehdy, když vytěžený název souhlasí s polem
autora (biografie nazvaná podle svého hrdiny by prošla naivním self-testem
záměny). Každé selhání zůstává v review s hintem surové záměny.

## C2 — Název souboru použitý jako název (odstraněná diakritika)

Kniha byla importována ze souboru; název souboru se stal jak složkou, tak
názvem. Diakritika nahrazena `_`. **Nejčastější kategorie (~47 % knihovny).**

| id | název | formáty | tvar |
|----|---|---|---|
| 1753 | `Kirill_Bulicov-Druha_cesta_k_pr` | epub,pdb | `Autor-Titul` s `_` |
| 3342 | `Buskov_A-Rytirka_Natal_n_` | epub,pdb | končí `_n_` |
| 1416 | `Microsoft Word - 4444.doc` | epub,pdf | dočasný název souboru Wordu |
| 3774 | `Bradbury` | doc | název je jen příjmení autora |
| 2497 | `Cas prilivu` | epub | má být „Čas přílivu" |

**Verdikt:** NEEDS_REVIEW (správný název poznáme až z obsahu/online)
**Opravná akce:** `accept` (pokud návrh potřebuje opravu, upravte hodnoty
`proposed`)

## C3 — Série/knihovna/vydavatel použitý jako autor

| id | autor | název | poznámka |
|----|---|---|---|
| 155 | `abeles` | `Dr. Oldrich Elias: Golem – Historicka studie` | štítek knihovny, ne autor |

**Verdikt:** NEEDS_REVIEW

## C4 — Poškození kódování (mojibake)

Pozorovány dvě formy:
1. **Oktalový escape** — `repr()` Pythonu UTF-16 bytů prosákl do JSON
   hodnoty: `\\376\\377\\000K\\000u\\000l\\000h\\000\\341\\000n\\000e\\000k`
   = „Kulhánek"
2. **Špatně dekódované** — byty cp1250 načtené jako cp1251/cp1252/iso-8859-1,
   překódované do UTF-8: `Jiшн Kosek` (cyrilice), `¬as pý¡livu` (latin-1),
   `Kamenáè` (iso-1)

| id | příznak | originál |
|----|---|---|
| 5687 | `\\376\\377\\000K...` | Kulhánek Jiří-Stroncium |
| 2184 | `'Darko\uffbd je bytost'` | Darkon doma a na cestách |
| 1795 | `1. ZAĚTEK VELIKÝ CESTY` | Svandrlik Příliš tlustý dobrodruh |

**Verdikt:** NEEDS_REVIEW (LLM pro neopravitelné případy)
**Poznámka:** mnoho z nich modul kódování opraví automaticky; do revize se
dostanou jen neopravitelné.

## C5 — Zástupný záznam

Doslovný prázdný zástupný záznam s title="title", author="author".

| id | autor | název |
|----|---|---|
| 5575 | `author` | `title` |

**Verdikt:** AUTO_FIXABLE (smazání)

## C6 — Duplikát zámku MS-Word

`~$` je prefix dočasného zámku Wordu; jde o duplikáty.

| id | složka autora |
|----|---|
| 3690 | `~$N. Shearer` |

**Verdikt:** AUTO_FIXABLE (smazání)

## C7 — Slepení autoři

Tokeny autorů slepené dohromady bez mezer kolem spojek.

| id | autor | má být |
|----|---|---|
| 317 | `byKathy SierraandBert Bates` | Kathy Sierra, Bert Bates |

**Verdikt:** NEEDS_REVIEW

## C8 — Překladatel na místě autora

**POLITIKA (2026-09):** překladatel MEZI autory oddělenými čárkou je
PŘIJATENÝ stav, ne závada. Audiobookshelf modeluje jen autory a vypravěče —
pole pro překladatele nemá — takže položka v seznamu autorů je to jediné,
co dělá knihu dohledatelnou podle překladatele. Starý signál („česky
ypadající jméno po boku zahraničního autora → navrhni odstranit CZ jména")
označil každý smíšený seznam v knihovně (47 knih, u všech vedl zahraniční
autor správně první) a jeho návrh byl mrtvý: samostatné pole `translators`
není nikde modelované, takže použitá oprava by jména mlčky smazala.

Co se z metadat dá PROKÁZAT — položka autora s explicitním překladatelským
označením (`přeložil František Jungwirth`, `Překlad: J. Novák`,
`translated by X`): odstranit označení, holé jméno nechat v seznamu.
V knihovně dnes nula výskytů; zůstává jako levný bezpečnostní filtr bez
falešných poplachů pro budoucí calibrovské reimporty.

ZAMĚRNĚ se nedetekuje: pořadí s překladatelem prvnímu, nebo samotný
překladatel se ztraceným skutečným autorem. Heuristika na jména nerozliší
vedoucího překladatele od českého adaptora/editora legitimně na prvním
městě (změřené falešné poplachy: „Kate Wilhelmová" — americká autorka s
počeštělým příjmením; „Josef V. Pleva, Daniel Defoe" — český adaptér
první z konvence), a 0 knih knihovny je dnes v tvaru „překladatel
první". Tyto závady patří tokům s obsahem: identitní verifikátor chytne
primárního autora, kterému odporuje vlastní text knihy, `text_meta`
doloví „přeložil X" z textu stránek.

**Verdikt:** NEEDS_REVIEW

## C9 — Anonym (většinou falešný)

**MINULOST:** 99,7 % záznamů `Neznamy` v této knihovně je poškozených, ne
skutečně anonymních. Reálných je jen ~5 (Bible). Detekce dává náboženské
tituly na whitelist; vše ostatní s anonymním zápisem označí.

| zápis | počet v knihovně |
|---|---|
| `Neznamy` | 1844 |
| `Unknown` | 73 |
| `Neznámý` | 19 |

| id | autor | název | skutečný? |
|----|---|---|---|
| 5485 | anonym | Nová Bible kralická (Knihy Mojžíšovy) | YES (na whitelistu) |
| 2235 | Neznamy | `0 DEV_T PRINC_ AMBERU` | NO (poškozeno) |

**Verdikt:** NEEDS_REVIEW (pokud není na whitelistu → OK)

## C10 — Dlouhý seznam autorů *(vyřazeno 2026-09)*

**MINULOST:** 4+ autorů — „ověř antologii vs překladatelský tým",
NEEDS_REVIEW. Obě hypotézy jsou pod C8 politikou výšu přijaté stavy
(skuteční spoluautoři antologie, nebo překladatelé připojení kvůli
vyhledávání), takže už není co ověřovat. Změřeno v knihovně: všech 12
označených záznamů byly skutečná díla více autorů, většinou už ručně
přijatá. Nové C10 diagnózy se negenerují; kód zůstává v pořadí
kategorií, aby se historické review záznamy zobrazovaly.

| id | název | počet autorů |
|----|---|---|
| 197 | Soumrak světů | 13 (skutečná CZ SF antologie) |
| 4411 | Kuchařka stařenky Oggové | 4 (Briggs, Pratchett, Kantůrek, Kidby) |

**Verdikt:** — (už se neoznačuje)

## C13 — Nesouhlas umístění (složka ≠ metadata)

Složka knihy neodpovídá cílové cestě odvozené ze vzoru
(`{author}/{title} ({id})` ve výchozím stavu; viz `--pattern` /
`BMF_PATTERN`). Nejde o poškození samotných metadat — záznam je v pořádku,
kniha jen leží na špatném místě (přejmenování v calibre, které složku nikdy
nepřesunulo, pobyt pod `needfix/`, který už je vyřešený, …).

Čistá cestová matematika: žádné čtení obsahu, žádné online dotazy.
Vyhodnocuje se jen při analyze se zapnutou kontrolou umístění (výchozí
stav); `report`/`epubgen` umístění neřeší. Hodnota `proposed.location` je
informativní — `bmf apply` cíl přepočítá z FINÁLNÍCH metadat (v témže
průchodu můžeš opravit autora/název). Když je C13 jediným skutečným
problémem (nanejvýš s benigními doprovody — verdiktem OK, MISSING_* nebo
diagnózou obálky C11/MISSING_COVER, kterou apply v témže průchodu zkusí
obnovit), položka v
review dostane předvyplněné `action: accept`, takže zdravá, ale špatně
umístěná kniha se přesune hromadně; návrh, který mění autora/název,
zůstává na individuální kontrolu. Kniha pod `needfix/`, jejíž problémy
byly vyřešeny, se stejnou cestou vrací zpět do kořenového stromu.

**Verdikt:** AUTO_FIXABLE (přesun)

## C14 — Pořadí série zalepené v názvu série

Název série nese pořadí knihy — třeba `Mark Stone #73` jako název s
PRÁZDNOU hodnotou pořadí. Importery ABS/calibre občas uloží celý řetězec
„Název #N" do pole názvu; GUI pak zobrazuje rozbitou sérii, vyhledávání
podle série seskupuje podle znečištěného názvu a vzor umístění `{series}`
by zapsal „#N" i do názvu složky. Průzkum knihovny našel 353 takových
knih (Asterion, Agent JFK, Mark Stone, …).

Dělí se jen explicitní přípona `#N` — koncové holé číslo („MR-362
Espace 4") může být součástí skutečného názvu a záměrně se nedotýká.
Uložené pořadí, které NESOULASÍ s číslem v názvu, je vědomý stav a
pravidlo pak nevyhodnotí (shodné pořadí ano — špatný je tehdy jen název).

Oprava je deterministická a bezztrátová: holý název + `series_index`
(„Mark Stone" + 73). Položka v review dostane předvyplněné
`action: accept` a když je rozdělení jediným problémem knihy, i
`verified: true` — analyze + apply opraví celou knihovnu hromadně. GUI
v režimu `+ knihovna` navíc předvyplní tentýž návrh u knih, které
vyhledávání přineslo mimo review.

**Verdikt:** AUTO_FIXABLE (rozdělení)

## C15 — Varianty jména autora (úroveň knihovny)

Tentýž člověk zapsaný několika způsoby napříč knihami: „Robert A.
Heinlein" vs „Robert Anson Heinlein" (iniciála vs celé druhé jméno),
„Jiří Kulhánek" vs „Jiri Kulhanek" (diakritika), NFD-rozložené duplicity,
kopie velkými písmeny nebo mojibake („JiĹ™Ă Kosek"), akademické tituly
(„Ing. Václav Semerád"), rodina anonymů („Neznamy"/„Neznámý"/„Unknown" —
5 zápisů, ~107 knih) a prohozené pořadí jména a příjmení („James, Peter"
v calibre konvenci *Příjmení, Jméno*, nebo obyčejné „Podroužek Přemysl").

Detektor pracující s jednou knihou tohle vidět nemůže — vzor se objeví
až MEZI knihami. `bmf normalize` (jediný zdroj kategorie C15) pravopisy
naklastruje. Deterministická sloučení dostanou předvyplněné
`action: accept`: shodné po foldu, iniciály proti celým jménům, tituly,
kapitálky, rodina anonymů a prohozené pořadí doložené klastrem (vyhrává
většinový zápis). Úsudkové případy zůstávají pending na člověka:
varianty lišící se písmeny („Frederik"/„Frederick", „Miloslav"/
„Miroslav" — stejné iniciály mohou mít i dva různí lidé) a nedoložená
čárková forma („Weis, Hickman" mohou být dva autoři spojení čárkou).
Kanonická podoba je nejčastější NEpoškozený zápis — „Neznamy" ×63
prohraje s „Neznámý" ×24. Multi-author řetězce („Wilhelm a Jacob
Grimmové") se přeskočí; jejich štěpení je území C7/C8.

**Verdikt:** AUTO_FIXABLE (výměna celého seznamu přes `proposed.authors`),
NEEDS_REVIEW u úsudkových případů

## C16 — Varianty názvů žánrů/tagů (úroveň knihovny)

Řetězce žánrů lišící se jen velikostí písmen („Sci-fi"/„sci-fi" — 1 235
knih v průzkumu), diakritikou, pořadím slov („Literatura česká"/„česká
literatura"), jazykem („Science Fiction", „Comedy", „Thriller") nebo
pravopisem („mumour"). Žánry a tagy se kanonizují proti JEDNÉ společné
slovní zásobě — oba se serializují do OPF `dc:subject` a nesmí dospět k
různým verzím téhož názvu.

`bmf normalize` je sjednocuje na české názvy pouze dvěma deterministickými
mechanismy: fold skupiny (duplicity velikost písmen/diakritika/pořadí
slov; zástupce je nejčastější podoba z vlastní knihovny) a kurátorovaná
tabulka `GENRE_ALIASES` v `normalize.py` (angličtina→čeština,
jednotné/množné číslo, pozorované překlepy — každé sloučení je explicitní
řádek ke kontrole). Fuzzy automatické porovnávání zde záměrně CHYBÍ: na
1 621 názvech reálné knihovny byly „překlepy" na vzdálenosti 2 z ~50 %
falešné (Afrika→Amerika, vlaky→války, etika→erotika). Dlouhý ocas
(„Hugo (literární cena)") se nechá být, pokud ho nepokrývá řádek tabulky.

**Verdikt:** AUTO_FIXABLE (výměna celého seznamu přes `proposed.genres` /
`proposed.tags`)

## C17 — Neplatný soubor e-knihy (nezachovatelný obsah)

Soubor e-knihy, jehož OBSAH nelze rozpoznat jako žádný formát knihy:
0 bajtů, binární šum bez signatury (žádný ZIP / `%PDF-` / `BOOKMOBI` /
`Rar!` / struktura PalmDB, není čitelným textem), čitelný ZIP bez
knižního obsahu (bez `META-INF/container.xml`, bez obrázků, bez čitelných
textových položek), nebo zkrácený ZIP se ztraceným centrálním adresářem.

Emituje POUZE `bmf clean --files` (opt-in; nikdy `bmf analyze` —
platnost souborů záměrně zůstává mimo tok autokorekcí). Sondy jsou
obsahové, ne podle přípony: platná kniha uložená pod špatnou příponou
(EPUB pojmenované `.pdf`) se rozpozná a nikdy se nenavrhuje — přípona
pouze rozhoduje, zda *nerozpoznaný* obsah smí být vůbec prohlášen za
neplatný (`_FULLY_PROBED_SUFFIXES` v `filecheck.py`; formáty s variantami,
které bmf nedokáže spolehlivě identifikovat, jako `.prc`/`.pdb`, se
nikdy neflagují). Záměrné mezery, bezpečnost před úplností: zkrácené PDF
stále začíná `%PDF-`, textový odpad (HTML chybová stránka uložená jako
`.epub`) se stále jeví jako zachovatelný text. Když soubor čistě přečte
calibre `ebook-meta` (exit 0 a prázdný stderr — změřeno: u smetí končí 0
s tracebackem a fallbackem na název souboru), soubor NEPLATNÍ není.

Nové položky přichází s předvyplněným `action: delete` a
`proposed.delete_files`; GUI filtruje podle stavu delete a Ctrl+Shift+D
hromadně ruší rozhodnutí (Ctrl+Shift+R odebírá položky z review).
`bmf apply` maže jen pojmenované SOUBORY (složka a zdravé formáty v ní
zůstávají), těsně před smazáním každý soubor překontroluje (soubor,
který se stal platným — nebo zmizel — se přeskočí) a vše archivuje do
`deletion_snapshot_*.tar.gz`. Smazání jediného knižního souboru
kaskáduje: další běh nahlásí EMPTY_BOOK a přesune složku do
`needfix/empty/`.

**Verdikt:** NEEDS_REVIEW (návrh smazání; vždy schvaluje člověk, nikdy se neaplikuje automaticky)

## C18 — Varianty názvů sérií (úroveň knihovny)

Tentýž název série zapsaný několika způsoby napříč knihami: „Zaklínač" /
„zaklínač" / „Zaklinac" (velikost písmen/diakritika), „Perry Rodan" /
„Perry Rhodan" (skutečné přejmenování), nebo příbuzný název s příponou
„Mark Stone" / „Mark Stone (edice)". Stejně jako C15/C16 je to neviditelné
pro detektor jedné knihy — nekonzistence je vlastností celé knihovny.
Pořadové číslo dílu se NIKDY nenavrhuje, jen název (`_apply_fields`
si u každé knihy drží vlastní polovinu páru název/číslo).

`bmf normalize --series` klastruje názvy ve třech vrstvách důkazů:

1. **Fold (deterministické, předvyplněno accept):** NFC + casefold +
   diakritika ven + interpunkce na mezery, mezery slepené. Pořadí slov se
   ZACHOVÁVÁ (na rozdíl od žánrů: „Legenda o Drizztovi" ≠ „Drizztova
   legenda" — blízké pod-série se nesmí automaticky sloučit). Kanonik =
   nejčastější původní pravopis skupiny. Zlepené „#N" na konci názvu se ze
   skládacího klíče odstraní, ale jinak se zlepený tvar PŘESKOČÍ — tyto
   knihy už vlastní rozdělení C14.
2. **Podezřelé dvojice (pending):** předponové páry („Mark Stone" /
   „Mark Stone (edice)") a názvy fuzzy-blízké OVĚŘENÉ sérii. Důkazem je
   především ČÍSLOVÁNÍ dílů — dvě množiny čísel, které se doplňují do jedné
   souvislé řady (1,2,4 + 3 ⇒ sloučit), versus kolize (obě tvrdí díl 1 ⇒
   dvě série nebo duplicitní knihy, nikdy neslučovat) — a s `--online`
   také existence názvů v bibliografické DB (základ existuje a podezřelý
   ne ⇒ sloučit; oba existují ⇒ odlišné). Online odpovědi jdou přes
   perzistentní cache enricheru, opakované běhy nic nestojí.
3. **Alias tabulka (`SERIES_ALIASES` v `normalize.py`, pending):**
   ručně psané řádky pro přejmenování, která žádná vrstva nevidí. Na
   rozdíl od `GENRE_ALIASES` (HIGH) řádek přistane jako PENDING —
   přejmenovává celou skupinu najednou a jednorázové potvrzení v gui je
   pojistka proti překlepu v řádku samotném.

Knihy s VÍCE sériemi se přeskočí a vypíšou (aplikace jednoho názvu by
zahozením druhé série ublížila — upravte je v `bmf gui`). Volná fuzzy
vrstva záměrně chybí, stejný trade-off jako C16.

`bmf series` je read-only doplněk: všechny série s počty knih, pokrytím
dílů („1–3, 5 (chybí: 4)"), varováními na duplicitní díly a podezřelými
dvojicemi s verdikty. Chybějící díly jsou INFORMACE (osobní knihovna
nemusí být úplná); číslo dílu, na které si dělá nárok dvě knih, je
VAROVÁNÍ (duplicitní složka nebo špatné číslo).

**Verdikt:** AUTO_FIXABLE (fold) / NEEDS_REVIEW (alias řádky, důkazní
podezřelá sloučení)

## C19 — Duplicitní složky téhož díla (úroveň knihovny)

Ta samá kniha naimportovaná dvakrát žije ve dvou složkách. Výchozí vzor
`{author}/{title} ({id})` dá každému importu vlastní cestu, takže složky
nikdy nekolidují — a apply sloučení při kolizi cíle (které včleňuje knihu do
obsazeného cíle téhož díla) se k nim nikdy nedostane. Duplicity tak
přetrvávají navždy: dva napůl vyplněné záznamy, v každé složce jeden formát,
a žádný enricher nevidí kompletní knihu.

Stejně jako C15–C18 to nemůže vidět per-book detektor — je to vlastnost
celé knihovny. `bmf merge` (nebo `analyze --merge`, který znovu použije scan
daného běhu) shlukne složky téhož díla a zapíše do review.yaml záznamy
`action: merge`:

* **Detekce je záměrně pouze přesná.** Dvě knihy jsou kandidáti, když se
  jejich zfoldovaný první autor + zfoldovaný název shodují (diakritika/
  velikost/interpunkce zfoldována — recept `fold_series`), nebo když obě
  nesou stejné platné (kanonizované) ISBN. Fuzzy vrstva záměrně chybí:
  knihovna-wide sweep na 0,85 nad ~5 000 knihami násobí falešné páry a
  špatný návrh sloučení je šum, který platí uživatel. Překlepaný název tak
  C19 nevidí — opravte ho přes C15 a dvojice se objeví při dalším běhu.
* **Poslední branou je `mover.same_book`** (rozhoduje rok): složky, u kterých
  oba roky existují a liší se, jsou různá vydání a nikdy se neslučují —
  pokud už si nesou shodné ISBN (shodné ISBN určuje též vydání i tehdy,
  když jeden záznam nese špatný rok; sloučení pak doplní chybějící rok
  přeživšího z poraženého). Mrtvé záznamy (bez souborů formátů) se
  vynechávají — ty vlastní EMPTY_BOOK.
* **Přeživšího** volí `mover._pick_base`: vyhrává platné ISBN, pak nejnižší
  calibre_id — deterministicky mezi běhy. Každý další člen klastru dostane
  review záznam, jehož `proposed.merge_into` jmenuje přeživšího.
* **Předvyplnění:** jen ISBN-potvrzené duplicity (obě strany nesou stejné
  platné ISBN) přicházejí předvyplněné `action: merge`; přesná shoda
  autora+názvu bez ISBN důkazu zůstává pending. Záznamy se nikdy nerodí
  `verified` — sloučení složek není důkaz identity.

`bmf apply` vykonává sloučení PŘED hlavní smyčkou (přeživšího může ještě v
témže běhu přesunout jeho vlastní placement). Každé sloučení se znovu
kontroluje proti čerstvému stavu na disku — `same_book` musí stále platit,
metadata se od návrhu mohla změnit — a neprošlá kontrola záznam přeskočí a
nechá ho v review. Soubory formátů poraženého se přesunou do složky
přeživšího (kolize jmen se přejmenovávají s id poraženého, identické bajty
se přeskakují, obálka přeživšího vyhrává), metadata se sloučí pole po poli
přeživší-první a sidecary + obálka poraženého jedou do
`deletion_snapshot_*.tar.gz` pro možnost rollbacku. Přeživší se svým vlastním
decidovaným `delete` na celou složku se nedotkne (konflikt rozhodnutí).
Cache řádky obou složek se invalidují.

**Verdikt:** AUTO_FIXABLE (ISBN-potvrzené) / NEEDS_REVIEW (přesná shoda bez
ISBN důkazu)

## EMPTY_BOOK — Mrtvý záznam (knižní soubor chybí)

Složka obsahuje jen metadata, jejich zálohy a případně obálku — žádný
knižní soubor a žádný podadresář. Kniha sama byla ztracena (nepovedený
import do calibre, smazaný soubor); přežil jen záznam. Není co extrahovat
ani čím ověřovat — záznam nelze potvrdit proti obsahu.

Pravidlo běží PRVNÍ: když chybí samotný soubor knihy, na názoru ostatních
pravidel na metadata nezáleží. `bmf apply` přesune složku do
`needfix/empty/` (se zachováním relativní cesty); metadata zůstávají
nedotčená pro případnou pozdější ruční záchranu. Položka dostane
předvyplněné `action: accept` — přesun je mechanický a vratný. Složka
obsahující jakýkoli jiný soubor (nerozpoznaný formát, zatoulaný dokument)
nebo podadresář prázdná NENÍ.

**Verdikt:** AUTO_FIXABLE (karanténa do `needfix/empty/`)

## MISSING_ISBN / MISSING_YEAR

Není poškození — jen chybějící data, která lze doplnit online dotazem
(databazeknih.cz pro CZ/SK, plus Google Books / OpenLibrary jako fallbacky).

**Verdikt:** AUTO_FIXABLE (obohacení)

## Poznámky ke kalibraci detektorů

- **Samotné C2 je holočné** — 47 % názvů obsahuje `_`. Aby pravidlo vůbec
  spustilo, vyžaduje silnější signál (příponu souboru v názvu, prefix
  dočasného souboru Wordu, přesnou shodu s názvem souboru nebo 3+
  podtržítka).
- **C2 má prioritu před C1** — znečištěné názvy produkují falešné signály
  záměny (název souboru obsahuje autora i název).
- **Výchozí pro C9 je NEEDS_REVIEW**, ne OK — whitelist je jediná cesta,
  jak pro anonymní záznam dostat OK.
