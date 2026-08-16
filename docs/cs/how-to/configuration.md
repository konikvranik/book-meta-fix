[English](../../how-to/configuration.md) | **Čeština**

# Konfigurace

Nastavení se řeší s touto prioritou (nejvyšší první): **CLI přepínač →
proměnná prostředí → soubor `.env` (hledá se od aktuálního pracovního
adresáře směrem vzhůru) → vestavěná výchozí hodnota**. Zkopírujte
`.env.example` jako `.env` a upravte. Klíčové proměnné: `BMF_LIBRARY`,
`BMF_CACHE`, `ZAI_API_KEY`, `ZAI_BASE_URL` (coding plan vs PaaS — viz
`.env.example`), `BMF_LLM_MODEL`, `BMF_LLM_FALLBACK_MODEL`,
`BMF_LLM_MIN_INTERVAL`, `BMF_LLM_BURST`, `BMF_LLM_RATE_LIMIT_BASE`,
`BMF_LLM_RATE_LIMIT_MAX`.
