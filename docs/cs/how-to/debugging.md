[English](../../how-to/debugging.md) | **Čeština**

# Ladění běhu

- **Zachráněno nevalidní JSON** — pokud vidíte `LLM JSON salvaged via json-repair
  (unescaped quotes/control chars fixed)`, model vrátil mírně poškozený
  JSON a ten byl zachráněn. Není potřeba žádná akce; toto nahrazuje dřívější
  plýtvání 3 opakováními. Pokud vidíte `LLM returned invalid JSON` (bez řádku
  o záchraně), nainstalujte extra `[llm]` (`json-repair`).
- **Rate limit** — `Z.AI rate-limited (429); global cooldown Xs` znamená, že
  jistič (circuit breaker) dělá svou práci. Časté výskyty znamenají, že
  máte zvýšit hodnoty v [ladění omezení rychlosti LLM](llm.md#ladění-omezení-rychlosti-llm).
- **Kam se poděla moje revize?** — `review.yaml.bak` obsahuje stav před
  spuštěním, pokud byl běh přerušen; pro obnovu jej přejmenujte zpět.
- **Kniha nedostala návrh** — pravděpodobně není k dispozici použitelný text
  první strany (LLM se přeskočí) nebo všechy stupně kaskády nic nenašly.
  Zkuste `--verify-ok`, aby se auditovaly i knihy, které strukturální
  detektory označily jako OK.
- **Podrobné logy** — `bmf -v analyze ...` zapne debug logování.
