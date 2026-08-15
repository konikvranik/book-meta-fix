.PHONY: help venv install dev-install clean lint test scan report verify apply

PY ?= python3
VENV := .venv
BIN := $(VENV)/bin

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

$(VENV):
	$(PY) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip

venv: $(VENV)  ## Create virtual environment

install: venv  ## Install package (runtime deps only)
	$(BIN)/pip install -e .

dev-install: venv  ## Install package + dev/test/optional deps
	$(BIN)/pip install -e ".[pdf,llm,dev]"

lint:  ## Run ruff
	$(BIN)/ruff check src tests

test:  ## Run tests
	$(BIN)/pytest

# BMF defaults; override via env: make scan LIBRARY=/other/path
LIBRARY ?= $(HOME)/Books

scan:  ## Scan library and print stats
	$(BIN)/bmf scan --library "$(LIBRARY)"

report:  ## Run detectors and print category counts
	$(BIN)/bmf report --library "$(LIBRARY)"

verify:  ## Verify OK books against content
	$(BIN)/bmf verify --library "$(LIBRARY)"

apply:  ## Apply changes from review.yaml (dry-run by default)
	$(BIN)/bmf apply review.yaml

clean:  ## Remove venv, caches, build artifacts
	rm -rf $(VENV) .pytest_cache .ruff_cache build *.egg-info dist
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
