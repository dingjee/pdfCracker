# Full scanned book EPUB delivery retrospective — 2026-11-10

## Trigger

First full-book scanned delivery (427-page book, local OCR, EPUBCheck-gated
EPUB). Three independent pipeline gates caught three real defects between OCR
completion and publication; all were fixed with evidence and the delivered
file passed every gate.

## What the gates caught

1. **Join chains.** Reviewed joins form chains: a same-page seam consumed the
   block a cross-page rule named as its left (one page), and separately one
   paragraph spanned three pages. The assembler applied joins independently,
   so it either refused a valid chain or would have misordered text. Fix:
   survivor/redirect semantics applied in page order; promoted as
   `pdfcracker.joins` with regression tests failing on the old behavior.
2. **OCR pseudo-markup.** The OCR model emitted `<fcel>`/`<nl>` tags for an
   inset table. Converters passed them through as raw HTML and EPUB packaging
   crashed on invalid XHTML. Fix: evidence-checked correction of that block
   into a Markdown table with the print layout verified from the independent
   line cache; four inset group-label headings tombstoned (text preserved as
   bold lead-ins) to keep the TOC clean. Promoted as the
   `ocr_artifacts` screen.
3. **Unmarked first-page author note.** An asterisk funding note at an
   article's first page was misclassified as a list item and sat between a
   paragraph and its continuation, breaking a cross-page seam mid-sentence.
   Fix: reversible tombstone plus verbatim `（作者注：…）` re-attachment under
   the byline, restoring the seam.

## Durable decisions

- Screen every OCR page for raw markup before assembly; fix flagged blocks
  against independent evidence, never with bulk regex deletion
  (`knowledge/rules/ocr-acceptance-guards.md`).
- Joins are applied with chain-aware redirect semantics; unreviewed or
  mismatched seams keep their paragraph boundary
  (`knowledge/rules/paragraph-joins.md`).
- Independent audits are worth their cost: the content audit compares chapter
  prose, footnote round-trips, image alts and nav targets against the
  Markdown the EPUB claims to render (`pdfcracker.epub_audit`).
- The whole-book workflow, its state machine and its module map are recorded
  in `knowledge/workflows/full-scanned-book-to-epub.md`; run scripts stay
  frozen in the run directory as delivery provenance.

## Regression coverage

- `tests/test_joins.py` — sentence-end detection, same-page merge around a
  figure, both chain patterns, mismatch refusal, boundary preservation.
- `tests/test_ocr_artifacts.py` — pseudo tags, truncated tags, comparisons
  that must not flag.
- `tests/test_coverage_review.py` — contained/similar/missing lines,
  confidence and length filters, tunable thresholds.
- `tests/test_epub_audit.py` — minimal synthetic EPUB passes; broken
  backlink and altered text raise; unlinked notes become visible markers.
- `tests/test_line_geometry.py` — ink-line detection; seam supported,
  indented and short-line refusals.
- Run-local frozen suites (41 tests) remain in the run directory.
