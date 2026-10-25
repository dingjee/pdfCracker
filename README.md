# PDFCracker

An adaptive, code-first PDF-to-Markdown workflow built as a **meta-tool**: instead of
hard-coding one parsing strategy, it helps an AI agent fingerprint each new PDF, calibrate
a document-specific extraction profile, and gate full-book processing behind human review —
on top of a reusable, fully tested engine.

## Why not just use a parser or a skill?

- **Versus a pure parsing algorithm/library** (pdftext, Marker, Docling, pymupdf4llm):
  those apply fixed strategies to whatever PDF they receive. PDFCracker wraps them with
  document adaptation — layout clustering, per-document thresholds, confidence scoring —
  and adds what converters lack: complete provenance, reversible deletions, cross-page
  merge decisions with recorded reasons, and an audited intermediate (`document.json`)
  from which Markdown is only one rendering.
- **Versus an LLM skill or prompt-driven conversion**: a skill tells a model to improvise;
  output varies run to run and cannot be verified. PDFCracker encodes the decisions as
  deterministic, regression-tested code. Vision models are called only for unresolved
  ambiguity (low confidence, parser disagreement), return short structured JSON, and are
  logged with cost and latency. The same input yields the same output.
- **The method in one line**: extract everything deterministically from PDF objects
  (geometry, fonts, colors, vector lines), measure confidence on every decision, escalate
  the smallest possible ambiguous region to a vision model, and never publish without an
  audit pass and a human-reviewed pilot.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/dingjee/pdfCracker.git
cd pdfCracker
uv sync --locked
```

Set `OPENROUTER_API_KEY` in a local `.env` (copy `.env.example`) only if you use the
`vision-benchmark` command. Everything else runs offline.

## Usage

Put source PDFs under `inputs/` (never committed). Extract a pilot — page numbers are
1-based:

```bash
uv run pdfcracker extract "inputs/book.pdf" --pages 3-20 --output "runs/book/pilot"
```

Or run the full staged workflow on a bounded slice; `process` writes to a hidden staging
directory, extracts embedded image objects, builds a risk-page plan, runs a graded audit,
and promotes the result only when no integrity error is present:

```bash
uv run pdfcracker process "inputs/book.pdf" --pages 3-20,37-38 \
  --output "runs/book/pilot-v2" --keep-review-renders
```

Omit `--pages` for full-document coverage (guarded by the audit gate). Other commands:

| Command | Purpose |
|---|---|
| `render` | Render selected pages to PNG for visual QA (default 96 DPI) |
| `style-probe` | Inspect native fonts, colors, vector lines, duplicate glyphs — no vision |
| `audit` | Re-audit a `document.json` + Markdown pair against its artifacts |
| `package` | Bundle accepted Markdown + images into a verified ZIP / self-contained HTML |
| `vision-benchmark` | Score vision providers against gold data via OpenRouter |

Apply reviewed, document-specific decisions with `--profile` (kept out of the reusable
core). Re-run audits with `--require-full`; only fundamental integrity failures block
publication, ordinary fidelity uncertainty is reported for targeted review.

## What the pipeline guarantees

- Page/block/span provenance for every deletion, style transformation, paragraph join,
  and cross-page merge.
- Inline semantics as orthogonal flags (bold, italic, underline, semantic color) — never
  flattened; Markdown stays readable, exact presentation is deferred to later EPUB CSS.
- Embedded image objects only; no page rasterization fallback. Decoration/ambiguity is
  flagged for review, not silently inserted.
- Audits that check delivered Markdown content, table cells, image references, and asset
  hashes — not just the intermediate.
- OCR and vision are escalations, never the default for a healthy text layer.

See [RESEARCH_AND_TEST_PLAN.md](RESEARCH_AND_TEST_PLAN.md) for the full architecture,
benchmarks, and phased plan, and [knowledge/](knowledge/) for promoted extraction rules.

## Repository layout

```
src/pdfcracker/      reusable engine (extraction, styles, audit, delivery, vision)
tests/               unit tests + golden tests (skip without a local regression PDF)
knowledge/           promoted, evidence-backed rules and workflow lessons
research/            one-off study scripts; generated evidence stays ignored
inputs/              your source PDFs (ignored — never enter version control)
runs/<document-id>/  per-document profiles, pilots, outputs (ignored)
```

## Development

```bash
uv run python -m pytest
uv run python -m ruff check .
```

Golden tests look for their named regression PDF under `inputs/` and skip cleanly when it
is absent, so cloning this repo runs the full suite with no proprietary files.

## Security notes

- Source PDFs, extracted content, and generated evidence are never committed.
- API credentials live only in a local `.env`; code reads them from the environment.
- Image references are validated against path escapes; packaged archives are CRC- and
  link-verified before publication.

## License

[MIT](LICENSE)
