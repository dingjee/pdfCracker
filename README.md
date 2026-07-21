# PDFCracker

PDFCracker is an adaptive, code-first PDF-to-Markdown workflow. It preserves
page/block/span provenance, classifies layout before using vision, and produces
a bounded pilot for human review before full-book processing.

## Development

```powershell
uv sync --locked
uv run pytest
uv run ruff check .
```

## Repository boundary

This repository contains only reusable engineering assets: source code, tests,
configuration templates, scripts, and documentation. Local source documents
belong in `puzzle/`; per-document intermediates and final outputs belong in
`runs/`. Generated benchmark data, screenshots, and document-derived gold data
stay under their ignored `research/` directories. None of these inputs or
outputs are versioned.

Copy `.env.example` to `.env` for local API credentials. Tests that require a
local regression PDF are skipped when `puzzle/` is absent.

Extract a pilot (page numbers are 1-based):

```powershell
uv run pdfcracker extract "puzzle/book.pdf" --pages 3-20 --output "runs/book/pilot"
```

Render diagnostic pages:

```powershell
uv run pdfcracker render "puzzle/book.pdf" --pages 3,15,37-38 --dpi 96 --output "runs/book/qa"
```

Inspect native style evidence without vision:

```powershell
uv run pdfcracker style-probe "puzzle/book.pdf" --pages 3,15 --output "runs/book/qa/styles.json"
```
