[English](../../how-to/setup.md) | **Čeština**

# Instalace a nastavení

```bash
make dev-install                  # create .venv, install package + dev + pdf + llm extras
cp .env.example .env              # then edit .env: set BMF_LIBRARY, ZAI_API_KEY, ...
```

`make dev-install` spustí `pip install -e ".[pdf,llm,dev]"`. Extra `[llm]`
přitáhne `openai` (klient Z.AI) a `json-repair` (zachraňuje nevalidní
LLM JSON). Bez něj není `--llm` k dispozici a záchrana LLM JSON spadne
zpět na vestavěnou (slabší) opravu.

Externí nástroje jsou volitelné, ale rozšiřují pokrytí: `pdftotext`/
`pdfinfo` (poppler) pro PDF, `ebook-convert`/`ebook-meta` (calibre) pro
generování EPUB, `pandoc` pro txt/doc, `tesseract` pro OCR skenovaných
PDF.
