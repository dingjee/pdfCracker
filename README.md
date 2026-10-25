# PDFCracker

PDFCracker is an adaptive, code-first PDF-to-Markdown workflow. It preserves
page/block/span provenance, classifies layout before using vision, and produces
a bounded pilot for human review before full-book processing.

## Development

```powershell
uv sync --locked
uv run python -m pytest
uv run python -m ruff check .
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

Run the staged workflow on a bounded pilot:

```powershell
uv run pdfcracker process "puzzle/book.pdf" `
  --pages 3-20,37-38 `
  --output "runs/book/pilot-v2" `
  --keep-review-renders
```

Apply reviewed, document-specific decisions without promoting them into the
reusable core:

```powershell
uv run pdfcracker process "puzzle/book.pdf" `
  --output "runs/book/full-v2" `
  --profile "runs/book/profile-v1.json"
```

Use `--keep-cache` during iterative calibration when another run is expected.
Final successful runs clean the content-addressed cache by default.

Omit `--pages` for full-document coverage. `process` writes to a hidden staging
directory, extracts embedded PDF image objects, builds a risk-page plan, runs the
graded audit, and promotes the result only when no fundamental integrity error is
present. Successful runs remove their content-addressed raw-parser cache.

The current image scope is deliberately narrow: PDFCracker extracts only embedded
image objects. It does not rasterize whole pages or arbitrary regions as a fallback.
Every decoded object is retained in the manifest; only evidence-backed content images
are inserted into Markdown. Decorations and ambiguous objects remain auditable.

Re-run an audit from saved artifacts:

```powershell
uv run pdfcracker audit "runs/book/full/document.json" `
  "runs/book/full/book.md" `
  --artifact-root "runs/book/full" `
  --output "runs/book/full/qa/audit.json" `
  --require-full
```

Audit levels are `fatal`, `needs_review`, `warning`, and `information`. Only
fundamental integrity failures block publication; ordinary fidelity uncertainty is
reported for targeted review.

Package accepted Markdown with its image files (no `document.json` required):

```powershell
uv run pdfcracker package "runs/book/full/book.md" `
  --artifact-root "runs/book/full" `
  --output "runs/book/delivery" `
  --self-contained
```

The ZIP preserves paths relative to `--artifact-root`, so Markdown in a nested
directory can still reference sibling assets. It includes a browser reading
version of the first Markdown input. `--self-contained` also writes one HTML
file with original image bytes embedded. Multiple Markdown inputs and repeated
`--include path/to/file.txt` options can share one package. The output directory
must be new. All references, image decoding, archive CRC, and embedded image hashes
are checked before the delivery directory is published. Images must be local,
inside the declared root, and browser compatible for HTML delivery.

Audits check the delivered Markdown, including body text, materialized table
cells, empty/missing image destinations, and asset hashes. CLI image references
resolve from the supplied Markdown file's directory rather than the sidecar's
directory. These checks detect missing content; they do not prove reading order
or cross-page merge precision.

See [extraction and delivery lessons](knowledge/workflows/native-extraction-and-delivery.md)
for the acceptance method, common false passes, and retention policy. Local golden
tests select their named regression PDF in `puzzle/` or `puzzle/achieved/`; adding
another PDF must not silently change the regression source.
