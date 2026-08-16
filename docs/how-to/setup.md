# Setup

**English** | [Čeština](../cs/how-to/setup.md)

```bash
make dev-install                  # create .venv, install package + dev + pdf + llm extras
cp .env.example .env              # then edit .env: set BMF_LIBRARY, ZAI_API_KEY, ...
```

`make dev-install` installs `pip install -e ".[pdf,llm,dev]"`. The `[llm]`
extra pulls `openai` (the Z.AI client) and `json-repair` (salvages invalid
LLM JSON). Without it, `--llm` is unavailable and the LLM JSON salvage falls
back to the built-in (weaker) repair.

External tools are optional but extend coverage: `pdftotext`/`pdfinfo`
(poppler) for PDFs, `ebook-convert`/`ebook-meta` (calibre) for EPUB
generation, `pandoc` for txt/doc, `tesseract` for OCR of scanned PDFs.
