[English](../../how-to/llm.md) | **Čeština**

# Běh s LLM fallbackem

```bash
bmf analyze --llm -o review.yaml                       # default loop: Flash→final
bmf analyze --llm --llm-model glm-4.6                  # cheaper, weaker CZ
bmf analyze --llm --no-llm-loop --llm-model glm-4.5-flash   # single cheap call
bmf analyze --llm --llm-reasoning-effort max           # slow/costly, hard batch
```

LLM je **poslední možnost** — deterministické stupně (dolování textu,
online vyhledávání) běží první a LLM vidí jen knihy, které minuly, *a*
zároveň mají použitelný text první strany. Výstupem je vždy *návrh* pro
`review.yaml`, nikdy se neaplikuje automaticky. Viz
[concepts.md → Sebekorekční smyčka LLM](../concepts.md#sebekorekční-smyčka-llm).

## Ladění omezení rychlosti LLM

Coding plan Z.AI vynucuje dynamický limit počtu požadavků za minutu; 429
`Rate limit reached for requests` (kód 1302) se spustí, když v posuvném
okně přistane příliš mnoho volání. Pod limitem vás drží dvě vrstvy (viz
[architecture.md → Model souběžnosti](../architecture.md#model-souběžnosti)):

1. **Vyhlazovač typu leaky bucket** — konstantní agregované RPM.
2. **Globální 429 cooldown** — free tier Z.AI kaskádově škrtí *každý*
   model, jakmile jeden dostane 429, takže když *kterýkoli* worker uvidí
   429, pozastaví se *všechny* workery.

| Parametr | CLI | Env | Výchozí |
|---|---|---|---|
| Stálý interval (s) mezi voláními | `--llm-min-interval` | `BMF_LLM_MIN_INTERVAL` | `2.0` (~30 RPM) |
| Kapacita burstu (volání na interval) | `--llm-burst` | `BMF_LLM_BURST` | `1` (rovnoměrné kapání) |
| Základní cooldown 429 (s) | `--llm-rate-limit-base` | `BMF_LLM_RATE_LIMIT_BASE` | `5` |
| Strop maximálního cooldownu 429 (s) | `--llm-rate-limit-max` | `BMF_LLM_RATE_LIMIT_MAX` | `60` |

Leaky bucket je omezovač typu **počet za čas**, ne strop souběžnosti. S
výchozím `--llm-burst 1` jde o čisté rovnoměrné kapání — přesně jedno
volání začne každých `--llm-min-interval` sekund, rovnoměrně rozložená,
bez hromadění (5 volání v jedné sekundě a pak nic je přesně to, co limit
vyšlape). Burst >1 dovolí, aby ve stejné sekundě startovalo několik volání;
zvyšte jej jen s potvrzenou rezervou v limitu.

Cooldown eskaluje `base * 2^(n-1)` (5, 10, 20, …) s po sobě jdoucími 429,
respektuje serverové `Retry-After`, je-li delší, a je zastropován na
`max`.

**Pokud stále narážíte na 429** (v logu uvidíte `Z.AI rate-limited (429);
global cooldown …s across all workers`), zpomalte kapání a prodlužte
cooldown — burst už je ve výchozím nastavení 1:

```bash
# Slower drip: 4s apart (15 RPM), longer cooldown
bmf analyze --llm --llm-min-interval 4.0 --llm-rate-limit-base 10

# Slow it down hard for a free tier
bmf analyze --llm --llm-min-interval 4.0 --llm-rate-limit-base 15 --llm-rate-limit-max 120
```

**Máte-li vyšší tier a chcete rychlost**, snižte interval a základ
cooldownu:

```bash
bmf analyze --llm --llm-min-interval 1.0 --llm-rate-limit-base 3
```

Leaky bucket je **odpojen od `--workers`**: levné I/O (extrakce,
obohacení) stále běží s plným počtem workerů; vyhlazují se jen volání LLM.

Viz také [výběr LLM modelu](llm-models.md).
