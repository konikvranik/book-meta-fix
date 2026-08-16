# Configuration

**English** | [Čeština](../cs/how-to/configuration.md)

Settings resolve (highest precedence first): **CLI flag → env var → `.env`
file (walked up from CWD) → built-in default**. Copy `.env.example` to `.env`
and edit. Key variables: `BMF_LIBRARY`, `BMF_CACHE`, `ZAI_API_KEY`,
`ZAI_BASE_URL` (coding plan vs PaaS — see `.env.example`),
`BMF_LLM_MODEL`, `BMF_LLM_FALLBACK_MODEL`,
`BMF_LLM_MIN_INTERVAL`, `BMF_LLM_BURST`, `BMF_LLM_RATE_LIMIT_BASE`,
`BMF_LLM_RATE_LIMIT_MAX`.
