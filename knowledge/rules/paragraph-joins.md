# Paragraph joins across blocks, figures and pages

Status: promoted to reusable knowledge on 2026-11-10 after full-book delivery evidence and regression tests (`tests/test_joins.py`, `tests/test_line_geometry.py`).

## Principle

OCR splits one source paragraph into several blocks: around figures on the same page and at every page turn. Joins reassemble them, but an unreviewed seam must keep its paragraph boundary — a missed join costs readability, a wrong join destroys text. Every applied join carries evidence and provenance; nothing merges in bulk.

## Candidate seams

A cross-page seam is a join candidate only when all of the following hold:

- The left block is the last body block of its page; the right block is the first body block of the next page.
- Neither block is a footnote definition, figure caption, running header/footer, or the page title.
- No heading, quote, or list item sits before the right block on its page (a barrier: the page starts a new context).
- The left text does not end a sentence (`joins.ends_sentence` ignores trailing note marks like `①` or `[^p1-2]` when sentence punctuation sits before them).
- Ink geometry supports continuation (`line_geometry.seam_support`): the left block's final line fills about ≥ 0.91 of the column width and the right block's first line is not indented beyond ±11 px at 192 dpi render scale. Geometry is measured from ink, not OCR text, so it stays evidence when the text is uncertain.

A same-page seam (text flowing around an inset figure) is a candidate when the same fill and indent conditions hold between two adjacent paragraph regions with only figure regions between them.

## Review discipline

- Read every candidate's tail/head text plus its geometry evidence. A mid-word split across the seam is the strongest semantic evidence.
- Record approved ranges with the reviewed-join tooling; never batch-approve without per-seam evidence.
- A discrepancy between the primary OCR and an independent line cache is review evidence, not a command to rewrite (`coverage_review.flag_missing_lines`).

## Chain semantics

Joins form chains: one paragraph can span three or more blocks and pages (`A | B | C`). Applying a join merges the right block into the left one and removes it from the flow, so a later rule that still names the consumed block must redirect to the surviving block. `joins.apply_reviewed_joins` implements this:

- Same-page seams first, then cross-page seams in page order, so chain text is appended in source order.
- `redirects` maps every consumed block id to its surviving block id; a survivor map locates the surviving flow item on its page.
- A rule whose named blocks are missing or unexpected is reported as an issue and skipped — its blocks keep the paragraph boundary (safe default).

Evidence: two gate catches during one full-book delivery — a same-page+cross-page chain and a three-page chain — both of which a naive "apply each join independently" implementation refuses or misorders.
