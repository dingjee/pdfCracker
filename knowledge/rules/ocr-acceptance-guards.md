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

## Keep the body outline equal to the printed table of contents

Readings apps differ in where they take the table of contents from: some read the EPUB navigation document, others derive an outline from the body headings. Cleaning only the navigation document leaves those readers with the full heading mess (note sections, ordinal fragments, running labels, kept-duplicate title lines).

- Treat the printed table of contents as the single source of truth for the outline: numbered sections and the unnumbered sub-entries it lists stay headings; everything else (note-section heads, inset-box labels, running labels, ordinal fragments, duplicate title lines, sentence fragments misread as headings) is demoted to a bold label — words preserved, outline level removed.
- Demotion must be recorded as a transformation with evidence, and it applies to synthesized heads too (the note-section label the assembler adds at each section end, and the pilot chapter's own heads).
- An OCR heading that is really a fragment of one sentence must be repaired, not demoted: join it back to the paragraph it was cut from (evidence: the independent line cache places the line between body lines, not in a heading position).
- A heading that repeats another section's title (a table-of-contents listing
  line promoted to a heading) is a label, not a section: demote it, so the
  title appears once, on the section it names.
- Give every content document its own `<title>`. Converters that split by
  chapter leave the file name in place, and readers that label their entries
  from the document title then show bare numbers instead of chapter names.

## Unmarked first-page notes break seams

Journals and article collections reprint articles with asterisk author notes (`* 本文为…`) at the first page's bottom. OCR often misclassifies them as list items (a `- ` item) or plain body text. Left in place, the note sits between a paragraph and its continuation, and every cross-page seam over it fails validation (or worse, merges around it silently).

- Detect: a short note-like block at the page bottom, near an article start, without a circled mark, often prefixed `*` in print (compare the independent line cache).
- Fix with evidence: tombstone the misfiled block and re-attach its text verbatim as a visible `（作者注：…）` marker under the byline/title block. Provenance records both operations.

## Cross-check coverage with an independent line cache

- Compare every non-blank page's OCR text against an independent local line cache (`coverage_review.flag_missing_lines`): contained lines pass; neither-contained-nor-similar lines (bigram overlap < 0.55) are flagged for review.
- Short and low-confidence lines are skipped; they are dominated by running heads and noise.
- The primary OCR stays primary: the independent cache has both missed and inserted characters. A flag routes a human to the page render; it never triggers automatic rewriting.
