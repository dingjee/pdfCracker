# Lessons from extraction through final delivery

## Retrospective

A document adapter produced plausible paragraphs, but its first QA pass inspected
the structured intermediate rather than the delivered files. Tables existed in
the intermediate and disappeared during rendering. Image extraction succeeded,
but the two parser adapters disagreed about object names with file extensions,
leaving empty image links. Footnote counts matched while individual labels were
wrong. Finally, otherwise valid image links depended on a sibling asset directory
that was easy to omit when moving the Markdown.

These are different failure classes. A successful extraction, a populated
intermediate, aggregate counts, and a readable sample do not establish that the
deliverable is complete. Reuse existing engine adapters before writing another
document-specific adapter; object-name normalization already existed in the core.
The old local golden fixture also selected the first PDF in a directory, so
adding a new book silently changed the test input. Bind gold tests to their named
source and an explicit location instead of directory iteration order.

## Native extraction

- Confirm requested chapter boundaries against the TOC and actual start/end pages;
  retain both PDF and printed page labels. Explain truly blank selected pages.
- Inspect fonts, geometry, explicit space glyphs, duplicate glyphs, vector lines,
  and mirrored margins. Do not infer English word spaces from every geometric gap:
  letterspacing and justified CJK text can create misleading gaps.
- Classify small text before deleting it. A smaller font can represent a table,
  caption, footnote, or inline punctuation rather than decoration.
- Reconstruct ruled tables from cell boundaries, keeping headers, wrapped cells,
  and column order. Do not zip independently observed rows or infer row counts
  from numbered markers alone.
- Locate the footnote separator and bind each note-area marker to its own entry;
  body superscripts near the page bottom are not note-area markers. Counts alone
  cannot detect swapped labels.
- Keep document-specific font names, coordinates, thresholds, and exceptions out
  of the reusable engine unless a regression demonstrates a broader rule.

## Acceptance of the rendered result

1. Account for every source block exactly once: emitted, represented by a table,
   or reversibly excluded with evidence.
2. Compare independently selected native body characters with emitted characters
   per page. A character multiset detects missing characters, but does **not**
   establish reading order, paragraph correctness, or merge precision. Font-based
   selection itself needs review; it must include small-font content tables.
3. Verify the actual Markdown contains active text and every materialized table
   cell. Checking only the intermediate misses renderer data-loss defects.
4. Parse all image references, including empty destinations and reference-style
   syntax. Ignore examples inside code fences. Resolve links from the Markdown
   file's directory, within the declared artifact root; verify saved asset hashes.
5. Check identities, not only totals: section inventory, figure labels, footnote
   labels, table cells, and asset identities. Derive expectations from source
   evidence rather than guessed numbering ranges.
6. Visually sample chapter starts/ends, wrapped body text, dense captions, tables,
   notes, and cross-page joins. Keep unresolved fidelity limits explicit.

## Delivery and retention

A Markdown file and its assets form one deliverable. A portable ZIP must preserve
their relative directory structure and must be checked as an archive, including
CRC and link resolution inside the archive. A self-contained HTML can embed the
original browser-compatible image bytes; validate decoded bytes against the source
asset hashes. Never substitute page screenshots for extracted image objects.

Keep original PDFs and the assets needed by retained final outputs. The normal
workflow retains provenance, reversible exclusions, and reproducible profiles.
When a user explicitly requests removal of intermediates, verify final text and
links first, retain a concise final index, and then remove only the scoped run's
superseded logs, renders, prompts, temporary scripts, and intermediate documents.
Do not infer permission to clear other document runs or project research.

Provider-reported zero cost is not proof of zero billing. Store unavailable
telemetry as unknown; do not turn model confidence or a CLI exit code into an
acceptance result.
