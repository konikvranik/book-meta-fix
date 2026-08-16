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
pro jednotlivé formáty s opravou dvojitého kódování (`Ctrl+G`; boxy na
kodeky umožňují ruční experimentování — „přečteno jako" je chybný kodek,
kterým byl text kdysi přečten, „skutečně je" ten skutečný a `⇄` je
prohazuje; nefungující dvojici vysvětlí nápověda, která nabízí opačný směr
jako kliknutí;
byty ztracené dřívějším dekódováním s replace (`�`) opravu neblokují a
zůstávají označené; dvouvrstvé řetězce se opravují automaticky a nápověda
je
jmenuje; vždy vykresleno jako UTF-8 — přepínač se nikdy nezaškrtává
automaticky, vidět opravený text je vaše rozhodnutí: zaškrtněte jej nebo
stiskněte `Ctrl+G`). Táhněte úchyp pod náhledem obsahu pro svislou změnu
velikosti (dvojklik vrací výchozí). Detailní
sloupec se posouvá; každá akce má zkratku `Ctrl+písmeno` (`F1` je
vypíše); `PgUp`/`PgDn` přecházejí mezi knihami a `Tab` prochází jen
upravitelná
pole — autor, název, ISBN, rok, vydavatel, jazyk, série, pořadí v
sérii, autoři, žánry (`Ctrl+A` vybere v poli vše). Seznam zobrazuje vlevo
štítek a v každém řádku vpravo přilepenou miniaturu obálky. Vyžaduje Tk
bindings
(`sudo apt install python3-tk` na Debianu/Ubuntu). Úpravy se zapisují
zpět do `review.yaml` — potvrďte je příkazem `bmf apply` jako v
[úprava + aplikování](edit-and-apply.md).
