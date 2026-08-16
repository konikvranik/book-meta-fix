# Choosing an LLM model

**English** | [Čeština](../cs/how-to/llm-models.md)

Five settings were measured on hard CZ/SK books (`scripts/llm_experiment.py`):

| Variant | ok% | out tok | reasoning | Cost |
|---|---|---|---|---|
| **glm-5.2 reasoning_effort=low (default)** | 100% | 346 | yes | 1.40 / 4.40 |
| glm-4.6 thinking=disabled | 100% | 139 | no | 0.60 / 2.20 |
| glm-4.5-air thinking=disabled | 100% | 122 | no | 0.20 / 1.10 |
| glm-4.5-flash | 100% | 96 | no | free |
| glm-4.7-flash | 100% | 147 | no | free |

Non-reasoning models use 3–4× fewer tokens but hallucinate more on CZ/SK series
and diacritics. GLM-5.2 `low` is the default; switch when you know what you're
doing.
