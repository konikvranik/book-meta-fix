.PHONY: help venv install dev-install clean lint test scan report verify apply i18n-extract i18n-compile docker-build docker-build-multi

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

# --- i18n -------------------------------------------------------------------
# Extract/update/compile the translation catalogs (gettext, English msgids,
# cs catalog). The compiled .mo files are committed, so a plain install or
# test run never needs pybabel — only these targets do.
LOCALES_DIR := src/book_meta_fix/locales

i18n-extract:  ## Update the .po catalogs from source strings (needs pybabel)
	pybabel extract -F babel.cfg -k _ -o $(LOCALES_DIR)/bmf.pot src/book_meta_fix
	@test -f $(LOCALES_DIR)/cs/LC_MESSAGES/bmf.po || pybabel init -i $(LOCALES_DIR)/bmf.pot -d $(LOCALES_DIR) -D bmf -l cs
	pybabel update -i $(LOCALES_DIR)/bmf.pot -d $(LOCALES_DIR) -D bmf

i18n-compile:  ## Compile .po -> .mo (needs pybabel or msgfmt)
	pybabel compile -d $(LOCALES_DIR) -D bmf

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

# --- docker ------------------------------------------------------------------
# Local single-arch image
docker-build:  ## Build the local docker image (native arch)
	docker build -t bmf .

# Multi-arch manifest (amd64 + arm64 + arm/v7); needs buildx + binfmt for
# foreign architectures (docker run --privileged --rm tonistiigi/binfmt --install all)
docker-build-multi:  ## Build multi-arch image (linux/amd64,linux/arm64,linux/arm/v7)
	docker buildx build --platform linux/amd64,linux/arm64,linux/arm/v7 -t bmf:multi .

clean:  ## Remove venv, caches, build artifacts
	rm -rf $(VENV) .pytest_cache .ruff_cache build *.egg-info dist
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
