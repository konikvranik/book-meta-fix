# Sjednocení pravopisů autorů, názvů žánrů a sérií

[English](../../how-to/normalize.md) | **Čeština**

```bash
bmf series                       # přehled jen pro čtení: všechny série,
                                 # pokrytí dílů, varianty, podezřelé dvojice
bmf normalize                    # dry-run: vypíše clustery autorů/žánrů/sérií
bmf normalize --apply            # naplní review.yaml návrhy C15/C16/C18
bmf normalize --series --apply   # jen názvy sérií
bmf normalize --series --online --apply   # + vážit podezření proti databazeknih
bmf normalize --genres --apply   # jen žánry+tagy (bez autorů)
bmf normalize --authors          # jen autoři
bmf analyze --normalize          # analyze a po něm průchod normalize nad
                                 # týž sken (bez druhého průchodu knihovnou)
bmf gui                          # revize; hromadné potvrzení Ctrl+Shift+A
bmf apply --apply review.yaml    # zápis metadat + přesuny složek
bmf abs-rescan --apply           # dotlačí změny do Audiobookshelf
```

Tři nepořádky jsou pro per-knihové detektory neviditelné, protože se
projeví až MEZI knihami: tatáž osoba zapsaná několika způsoby (C15),
názvy žánrů lišící se jen velikostí písmen, pořadím slov, jazykem nebo
pravopisem (C16) a tatáž série zapsaná několika způsoby (C18).
`bmf normalize` jednou proskenuje knihovnu, naklastruje varianty a převede
je na obyčejné návrhy do review.yaml — výměny celých seznamů
`proposed.authors` / `proposed.genres` / `proposed.tags` a `proposed.series`
(pouze NÁZEV — každá kniha si drží své pořadové číslo), které `bmf apply`
zapíše jako každou jinou opravu.

## Co se sloučí automaticky (předvyplněné `accept`)

- **Varianty autorů, deterministicky:** též jméno modulo velikost písmen,
  diakritika („Jiří"/„Jiri"), NFD rozložení, tečky u iniciál („G. J."/
  „G.J."/„G.-J."), iniciály proti celým druhým jménům („Robert A." /
  „Robert Anson"), akademické tituly („Ing. Václav Semerád"), kopie
  kapitálkami, rodina anonymů („Neznamy"/„Neznámý"/„Unknown"/„Anonym" →
  „Neznámý") a prohozené pořadí doložené klastrem („James, Peter" →
  „Peter James", protože „Peter James" jinde v knihovně existuje;
  „Podroužek Přemysl" prohraje s většinovým „Přemysl Podroužek").
- **Varianty žánrů/tagů:** duplicity velikost písmen/diakritika/pořadí
  slov („Sci-fi"/„sci-fi", „Literatura česká"/„česká literatura") a
  kurátorovaná tabulka `GENRE_ALIASES` v `src/book_meta_fix/normalize.py`
  (angličtina→čeština: „Science Fiction"→„Sci-fi", „Comedy"→„Humor";
  jednotné/množné číslo: „román"→„Romány"; pozorované překlepy:
  „mumour"). Zástupce je vždy nejčastější podoba z vlastní knihovny a
  žánry s tagy sdílejí jednu slovní zásobu (obě míří do OPF
  `dc:subject`).
- **Fold varianty sérií:** „Zaklínač"/„zaklínač"/„Zaklinac" — stejná
  písmena po NFC + casefold + diakritika ven + interpunkce na mezery.
  Zástupcem je nejčastější původní pravopis skupiny. Pořadí slov se
  NESKLÁDÁ („Legenda o Drizztovi" ≠ „Drizztova legenda" — blízké pod-série
  zůstávají oddělené) a pořadové číslo dílu NIKDY není součástí návrhu:
  mění se jen název, každá kniha si drží své číslo.

## Co zůstane pending (váš úsudek)

- **Varianty autorů lišící se písmeny** — „Frederik"/„Frederick" Pohl,
  „Miloslav"/„Miroslav" Švandrlík, mojibake kopie („JiĹ™Ă Kosek").
  Stejné iniciály může mít i dva různí lidé, proto tyto návrhy přicházejí
  bez předvyplněné akce.
- **Nedoložené čárkové formy** — izolované „Weis, Hickman" mohou být dva
  autoři spojení čárkou, ne jedno prohozené jméno.
- **Alias řádky sérií a podezřelá sloučení sérií** — řádek v
  `SERIES_ALIASES` přejmenovává celou skupinu najednou a předponová/fuzzy
  podezření („Mark Stone" / „Mark Stone (edice)") je úsudek, obojí přichází
  bez předvyplněné akce. Z podezření se stane návrh jen s pozitivním
  důkazem: čísla dílů se se základní sérií DOPLŇUJÍ do jedné souvislé
  řady (1,2,4 + 3 ⇒ sloučit), nebo — s `--online` — základní série v
  bibliografické DB existuje a podezřelý název ne. KOLIZE číslování (obě
  tvrdí díl 1) nebo existence obou online znamená dvě odlišné série:
  dvojice se vypíše v advisory tabulce a nikdy se neslučuje.
- **Pojmenované pod-série zůstávají pod-sériemi** — předponové podezření,
  jehož prodloužení něco POJMENOVÁVÁ („Star Wars" /
  „Star Wars - Akademie Jedi", „Duna Chronicles"), je samostatná linie
  série, ne pravopisná varianta: rovnou dostává verdikt `distinct` a
  žádný důkaz ji neslučuje s „deštníkovou" sérií. Souvislost čísel dílů
  je náhoda (deštník má 3,4 a osamocený díl linie 2) a online existuje
  samotná FRANŠÍZA, ne lokální název linie. Na důkazech se slučují jen
  dekorativní ocasy („(edice)", „(série)"); záměrné sloučení pojmenované
  pod-série patří do `SERIES_ALIASES`.

Různé iniciály křestních jmen se nikdy nesloučí (Karel vs Josef Čapek,
Dan vs Eric Brown zůstávají oddělení). Multi-author řetězce („Wilhelm a
Jacob Grimmové") se přeskočí a vypíší zvlášť — jejich štěpení je území
C7/C8. Stejně se přeskočí a vypíšou knihy s více sériemi (aplikace
jednoho názvu by zahodila druhou sérii — upravte je v `bmf gui`) a
pořadí série zlepené v NÁZVU („Mark Stone #73") se nechává C14, které si
samo předvyplní accept.

## Přehled sérií (`bmf series`)

`bmf series` vypíše read-only inventář: každou sérii s počtem knih,
pokrytím dílů („1–3, 5 (chybí: 4)" — chybějící díly jsou INFORMACE,
osobní knihovna nemusí být úplná), duplicitními čísly dílů (⚠ dvě knihy
si dělají nárok na tentýž díl — duplicitní složka nebo špatné číslo) a
podezřelými dvojicemi s verdikty. Nic nezapisuje a bez `--online` ani
nesahá na síť.

## Přidání sloučení žánrů či sérií

Fuzzy automatické porovnávání žánrů (a sérií) zde záměrně CHYBÍ: na
1 621 názvech žánrů reálné knihovny byly „překlepy" na vzdálenosti 2
z ~50 % falešné (Afrika→Amerika, vlaky→války, etika→erotika). Chcete-li
dvě podoby sloučit, přidejte řádek do `GENRE_ALIASES` a spusťte znovu —
každé sloučení je explicitní řádek ke kontrole. Skutečná přejmenování
sérií fungují stejně přes `SERIES_ALIASES`:

```python
SERIES_ALIASES: dict[str, str] = {
	"Perry Rodan": "Perry Rhodan",          # překlep, fold ho nevidí
	"Mark Stone (edice)": "Mark Stone",     # přípona edice
}
```

Cíl se řeší přes vlastní fold skupinu knihovny (vyhraje nejčastější
pravopis cíle) a — na rozdíl od žánrových řádků — každý řádek sérií
přistane jako PENDING: přejmenovává celou skupinu najednou, takže ho
jednou potvrdíte v `bmf gui` a je hotovo.

## Poznámky k pracovnímu postupu

- `bmf analyze --normalize` spustí tento průchod na konci analyze nad
  TÝMŽE skenem (všechny knihy včetně verified — clustering potřebuje celou
  knihovnu; flag existuje, protože druhý sken stojí minuty i na NFS s
  plně nahraným cache). Sloučení proběhne po finalizaci analyzinho
  writeru, takže vlastní položky analyzeru jsou v bezpečí: PENDING
  položky dostanou návrhy C15/C16/C18 překryté, rozhodnuté se přeskočí.
  Běží všechny čtyři kategorie — pro rozsah `--authors`/`--genres`/
  `--tags`/`--series` nebo nejdřív dry-run použijte samostatný příkaz.
- `normalize --apply` spusťte PŘED `bmf gui`/`bmf apply`. Pozdější
  `bmf analyze` přesune review.yaml do `.bak` a sloučí podle uuid:
  rozhodnuté položky (včetně předvyplněných `accept` z normalize) se
  převezmou doslovně a pending normalize položky přežijí u knih, které
  analyzer sám znovu nevyflaguje (`finish()` převezme nezpracované
  priory). Jen když analyze tutéž knihu znovu vyflaguje, PENDING položka
  se vybuduje znovu (normalize klíče v návrhu vypadnou) — opětovné
  spuštění normalize je znovu navrhne.
- Přejmenování autora mění cílovou cestu umístění: `bmf apply` přesune
  tyto složky do složky kanonického autora, takže práci zakončete
  `bmf abs-rescan --apply` (ABS má vlastní databázi). Přejmenování série
  složky NEpřesouvá — ale přepisuje metadata.json, takže závěrečné
  `bmf abs-rescan --apply` pokryje i je.
- Příkaz je idempotentní: jakmile jsou kanonické hodnoty na disku,
  další běh nenajde co navrhovat.
