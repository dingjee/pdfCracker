# Local OCR and chapter reflow — 2026-11-05

The first scan-only pilot produced useful page transcriptions, but a concatenation
of pages was not yet a chapter reading copy. Physical page breaks fragmented words
and paragraphs; a chapter title was omitted, a margin number entered body text,
inline emphasis was flattened, and a plausible character confusion changed a
technical term. Footnote definitions also needed independent treatment while
joining the surrounding body across a page break.

The chapter preparation pass therefore reviews every boundary against scan-edge
evidence and indentation. Chapter-specific corrections and heading hierarchy stay
in a hashed run plan. The reusable assembly tool applies exact edits and explicit
joins, relocates notes, retains exclusions, and checks delivered-text conservation
with paragraph-to-source mappings. A full sentence at a page edge is insufficient
evidence for either joining or splitting a paragraph.

These decisions are an agent review for a reader-ready draft, not human approval
of recognition quality. The reader's substantive corrections are the next source
of gold data. Raw and derived artifacts remain available so later fixes and the
full-book conversion can reuse evidence without rerunning every model call.

An initial assembly check also caught a duplicated chapter title and a footnote
definition that existed in the sidecar but was absent from the delivered
Markdown. Metadata pairing was insufficient. The final checks therefore cover
actual rendered definitions and references, title identity, paragraph character
ranges and source-block accounting; a failed quality check must block artifact
publication. The failed attempt is retained as process evidence.

The reusable workflow is documented in
`knowledge/workflows/local-ocr-and-chapter-review.md`. No document-specific word
substitutions or coordinates were added to the reusable classifiers or AGENTS.md.
