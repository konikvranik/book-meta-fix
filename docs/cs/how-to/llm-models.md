[English](../../how-to/llm-models.md) | **Čeština**

# Výběr LLM modelu

Na těžkých CZ/SK knihách bylo změřeno pět nastavení
(`scripts/llm_experiment.py`):

| Varianta | ok% | výst. tokeny | reasoning | Cena |
|---|---|---|---|---|
| **glm-5.2 reasoning_effort=low (výchozí)** | 100% | 346 | ano | 1.40 / 4.40 |
| glm-4.6 thinking=disabled | 100% | 139 | ne | 0.60 / 2.20 |
| glm-4.5-air thinking=disabled | 100% | 122 | ne | 0.20 / 1.10 |
| glm-4.5-flash | 100% | 96 | ne | zdarma |
| glm-4.7-flash | 100% | 147 | ne | zdarma |

Modely bez reasoning používají 3–4× méně tokenů, ale na CZ/SK sériích
a diakritice halucinují více. GLM-5.2 `low` je výchozí; přepínejte, jen
když víte, co děláte.
