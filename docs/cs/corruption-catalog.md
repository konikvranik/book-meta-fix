[English](../corruption-catalog.md) | **Čeština**

# Katalog poškození (C1–C10)

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

## C8 — Překladatel označený jako autor

Překladatelé jsou kódováni jako druhý `<dc:creator opf:role="aut">` — role
`trl` se v této knihovně nikdy nepoužívá. Jediný spolehlivý signál: česky
ypadající jméno po boku zahraničního autora.

| id | název | autoři | pravděpodobný překladatel |
|----|---|---|---|
| 393 | Měsíční prach | Jarmila Emmerová, Arthur C. Clarke | Emmerová |
| 499 | Hrobky Atuánu | Karel Soukup, Petr Kotrle, Ursula K. Le Guin | Soukup, Kotrle |
| 2144 | Smrt lorda Edgwarea | Marek Roesel, Agatha Christie | Roesel |

**Verdikt:** NEEDS_REVIEW
**Výhrada:** při 4+ autorech a 2+ zahraničních jménech je to spíš skutečná
antologie (C10) než překladatelský tým.

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

## C10 — Dlouhý seznam autorů

4+ autoři — může jít o skutečnou antologii NEBO překladatelský tým. Rozlišit
to nejde.

| id | název | počet autorů |
|----|---|---|
| 197 | Soumrak světů | 13 (skutečná CZ SF antologie) |
| 4411 | Kuchařka stařenky Oggové | 4 (Briggs, Pratchett, Kantůrek, Kidby) |

**Verdikt:** NEEDS_REVIEW

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
