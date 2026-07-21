# Semantic style preservation retrospective — 2026-07-21

## Trigger

Human review found lost TOC hierarchy, boxed callouts, a mixed-size heading, underline, synthetic bold, nested inline styles, and quote-line boundaries.

## Root causes

- `pdftext` block boundaries were too coarse for a heading followed by smaller body lines.
- Font name/weight alone could not detect fill+stroke synthetic bold.
- Line-oriented Markdown rendering fragmented styles across physical line wraps.
- TOC entries were rendered from block text rather than line indentation.
- Box and underline vectors were available but not fused into `document.json`.

## Generalized fixes

- Split mixed-size blocks before classification.
- Combine `pdftext` geometry, `pdfplumber` chars/colors/vectors, and `pypdf` text-rendering operators on selected pilot pages.
- Store orthogonal style flags and evidence on spans.
- Normalize style boundaries to whole words and consolidate same-style line wraps.
- Detect PDF-native callout rectangles and quoted-line blocks.
- Parse TOC title/page labels per line and map x-position clusters to nested Markdown levels; omit dot leaders and printed page labels from the reflowable reading copy.

## Deliberately not generalized

- Literal `**` visible on this book's PDF page 6 remains escaped source text; it is not a global cleanup rule.
- The misspelling `jokee` and Word's `Error! Bookmark not defined.` are source-content issues and are not silently corrected.
- Original gray backgrounds, drop shadows, border thickness, exact fonts, and spacing are presentation details deferred to EPUB CSS.
