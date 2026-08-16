[English](../../how-to/testing.md) | **Čeština**

# Spuštění testů

```bash
make test                         # full suite
.venv/bin/pytest tests/test_llm_rate_limit.py -q   # one module
.venv/bin/pytest -k cooldown                       # by name
make lint                         # ruff check src tests
```

Testy jsou per modul (`tests/test_<module>.py`) a nepoužívají síť — online
zdroje, LLM i HTTP jsou stubované/mockované.
