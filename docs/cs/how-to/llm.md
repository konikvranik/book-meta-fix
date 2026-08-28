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

Z.AI vrací HTTP 429 pro tři různé situace a `bmf` na každou reaguje jinak
(sub-kód se vždy zaloguje):

| Sub-kód | Význam | Reakce `bmf` |
|---|---|---|
| `1302` Rate limit reached for requests | **Vaše** rychlost požadavků vyšlapala RPM okno | Globální cooldown: pozastaví se všichni workeri, eskaluje `base * 2^(n-1)` (5, 10, 20, …), respektuje serverové `Retry-After`, je-li delší, strop `max` |
| `1305` The service may be temporarily overloaded | **Kapacita serveru** Z.AI (chronické u bezplatných flash modelů — nezpůsobeno vámi) | Krátké retry s rozestupem intervalu (bez globálního cooldownu), pak přepnutí na placený model; fleet-wide série po sobě jdoucích odmítnutí pozastaví model na ~3 min |
| `1113` Insufficient balance or no resource package | Billingová kontrola — tvrdá chyba, když kvóta opravdu došla, na coding endpointu ale vystřeluje i při zbývající kvótě, když se překročí ~5souběžný strop požadavků na účet (fallback stádo bouří na placený model najednou) | Strop souběžných volání bouře předchází; zbytky dostanou tranzientní zacházení (streak) jako 1305 s krátkou pauzou ~30 s. Přetrvává-li i při `--llm-max-inflight 1`, prověřte plán / spárování endpointu v `.env.example` |
| `1308` Usage limit reached | Vyčerpaná kvóta usage modelu | Model se do konce běhu přeskočí |

Pod limitem 1302 vás drží tři vrstvy (viz
[architecture.md → Model souběžnosti](../architecture.md#model-souběžnosti)):

1. **Vyhlazovač typu leaky bucket** — konstantní agregované RPM.
2. **Strop souběžných volání** — coding plan Z.AI připouští jen **~5
   souběžných požadavků na účet** (Z.AI přesná čísla nezveřejňuje: limity
   jsou tierové a dynamické podle usage policy Devpacku; změřeno: 12hluboký
   současný spike dostal 7× 429/1302, zatímco 6hluboký burst prošel, a
   interaktivní klienti — ZCode/chat — čerpají ze stejného stropu). Bucket
   rozestupuje *starty* volání, ne jejich *hloubku*: s 10 workery a
   vícesekundovými reasoning voláními může fallback stádo strop překročit
   a bouře se projeví i jako falešné `1113` „insufficient balance" při
   jasně zbývající kvótě (vlastní FAQ Z.AI přiznává 1113 na koupeném
   balíčku). Semafor (`--llm-max-inflight`) capuje, kolik požadavků běží
   najednou — workeři čekají lokálně ve frontě, místo aby je Z.AI odmítal.
   Flash-modely drží přísnější sub-strop `min(2, cap)`: bezplatný pool je
   chronicky nasycený a komunita udává jeho souběžnost i na 1, takže hluboké
   flash stádo jen živí 1305 bouři a vytlačuje placený fallback ze sdílených
   slotů. Při coding-plánovém `ZAI_BASE_URL` se glm-4.x flash navíc routuje
   na **PaaS endpoint** (`ZAI_FLASH_BASE_URL`, prázdné = auto, `off` = vypnout):
   stropy souběžnosti obou endpointů jsou nezávislé (změřeno: 6 flash@PaaS +
   6 glm-5.3@coding současně nechalo coding stranu na jejích obvyklých ~5)
   a flash je tam zdarma — vlastní pool, nula kreditů z plánu. Placené
   modely zůstávají na coding endpointu (na PaaS dávají 1113 — žádný cash
   balance) a glm-5.x flash na PaaS není (400/1210), takže také zůstává.
   Celá rate machinery (drip, cooldown, in-flight brána) je rovněž per
   endpoint: tlak na jednom endpointu nikdy nebrzdí druhý.
   Globální strop je **adaptivní**: externí klient na stejném plánu
   (ZCode, chat) je pro bmf neviditelný, takže tlak na strop se pozoruje,
   nepředpovídá — každé 1302 / falešné 1113 uvolní jeden in-flight slot
   (log: `in-flight cap 3 -> 2`) a ~20 čistých odpovědí si ho vrátí.
   Pod kapotou všechna volání sdílejí jednu sadu pooled keep-alive HTTP
   spojení (keep-alive 60 s přežije 30s balance pauzy, takže po žádné
   nenásleduje zbytečný nový TLS handshake) s read timeoutem 180 s —
   zavěšené volání nemůže obsadit vzácný in-flight slot po SDK výchozích
   600 s.
3. **Globální 429/1302 cooldown** — free tier Z.AI kaskádově škrtí *každý*
   model, jakmile jeden dostane 429, takže když *kterýkoli* worker uvidí
   1302, pozastaví se *všechny* workery.

| Parametr | CLI | Env | Výchozí |
|---|---|---|---|
| Stálý interval (s) mezi voláními | `--llm-min-interval` | `BMF_LLM_MIN_INTERVAL` | `2.0` (~30 RPM) |
| Max současně běžících volání | `--llm-max-inflight` | `BMF_LLM_MAX_INFLIGHT` | `3` (strop účtu ~5, flash sub-strop `min(2, ·)`) |
| Kapacita burstu (volání na interval) | `--llm-burst` | `BMF_LLM_BURST` | `1` (rovnoměrné kapání) |
| Základní cooldown 429 (s) | `--llm-rate-limit-base` | `BMF_LLM_RATE_LIMIT_BASE` | `5` |
| Strop maximálního cooldownu 429 (s) | `--llm-rate-limit-max` | `BMF_LLM_RATE_LIMIT_MAX` | `60` |

Leaky bucket je omezovač typu **počet za čas**, ne strop souběžnosti — k tomu
slouží `--llm-max-inflight`. S
výchozím `--llm-burst 1` jde o čisté rovnoměrné kapání — přesně jedno
volání začne každých `--llm-min-interval` sekund, rovnoměrně rozložená,
bez hromadění (5 volání v jedné sekundě a pak nic je přesně to, co limit
vyšlape). Burst >1 dovolí, aby ve stejné sekundě startovalo několik volání;
zvyšte jej jen s potvrzenou rezervou v limitu.
Interval je **adaptivní**: je jen podlahou. Skutečný strop požadavků Z.AI
se hýbe (jednoho večera naměřeny čtyři 1302 za dvě minuty při klidných
30 RPM), proto 1302/1113 drip roztáhnou (×1.3, strop ~4× podlaha) a každá
úspěšná odpověď ho zase vrátí — během minut se usadí na tom, co účtu
danou chvíli skutečně náleží. Jsou-li jednou pozastaveny všechny modely,
uvidíte jednou za minutu řádek `every model is paused` a knihy protéčejí
bez LLM návrhů, dokud některá pauza nevyprší.

**Pokud stále narážíte na 1302** (v logu uvidíte `Z.AI rate-limited
(429/1302 …); global cooldown …s across all workers`), zpomalte kapání a
prodlužte cooldown — burst už je ve výchozím nastavení 1:

```bash
# Slower drip: 4s apart (15 RPM), longer cooldown
bmf analyze --llm --llm-min-interval 4.0 --llm-rate-limit-base 10

# Slow it down hard for a free tier
bmf analyze --llm --llm-min-interval 4.0 --llm-rate-limit-base 15 --llm-rate-limit-max 120
```

**Pokud je log místo toho plný řádků `429/1305` (overload)**, jde o kapacitu
Z.AI, ne o vaši rychlost — zpomalení nepomůže. Běh to přežívá retry a
přepnutím na placený model a fleet-wide série po sobě jdoucích odmítnutí
zaparkuje přetížený model na ~3 minuty (`pausing model … for 180s`). Pokud to
večer dominuje, přepněte smyčku rovnou na placený model:

```bash
bmf analyze --llm --llm-model glm-5.3
```

**Uvidíte-li `429/1308 usage limit reached`**, kvóta usage modelu je
vyčerpaná, dokud se neobnoví na straně Z.AI (typicky hodiny); `bmf` tento
model automaticky přeskočí do konce běhu.

**Máte-li vyšší tier a chcete rychlost**, snižte interval a základ
cooldownu:

```bash
bmf analyze --llm --llm-min-interval 1.0 --llm-rate-limit-base 3
```

Leaky bucket je **odpojen od `--workers`**: levné I/O (extrakce,
obohacení) stále běží s plným počtem workerů; vyhlazují se jen volání LLM.

Viz také [výběr LLM modelu](llm-models.md).
