# Post-assembly paragraph reflow from print-line geometry

Status: promoted to reusable knowledge on 2026-11-11 after the second
full-book delivery pass (`runs/0dfd68a3113f-film-history-p31/`), regression
module `pdfcracker.reflow` with synthetic tests (`tests/test_reflow.py`),
121 evidence-backed merges over a 427-page book.

## When to use

Join review (`knowledge/rules/paragraph-joins.md`) catches most seams
before assembly, but hard breaks the converter emitted at every print
line-wrap survive it in the hundreds when the seam evidence was missed
(cross-page sentence-final wraps, text wrapping beside figures). Text-only
heuristics cannot fix these leftovers: a paragraph ending in `。` looks
identical to a real paragraph boundary in text. Chinese typesetting obeys a
strict, recoverable geometric invariant instead, so reflow the *assembled*
Markdown against line-level OCR geometry.

## The invariant and the rule

* Every paragraph's first print line is indented 2 em (~0.047 page-width);
  continuation lines start flush at the column's left margin.
* Justified paragraphs stop filling to the right margin only on their
  final line (trailing full-width punctuation squeezes ink by ~0.035).

A block B is a continuation of the previous body block P **iff B's first
print line starts at its column's left edge AND P's last print line still
reaches its column's right edge** (minus the punctuation squeeze). Both
facts come from line-level OCR bboxes (Vision framework `*-page-*.json`
records in reading order, normalized coordinates); text predicates are a
fallback only for the few blocks whose first line cannot be matched
(fuzzy 12-char prefix, ratio ≥ 0.66; a note block may legitimately match
a page behind the cursor).

P's last line is found **without text matching**: it is the nearest core
line above B's matched line (or the previous page's last core line when B
opens a page), because Markdown keeps print order.

## Pitfalls that produced wrong or missed merges (each was a live bug)

1. **Bottom-note area is not a y threshold.** Notes can start as high as
   y0≈0.837 while note-free pages run body text down to y0≈0.878; a fixed
   cutoff both excludes real body and includes footnotes. Locate the note
   area from the first line starting with a circled marker (①②…); bare
   page-number lines (norm length < 4) drop out of the body flow.
2. **Margin modes are polluted.** Pages dominated by an indented quote,
   or by many short paragraphs whose first lines make the indent position
   dense, put the mode at the indent. Estimate the left margin as the
   *lowest dense cluster* (smallest x0 with ≥2 occurrences).
3. **Indent position must never become a "column".** A far-right start is
   a continuation only when it wraps beside a *figure*: the figure bbox
   must end at/before the line start, span to its left, and vertically
   overlap the line (verify against a figure-asset manifest with
   `bbox_norm`). Stacked lists and indented quotes start equally far
   right; without figure evidence they keep their boundary.
4. **Wrapped figures change both edges.** A narrow column beside a figure
   ends at its own right edge (e.g. x1≈0.51 while the page right is
   0.92), and text that wraps to the right of a figure starts at the
   right column's left edge (e.g. x0≈0.52). Evaluate flush/reach against
   the line's own cluster, not page-wide extrema.
5. **Display pages freeze.** Divider/TOC pages contain only short stacked
   lines (no full-width line): mark a page display when it has ≥4 core
   lines but none spanning ≥0.55 page-width, and refuse geometry merges
   there.
6. **Hard textual guards survive geometry.** Enumeration items (一、/1./
   （1）), footnote markers (①…, `[^…]`, `（第 N 页，注 N）`) and source
   notes (`- 本文发表于…`, `原载`, `摘自`) never merge regardless of
   flush/reach.

## Discipline

* Never delete or rewrite text: merging joins blocks at the character
  boundary (a space only between two ASCII alnum chars); the non-space
  character count must be asserted unchanged, and the run refuses to
  write otherwise.
* Every decision lands in a provenance log with page, bboxes, margins and
  the flush/reach verdicts — the same "label, confidence, evidence" rule
  as every other classifier.
* Freeze front-matter/TOC regions whose lines are intentional lists even
  when they look flush and justified.
* Order inversions (a block whose matched print position precedes the
  previous block's) are assembly defects, not reflow material: report
  them upstream; do not force-merge across image blocks.
