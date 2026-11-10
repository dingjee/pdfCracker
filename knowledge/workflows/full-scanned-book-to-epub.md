# Full scanned book: local OCR to verified EPUB

Distilled on 2026-11-10 from the first full-book scanned delivery (run
`runs/0dfd68a3113f-film-history-p31/`, a 427-page scanned book). The run
directory keeps the frozen, self-contained scripts that produced the delivered
file; the reusable pieces live in `src/pdfcracker/` with tests in `tests/`.
Copy the pattern, not the page numbers.

## Pipeline shape

1. **Fingerprint and gate.** Hash the source PDF; get human approval for the
   full-book scope and any scanned-illustration cropping. A reviewed pilot
   chapter must already be accepted; its pages are assembled from the reviewed
   pilot markdown instead of raw OCR (the pilot range is excluded from page
   assembly and its content is reused verbatim).
2. **Background OCR with resume.** One OCR runner process over all pages
   (local loopback model), one page record per page with the source hash
   inside. Blank pages legitimately return empty responses — confirm each with
   pixel evidence and record the evidence file per blank page. The runner must
   reuse compatible success pages (`source_sha256` + page number + status) on
   restart. Exactly one runner and one completion process may exist; enforce
   with pid files and an flock on the completion side.
3. **Layout cache.** Region layout per page (roles: paragraph, heading,
   footnote, caption, image…), rendered page images for geometry evidence.
4. **Acceptance guards** (see `knowledge/rules/ocr-acceptance-guards.md`):
   markup-artifact screen per page; independent line-cache coverage review
   over all non-blank pages; blank-page evidence list.
5. **Join review.** Generate candidate seams with barriers and geometry gates,
   review each seam's tail/head plus geometry, record approved joins with
   evidence (`knowledge/rules/paragraph-joins.md`). Unreviewed boundaries keep
   their paragraph break.
6. **Staged assembly.** Bind figure assets (caption regions and anchors,
   numbered-figure captions bound to OCR blocks), apply profile page overrides
   (blank pages, corrections, drops with reversible tombstones, approved
   joins), collect footnote definitions per section, and emit `document.json`
   (full provenance), `book.md`, and images. Assembly QA must pass with zero
   failures; `needs_review` items are recorded, not ignored.
7. **EPUB packaging with hard gates.** Convert unlinked note definitions to
   visible inline markers, then package with a pinned converter:
   EPUBCheck (project-local jar) must report 0 errors AND 0 warnings;
   alt text, media mapping and chapter splitting are verified against the
   markdown. Packaging must refuse to overwrite existing outputs.
   The navigation must mirror the printed table of contents, not the
   converter's heading list: transcribe the printed TOC with levels, and
   rebuild the navigation list from it (`pdfcracker.epub_nav`), linking each
   entry to its body heading with an order cursor so repeated titles resolve
   to their own chapter; any unmatched entry aborts the build. Note sections,
   inset-box labels, running labels and sentence fragments stay in the body
   but never become navigation entries.
   The body outline must mirror the same list: readers that derive bookmarks
   from the body headings see every heading, so headings outside the printed
   TOC (note-section heads, inset labels, running labels, ordinal fragments,
   duplicate title lines) are demoted to bold labels during assembly, with
   the demotion recorded as a transformation.
   Every content document must also carry its own `<title>`. A converter that
   splits the book by chapter leaves the file name there (`ch010.xhtml`), and
   readers that label entries from the document title then show bare numbers
   instead of chapter names (`pdfcracker.epub_nav.set_document_titles`).
8. **Independent audit** (`pdfcracker.epub_audit.verify`): chapter prose
   round-trip, footnote reference↔definition↔back-link round-trips, image
   alt/resource equality, and every navigation target resolved.
9. **Atomic publish and delivery check.** The completion process hard-links
   the validated candidate to the delivery path only after all gates pass, and
   writes a completion state file (`processing → assembly → packaging →
   complete | needs_attention`). Final verification: file exists, state
   complete, SHA-256 recorded, full page coverage (assembled pages + pilot
   pages), figure and cover counts, navigation and footnote counts, a fresh
   coverage review, and EPUBCheck re-checked from the delivered file's build
   log.

## State machine and recovery

- The completion process polls the OCR/layout caches, updates a public state
  JSON, auto-restarts a dead OCR runner (bounded retries), and never runs
  concurrently (flock).
- On `needs_attention`, read the state detail and the stage QA JSON; fix the
  specific cause; back up failed intermediates (they are evidence); rerun the
  completion process only when no other instance holds the lock. Backup or
  remove stale intermediates — the packager refuses to overwrite.

## Reusable modules

| Module | Purpose |
|---|---|
| `pdfcracker.joins` | sentence-end detection; reviewed join application with chain redirects |
| `pdfcracker.line_geometry` | ink-line measurement; seam fill/indent support |
| `pdfcracker.ocr_artifacts` | raw-markup screen for OCR text |
| `pdfcracker.coverage_review` | independent line-cache cross-check |
| `pdfcracker.epub_audit` | unlinked-note preservation; independent EPUB audit |
| `pdfcracker.epub_nav` | rebuild the navigation TOC from the printed table of contents |

The run-specific pieces that stay per-document: profile (sections, page
overrides, corrections, blank-page evidence), figure manifest and crops,
reviewed-joins record, thresholds tuned to the scan, and the orchestration
scripts (kept frozen in the run directory as delivery provenance).
