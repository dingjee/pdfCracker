# PDFCracker Agent Guidelines

## Mission

Build PDFCracker as a meta-tool that helps an AI agent create, evaluate, and refine document-specific PDF parsing workflows while preserving a reusable, tested core.

The primary intermediate outputs are a provenance-rich `document.json` and a simple, readable Markdown document. EPUB is a downstream format produced only after Markdown quality is accepted.

## Durable Principles

- Prefer deterministic PDF objects, geometry, fonts, colors, vector lines, statistics, and tested code over OCR or vision-model judgment.
- Use OCR only when the native text layer is absent, corrupt, duplicated, or demonstrably worse.
- Use vision models for unresolved layout ambiguity, parser disagreement, and visual verification. Do not use them to transcribe an entire clean text-layer PDF.
- Preserve provenance for every deletion, style transformation, paragraph join, and cross-page merge.
- Model inline semantics as orthogonal flags (bold, italic, underline, semantic color, links, and roles); never flatten nested combinations. Preserve meaning in Markdown/HTML and defer exact appearance to EPUB CSS.
- Treat accidental body-text deletion and incorrect cross-page merges as release-blocking defects.
- Keep the readable Markdown simple. Preserve detailed layout and style information in the structured sidecar.
- Optimize correctness per dollar, not model prestige. Benchmark providers with the same gold data and structured-output schema.
- Process a bounded pilot slice and obtain human review before processing a full new book.

## Reusable Core vs. Per-Document Adaptation

Keep these concerns separate:

- Reusable engine code, schemas, metrics, parser adapters, renderers, and generic rules belong under `src/`, `tests/`, and `knowledge/`.
- Document-specific thresholds, page clusters, temporary prompts, exceptions, and review decisions belong under `runs/<document-id>/`.
- Do not promote a document-specific workaround into the reusable core without a regression test and evidence that it generalizes.
- Do not put document titles, page numbers, one-off layout facts, or temporary thresholds in this file.

## Code-First Classification

Before using a model, attempt to identify the following from PDF objects and geometry:

- table of contents entries from dot leaders, aligned page numbers, indentation, links, and outline targets;
- titles and headings from relative font size, weight, spacing, position, numbering, and bookmarks;
- body text from the dominant per-layout-cluster typography and paragraph flow;
- running headers, footers, and page numbers from section-aware repetition and stable position;
- bold and italic from font metadata plus duplicate-glyph handling;
- underline from horizontal vector geometry near the text baseline;
- semantic color from text fill/stroke colors and color clustering.

Every classifier should return a label, confidence, and evidence. Route only ambiguous or conflicting cases to visual review.

## Pilot-First Workflow

For every substantially new PDF:

1. Inspect and fingerprint the document.
2. Build page/layout clusters and detect a table of contents or section boundaries.
3. Select a coherent pilot: front matter or TOC plus the first chapter when available.
4. Enforce configured page, word, image, vision-call, and API-cost caps. If the book has no chapters, use a bounded first-body section plus coverage-driven diagnostic pages.
5. Run the complete extraction, cleanup, paragraph reconstruction, Markdown generation, and QA workflow on the pilot.
6. Produce human-review artifacts and record corrections as structured decisions.
7. Add regression tests for accepted generalized fixes.
8. Start a full-book run only after the pilot gate passes.

## Visual Model Policy

- Use a champion/challenger system. Do not call every provider on every page.
- Run all candidate models only on the controlled benchmark set.
- In production, call the current champion first; call a challenger only for low confidence, high severity, parser disagreement, or a small shadow sample.
- Use a third model or human review only to resolve remaining disagreement.
- Require short structured JSON output and cap output tokens.
- Log provider, exact model ID, media/detail setting, image dimensions, crop, input/output tokens, latency, retries, and cost.
- Treat model-reported confidence/readability as telemetry, not acceptance; score exact fields against human gold data.
- Preserve generation ID, usage, cost, and raw response evidence even when structured-output parsing fails.
- Pin stable model versions where available and rerun the benchmark before changing the champion.

## Python and Dependencies

- Manage Python exclusively with `uv`.
- Maintain `pyproject.toml` and `uv.lock`; use `uv sync --locked` for reproducible environments.
- Use optional dependency groups for heavy parsers, OCR, local VLMs, and provider SDKs.
- Do not use system Python or ad hoc `pip install` commands.
- For one-off research before project initialization, use isolated `uv run --with ...` commands and preserve reproducible scripts/results under `research/`.

## Artifacts and Safety

- Never overwrite the source PDF.
- Use content hashes and stable run IDs.
- Keep raw parser output and reversible tombstones for removed headers, footers, page numbers, and duplicate text.
- Store reusable outputs in documented artifact directories and remove unneeded temporary renders.
- Keep API credentials out of source files, prompts, logs, fixtures, and artifacts.

## Verification

Relevant changes must be verified at the smallest sufficient scope and then against affected golden documents.

Quality gates include:

- zero known body-text deletions;
- cross-page merge precision at or above the documented target;
- correct reading order and paragraph boundaries on the gold set;
- no unresolved high-severity visual issues;
- complete provenance for transformations;
- budget and usage telemetry present;
- pilot approval before full-book processing;
- EPUBCheck with zero errors once EPUB generation is enabled.

## Maintaining This File

Keep this file concise and durable. Update it only when:

- the same agent mistake or workflow friction occurs at least twice; or
- a new project-wide invariant, command, or quality gate has been established.

Before editing, write a short retrospective, add or update the relevant regression test, and keep document-specific details in the run profile instead. If this file grows large, move task-specific procedures to focused documentation and link them from here.

The detailed architecture and phased implementation plan live in `RESEARCH_AND_TEST_PLAN.md`.
