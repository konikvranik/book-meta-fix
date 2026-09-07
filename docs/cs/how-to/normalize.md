# Sjednocení pravopisů autorů a názvů žánrů

[English](../../how-to/normalize.md) | **Čeština**

```bash
bmf normalize                    # dry-run: vypíše clustery autorů/žánrů
bmf normalize --apply            # naplní review.yaml návrhy C15/C16
bmf normalize --genres --apply   # jen žánry+tagy (bez autorů)
bmf normalize --authors          # jen autoři
bmf gui                          # revize; hromadné potvrzení Ctrl+Shift+A
bmf apply --apply review.yaml    # zápis metadat + přesuny složek
bmf abs-rescan --apply           # dotlačí změny do Audiobookshelf
```

Dva nepořádky jsou pro per-knihové detektory neviditelné, protože se
projeví až MEZI knihami: tatáž osoba zapsaná několika způsoby (C15) a
názvy žánrů lišící se jen velikostí písmen, pořadím slov, jazykem nebo
pravopisem (C16). `bmf normalize` jednou proskenuje knihovnu, naklastruje
varianty a převede je na obyčejné návrhy do review.yaml — výměny celých
seznamů `proposed.authors` / `proposed.genres` / `proposed.tags`, které
`bmf apply` zapíše jako každou jinou opravu.

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

## Co zůstane pending (váš úsudek)

- **Varianty autorů lišící se písmeny** — „Frederik"/„Frederick" Pohl,
  „Miloslav"/„Miroslav" Švandrlík, mojibake kopie („JiĹ™Ă Kosek").
  Stejné iniciály může mít i dva různí lidé, proto tyto návrhy přicházejí
  bez předvyplněné akce.
- **Nedoložené čárkové formy** — izolované „Weis, Hickman" mohou být dva
  autoři spojení čárkou, ne jedno prohozené jméno.

Různé iniciály křestních jmen se nikdy nesloučí (Karel vs Josef Čapek,
Dan vs Eric Brown zůstávají oddělení). Multi-author řetězce („Wilhelm a
Jacob Grimmové") se přeskočí a vypíší zvlášť — jejich štěpení je území
C7/C8.

## Přidání sloučení žánrů

Fuzzy automatické porovnávání žánrů zde záměrně CHYBÍ: na 1 621 názvech
reálné knihovny byly „překlepy" na vzdálenosti 2 z ~50 % falešné
(Afrika→Amerika, vlaky→války, etika→erotika). Chcete-li dvě podoby
sloučit, přidejte řádek do `GENRE_ALIASES` a spusťte znovu — každé sloučení
je explicitní řádek ke kontrole.

## Poznámky k pracovnímu postupu

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
  `bmf abs-rescan --apply` (ABS má vlastní databázi).
- Příkaz je idempotentní: jakmile jsou kanonické hodnoty na disku,
  další běh nenajde co navrhovat.
