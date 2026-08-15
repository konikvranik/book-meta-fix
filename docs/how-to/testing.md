# Running the tests

```bash
make test                         # full suite
.venv/bin/pytest tests/test_llm_rate_limit.py -q   # one module
.venv/bin/pytest -k cooldown                       # by name
make lint                         # ruff check src tests
```

Tests are per-module (`tests/test_<module>.py`) and use no network — online
sources, the LLM, and HTTP are all stubbed/mocked.
