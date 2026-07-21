# Native PDF style semantics

Status: promoted to reusable knowledge on 2026-07-21 after pilot evidence and regression tests.

## Principle

Preserve semantic intent, not pixel-identical appearance. Store styles as independent flags so combinations such as bold+italic+underline+color remain representable. Markdown carries stable semantics; EPUB CSS supplies exact presentation later.

## Evidence sources, in priority order

1. PDF text rendering mode (`Tr`): stroke/fill+stroke text is evidence of synthetic bold even when the embedded font name is unchanged.
2. Font metadata: names containing Bold/Black/Semibold/Demi and Italic/Oblique.
3. Vector geometry: thin horizontal objects overlapping the text baseline indicate underline; enclosing filled/bordered rectangles indicate callouts, not underline.
4. Text fill color: non-gray colors become semantic color roles plus exact RGB provenance.
5. Vision: only when the native sources conflict or expose no usable evidence.

## Normalization

- Bold, italic, and underline may not end inside an alphanumeric word merely because the PDF split drawing operators there. Extend a confirmed style to the full word and internal hyphen/apostrophe, but not terminal punctuation.
- Do not apply that expansion to color; a differently colored initial letter can be intentional.
- Adjacent runs with identical semantics separated only by a physical line break or whitespace are one semantic run.
- Treat inline styles as orthogonal. A run may simultaneously be bold, italic, underlined, and colored.

## Markdown mapping

- Bold only: `**text**`
- Italic only: `*text*`
- Bold+italic: `***text***`
- Any combination containing underline or semantic color: semantic HTML (`strong`, `em`, `u`, and `span.text-<role>`).
- Exact RGB, bbox, parser evidence, and confidence remain in `document.json`.

## Block roles

- Text enclosed by a non-white PDF rectangle becomes a semantic blockquote; shadow, background, border thickness, and exact padding are deferred to CSS.
- Indented short lines that are independently quoted remain separate blockquotes rather than being flattened into one paragraph.

## Regression evidence

- `tests/test_extract.py`: mixed heading/body split, vector underline, synthetic bold, callouts, quote-line preservation, responsive TOC, and no mid-word Markdown emphasis boundary.
- `tests/test_markdown.py`: nested bold+italic+underline+red serialization.
