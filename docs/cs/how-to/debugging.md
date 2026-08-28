[English](../../how-to/debugging.md) | **Čeština**

# Ladění běhu

- **Zachráněno nevalidní JSON** — pokud vidíte `LLM JSON salvaged via json-repair
  (unescaped quotes/control chars fixed)`, model vrátil mírně poškozený
  JSON a ten byl zachráněn. Není potřeba žádná akce; toto nahrazuje dřívější
  plýtvání 3 opakováními. Pokud vidíte `LLM returned invalid JSON` (bez řádku
  o záchraně), nainstalujte extra `[llm]` (`json-repair`).
- **Rate limit** — `Z.AI rate-limited (429/1302 …); global cooldown Xs`
  znamená, že jistič (circuit breaker) dělá svou práci. Časté výskyty
  znamenají, že máte zvýšit hodnoty v
  [ladění omezení rychlosti LLM](llm.md#ladění-omezení-rychlosti-llm).
- **Přetížení serveru** — `Z.AI service overloaded (429/1305); retrying …`
  znamená, že serverům Z.AI (obvykle bezplatný flash model) došla kapacita —
  ne že byste posílali příliš rychle. Zpomalení nepomůže; běh to přežívá
  retry a přepnutím na placený model. `pausing model … for 180s` po
  fleet-wide sérii odmítnutí je tentýž příběh: pool je nasycený, takže běh
  model krátce zaparkuje a každý rate slot jde modelu, který odpovídá.
  Časté večery? Viz
  [ladění omezení rychlosti LLM](llm.md#ladění-omezení-rychlosti-llm).
- **Vyčerpaná kvóta** — `Z.AI usage limit reached (429/1308 …)` vypne
  dotčený model do konce běhu; kvóta se obnoví na straně Z.AI (hodiny).
- **Nedostatečný balance** — `Z.AI insufficient balance (429/1113 …)` znamená, že
  billingová kontrola odmítl. Na coding endpointu obvykle krátký burst (i se
  zbývající kvótou), který sleduje account-throttling při flash bouřích;
  přežije se retry a model se krátce (~30 s) pozastaví. Trvá-li, prověřte
  balance plánu a poznámku o spárování `ZAI_BASE_URL` v `.env.example`.
- **LLM idle** — `every model is paused right now` znamená, že není dostupný
  žádný model (flash ve vlně 1305, fallback v burstu 1113): knihy protéčejí
  BEZ LLM návrhů, dokud některá pauza nevyprší (~30 s–3 min). Opakuje-li se
  to ve špičce stále, běžte s `--no-llm-loop` (rovnou placený model) nebo
  mimo špičku.
- **Kam se poděla moje revize?** — `review.yaml.bak` obsahuje stav před
  spuštěním, pokud byl běh přerušen; pro obnovu jej přejmenujte zpět.
- **Kniha nedostala návrh** — pravděpodobně není k dispozici použitelný text
  první strany (LLM se přeskočí) nebo všechy stupně kaskády nic nenašly.
  Zkuste `--verify-ok`, aby se auditovaly i knihy, které strukturální
  detektory označily jako OK.
- **Podrobné logy** — `bmf -v analyze ...` zapne debug logování.
