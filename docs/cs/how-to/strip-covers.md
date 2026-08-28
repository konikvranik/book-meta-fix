[English](../../how-to/strip-covers.md) | **Čeština**

# Odstranění vygenerovaných obálek

```bash
bmf strip-covers                   # dry-run: vypíše knihy s vygenerovanými obálkami
bmf strip-covers --apply           # odstraní je (cover.jpg -> .bak, embedded obálky EPUB vysoupány)
```

Odstraní každou obálku, kterou pixelová analýza C11 klasifikuje jako
automaticky vygenerovanou (placeholder z Calibre), u každé knihy:

- **soubor `cover.jpg`** — přejmenuje se na `cover.jpg.bak` (vratné; existující
  `.bak` se přepíše), nikdy se natvrdo nemaže.
- **embedded obálka EPUB** — najde se přes OPF wiring a když je vygenerovaná,
  chirurgicky se vysoupne (přepis zipu + OPF; e-kniha sama zůstává).
- **ostatní formáty (MOBI/AZW3/PRC, PDF)** — záměrně se netknou: jejich obálky
  žijí v binárních EXTH hlavičkách bez bezpečné cesty ven.

Po zápisovém běhu další `bmf analyze` uvidí `MISSING_COVER` (chybí
`cover.jpg`) a doplní skutečnou obálku — z URL enricheru, nebo extrakcí
skutečné embedded obálky z e-knihy. To je zamýšlený follow-up workflow:
vysoupejte placeholdery, pipeline dočte skutečné obálky.
