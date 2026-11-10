# OCR acceptance guards before assembly and packaging

Status: promoted to reusable knowledge on 2026-11-10 after full-book delivery evidence and regression tests (`tests/test_ocr_artifacts.py`, `tests/test_coverage_review.py`).

## Screen for raw markup artifacts

Local OCR models sometimes emit pseudo-markup for layout they recognize — `<fcel>` table cells, `<nl>` line breaks — instead of plain text. Markdown converters pass such fragments through as raw HTML, and one unclosed pseudo-tag produced invalid XHTML that crashed an EPUB packaging step after hours of OCR.

- Screen every OCR page with `ocr_artifacts.find_markup_artifacts` before assembly. Any hit is review evidence for that block.
- Fix with a source-checked correction against independent evidence (line-cache coordinates, page render): rewrite the pseudo-markup into clean Markdown structure — a real table, bold-label lists — keeping every character of the primary OCR's cell text unless evidence contradicts it.
- Never regex-delete the tags in bulk: the tags encode real layout (a table box) whose content must survive.

## Classify inset boxes, not just headings

An OCR "heading" role on a boxed insert (table title, group labels such as 基本条件/做法/成效) does not make it a section of the book. Level-2 headings become TOC entries in EPUB readers. For an inset box:

- Keep the box title as one heading if it is visually prominent in print.
- Fold group labels into bold lead-ins of their content blocks (drop the label blocks with reversible tombstones, text preserved verbatim) so they do not pollute navigation.

## Unmarked first-page notes break seams

Journals and article collections reprint articles with asterisk author notes (`* 本文为…`) at the first page's bottom. OCR often misclassifies them as list items (a `- ` item) or plain body text. Left in place, the note sits between a paragraph and its continuation, and every cross-page seam over it fails validation (or worse, merges around it silently).

- Detect: a short note-like block at the page bottom, near an article start, without a circled mark, often prefixed `*` in print (compare the independent line cache).
- Fix with evidence: tombstone the misfiled block and re-attach its text verbatim as a visible `（作者注：…）` marker under the byline/title block. Provenance records both operations.

## Cross-check coverage with an independent line cache

- Compare every non-blank page's OCR text against an independent local line cache (`coverage_review.flag_missing_lines`): contained lines pass; neither-contained-nor-similar lines (bigram overlap < 0.55) are flagged for review.
- Short and low-confidence lines are skipped; they are dominated by running heads and noise.
- The primary OCR stays primary: the independent cache has both missed and inserted characters. A flag routes a human to the page render; it never triggers automatic rewriting.
