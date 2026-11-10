# Local OCR through a readable chapter

## Reproducible local setup

Use `uv sync --locked --extra ocr-layout` for Python dependencies. On Apple
Silicon, `uv run scripts/setup_xiaomi_ocr.py` installs the project-local pinned
Ollama runtime and checksum-verified Xiaomi-OCR BF16 language/vision weights.
Start inference with `bash scripts/start_xiaomi_ocr.sh`. The listener stays on
loopback; model and layout downloads require network access during setup.

Keep runtime versions, language/vision digests, detector artifact hashes, source
PDF hash, input dimensions, prompt, decoding options and raw responses. Ollama
raw-mode image markers are runtime-specific: the verified transport for the
pinned runtime uses `[img-0]` and an explicit empty think block. A mocked request
test cannot establish that a live runner actually received the image.

## Extract before reflowing

Confirm that native text is absent or unusable, fingerprint the source, locate
chapter boundaries, and select a bounded coherent pilot. For example:

```bash
uv run pdfcracker ocr-pilot input/book.pdf --pages 5,12-24 --max-pages 14 \
  --dpi 192 --output runs/book/pilot
uv run --extra ocr-layout research/scripts/verify_xiaomi_styles.py \
  runs/book/pilot/document.json --max-calls 32
uv run scripts/review_xiaomi_pilot.py runs/book/pilot/document.json
```

Retain the whole-page transcription before any correction. Layout detection and
recognition are separate evidence sources. A chapter title can be labelled as a
header and omitted by whole-page OCR; a successful response and high body-region
scores do not prove coverage. Review chapter openings, ambiguous page furniture,
small notes and detector/recognizer disagreements. Region transcription is
additional evidence, never an unconditional replacement for page text.

Recognized headings, paragraph roles and footnote syntax are distinct from
measured fonts. Preserve nested inline semantics where evidenced, but leave
unsupported font sizes, weight and appearance unknown. Record source-backed
style recoveries as explicit transformations.

## Assemble a chapter from reviewed decisions

Use `pdfcracker assemble-chapter` with a document-specific JSON plan and the
structured OCR sidecar. The plan selects contiguous chapter pages, pins the
source, lists exact text/style edits, and explicitly decides each page boundary.
Keep the plan and its scan evidence under the document run; the reusable engine
must not contain that document's page numbers, corrections or merge exceptions.

```bash
uv run pdfcracker assemble-chapter runs/book/pilot/document.structured.json \
  --plan runs/book/chapter-01.plan.json --output runs/book/chapter-01
```

- Check both page-edge fragments and first-line indentation. Terminal punctuation
  alone does not determine a paragraph boundary: a complete sentence can continue
  into another sentence in the same paragraph on the next page.
- Join only consecutive body fragments with an explicit decision. A continuation
  can split a word; inserting a space between CJK fragments changes the text.
  Footnotes are independent blocks and must not interrupt a body continuation.
- Keep paragraphs separate where the source shows a new indented paragraph.
  Never merge across an omitted page, heading, chapter boundary or intervening body
  block merely because the concatenated prose sounds plausible.
- Put footnote definitions at chapter end, retaining their original page/block
  identities. Check reference/definition identities and uniqueness, not just counts.
- Remove a number only after proving it is page furniture. A margin number caught
  in a body block needs an explicit reversible exclusion, not a global numeric
  cleanup rule.
- Correct a recognized word against the scan. Semantically plausible alternatives
  can change meaning; do not apply a global vocabulary substitution. Preserve
  source wording, unusual terms and suspected source errors for reader review.

The reader receives a clean chapter Markdown. The structured sidecar retains
source blocks/pages, every edit and merge, and output character ranges for tracing
a paragraph back to the scan. Verify the actual delivered text against the edited
source stream and account for every selected block exactly once. Character
conservation detects loss; it does not by itself establish correct reading order,
paragraph boundaries or OCR character accuracy.

## Review, cost and retention

Use local inference and compatible cached responses first. Cache inference rather
than parser mistakes: regenerate structure with the current parser. Report
historical cached token usage separately from newly consumed tokens.

Use one economical external visual checker only when it resolves a concrete
uncertainty. Preserve model/provider IDs, raw responses, usage/cost and the
adjudication. A visual model may mistake a valid Markdown footnote identifier or
a normal page continuation for a text defect. Compare its claim with actual
strings, source geometry and recorded transformations before changing content.

Code tests, region agreement and an agent-reviewed draft do not imply human pilot
approval. Ask the reader to report the section, quoted phrase and suspected issue;
turn corrections into run decisions first and promote only generalized fixes
with regression evidence. Expand to the full book after the pilot gate passes.

Keep source PDFs, raw OCR, crops/renders used as evidence, model responses,
profiles/plans, revisions, QA and reading artifacts while review is in progress.
Retain earlier versions when publishing a revised chapter. Do not clean these
artifacts while the user has requested retention for the next conversion stage.
