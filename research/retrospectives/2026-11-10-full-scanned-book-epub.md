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
- `tests/test_epub_nav.py` — printed-TOC matching with an order cursor over
  repeated titles, enumeration-prefix fallback, match aliases for TOC/body
  drift, unmatched-entry failure, nested list rebuild.
- Run-local frozen suites (41 tests) remain in the run directory.

## 2026-11-10 follow-up: navigation pollution

The delivered EPUB's first navigation (180 links) listed every level-2
heading, including 18 note sections, inset-box labels, a running label
(Chapter 10), ordinal fragments and duplicate title fragments. Fixed by
transcribing the printed table of contents (PDF pages 11-15) into
`toc-printed.json` and rebuilding the navigation from it (125 links);
`pdfcracker.epub_nav` now carries the reusable implementation. Two OCR-
truncated two-line headings (7.3, 8.3) were restored against the independent
line cache and the printed TOC.

The user then reported that bookmarks were still messy: their reader derives
the outline from the body headings, not from the navigation document, and the
body still carried all 180 headings. The body outline was aligned with the
printed TOC as well — headings outside it were demoted to bold labels during
assembly (32 demotions, each recorded as a transformation), including the
assembler's own synthesized note-section heads and the pilot chapter's. One
OCR "heading" turned out to be a sentence fragment cut in two
("电影史上文本变体的最让人头疼 | 的一个例子要数1903年…", PDF page 57) and was
repaired by joining it back to its paragraph. Result: 128 body headings, all
of them printed-TOC entries, and 125 navigation links.

Lesson recorded in the workflow doc: the navigation document *and* the body
heading structure must both mirror the printed TOC — fixing one does not fix
readers that use the other.
