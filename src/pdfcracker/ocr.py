"""Local Xiaomi-OCR-0 pilot adapter (Ollama, loopback only).

This module is the OCR escalation path described in ``AGENTS.md``: OCR is allowed
only when the native text layer is absent, corrupt, duplicated or demonstrably
worse. :func:`run_ocr_pilot` therefore inspects the native text layer first and
refuses to run against a healthy native-text slice unless ``force=True``.

Scope
-----
* Pilot only. The caller must pass an explicit, bounded list of pages. There is no
  full-book mode; ``max_pages`` caps the slice and a hard ceiling rejects oversized
  pilots.
* Local inference only. The Ollama base URL must resolve to a loopback host and the
  transport refuses anything else. No external API is ever contacted and the recorded
  cost is always zero.
* The OCR model is reached through Ollama's ``/api/generate`` endpoint in *raw*,
  non-thinking mode with a directly implemented ``httpx`` transport. The upstream
  repository is not required at runtime; the upstream prompt is transcribed verbatim
  as a constant and the intentional divergence is documented next to it.

Prompt fidelity and preservation
--------------------------------
The upstream document prompt asks the model to *ignore headers and footers*. That
conflicts with the PDFCracker rule that headers and footers are never deleted and
that footnotes are never dropped, so two prompt variants exist:

``upstream``
    The byte-exact upstream prompt, kept for parity comparisons with upstream runs.
``preservation`` (default)
    A documented variant that asks for **all** page text, explicitly including
    headers, footers, page numbers and footnotes. The divergence from upstream is
    recorded in telemetry and in the review file so a human can check that nothing
    was silently skipped.

Raw prompt and thinking mode
----------------------------
The model is called through Ollama ``/api/generate`` in raw mode, so no chat
template is applied by the server. The adapter therefore renders the prompt itself
using the canonical Qwen layout, and closes the assistant turn with an **empty
think block**::

    <|im_start|>user
    [img-0]
    {prompt}<|im_end|>
    <|im_start|>assistant
    <think>

    </think>

The trailing ``<think></think>`` mirrors the checkpoint's ``chat_template.jinja``
assistant ending for ``enable_thinking=false``. Without it the model may start
thinking, which wastes the output budget and pollutes the Markdown.

Image preprocessing
-------------------
The render path mirrors the upstream OCR PDF pipeline: ``pypdfium2`` renders at
``dpi / 72`` scale, RGB conversion, downscale to a 2200 px wide edge, PNG encoding,
then the upstream ``fit_image`` bound of a 2048 px long edge with LANCZOS
resampling. The prepared PNG is both the bytes sent to the model and the retained
inference evidence.

Known limits
------------
* **Page mode only.** The upstream two-stage pipeline with PP-DocLayoutV3 region
  detection and per-region crops is not implemented here; every page is sent as one
  image. Footnote regions detected by the layout model upstream are therefore not
  separated from the surrounding text.
* **No geometry.** OCR output is Markdown, so there are no bounding boxes, font
  metrics or reading-order coordinates. Every geometry field is explicitly ``null``
  and carries ``geometry.available = false`` with a reason. Nothing is invented.
* **Roles are heuristics.** Heading/body/list/footnote roles are inferred from
  ``markdown-it`` tokens. They are triage signals with confidence values, never
  acceptance criteria, and every page still requires human review.
* **No deletions.** Nothing is dropped: unknown blocks, tables, inline content and
  failed pages are all retained in the artifacts. Headers and footers are never
  removed, and blocks are never merged across page boundaries.
* **Failed pages are never accepted.** Truncated output, HTTP errors, timeouts and
  unparsable responses are recorded as failed review entries with severity ``high``;
  their text is preserved in the raw artifacts but is not part of the accepted book.
* **Page renders are inference evidence, not content images.** Renders are never
  inserted into the Markdown and are never registered as extracted images.
* **Faithful assembly.** ``book.md`` concatenates the raw model text of each page
  verbatim; the engine never rewrites, reinterprets or normalizes model output.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

import httpx
from markdown_it.rules_block.reference import reference as parse_reference_definition
from PIL import Image

from pdfcracker.markdown_content import markdown_parser


OCR_DOCUMENT_SCHEMA = "pdfcracker.ocr.document/1"
OCR_PAGE_SCHEMA = "pdfcracker.ocr.page/1"
OCR_REVIEW_SCHEMA = "pdfcracker.ocr.review/1"

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "longwayxu/xiaomi-ocr-0:bf16"
DEFAULT_MAX_PAGES = 12
DEFAULT_MAX_TOKENS = 4096
DEFAULT_DPI = 144
DEFAULT_TIMEOUT = 300.0

#: Absolute ceiling for a single pilot run, independent of ``max_pages``.
MAX_PILOT_PAGES = 64

#: Upstream image bounds (``evaluation_pipeline/cropping.py`` and the MCP server).
MAX_IMAGE_EDGE = 2048
MAX_RENDER_WIDTH = 2200

#: A page with at least this many non-whitespace native characters counts as having
#: a healthy text layer.
NATIVE_HEALTHY_CHARS = 200

#: Byte-exact upstream document prompt (``skills/xiaomi-ocr/mcp_ocr_server.py``).
UPSTREAM_DOCUMENT_PROMPT = (
    "Extract all information from the main body of the document image and represent it "
    "in markdown format, ignoring headers and footers. Tables should be expressed in OTSL "
    "format, formulas in the document should be represented using LATEX format, and the "
    "parsing should be organized according to the reading order."
)

#: PDFCracker preservation variant of the upstream prompt. The only difference is
#: the treatment of page furniture and footnotes: upstream asks the model to ignore
#: headers and footers, which would delete content PDFCracker must retain.
PRESERVATION_DOCUMENT_PROMPT = (
    "Extract all information from the document image and represent it in markdown "
    "format. Include every piece of text visible on the page, including headers, "
    "footers, page numbers, marginalia and footnotes; do not skip or summarize any "
    "of them. Tables should be expressed in OTSL format, formulas in the document "
    "should be represented using LATEX format, and the parsing should be organized "
    "according to the reading order."
)

PROMPT_VARIANTS = {
    "upstream": UPSTREAM_DOCUMENT_PROMPT,
    "preservation": PRESERVATION_DOCUMENT_PROMPT,
}

PROMPT_VARIANT_NOTES = {
    "upstream": (
        "Byte-exact upstream prompt. Upstream asks the model to ignore headers and "
        "footers, which may delete page furniture PDFCracker must retain; use only "
        "for parity comparisons."
    ),
    "preservation": (
        "Documented divergence from the upstream prompt: all visible text is "
        "requested, including headers, footers, page numbers and footnotes. The "
        "upstream wording ('ignoring headers and footers') was deliberately not used."
    ),
}

#: Canonical Qwen raw prompt used for Ollama ``/api/generate`` raw mode. The
#: assistant turn is closed with an empty think block, mirroring the checkpoint's
#: ``chat_template.jinja`` for ``enable_thinking=false``; without it the model may
#: start thinking and waste the output budget.
# Ollama 0.40.14 replaces [img-N] with its runner's media marker. A literal
# <image> yields zero media markers and HTTP 400 despite images being supplied.
QWEN_RAW_PROMPT_TEMPLATE = (
    "<|im_start|>user\n[img-0]\n{prompt}<|im_end|>\n<|im_start|>assistant\n"
    "<think>\n\n</think>\n\n"
)

#: Context window requested from Ollama. The default Ollama context is too short
#: for a dense scanned page plus a full Markdown answer.
OLLAMA_NUM_CTX = 16384

#: Role confidence heuristics. These are triage signals, never acceptance criteria.
ROLE_CONFIDENCE = {
    "heading": 0.9,
    "list_item": 0.85,
    "table": 0.85,
    "footnote_definition": 0.8,
    "quote": 0.7,
    "footnote": 0.65,
    "code_block": 0.6,
    "body": 0.55,
    "separator": 0.5,
    "unknown": 0.25,
}

FOOTNOTE_SECTION_RE = re.compile(
    r"^(footnotes?|notes|脚注|尾注|注释)\s*[:：]?$", re.IGNORECASE
)
FOOTNOTE_DEFINITION_RE = re.compile(r"^\[\^([^\]\s]+)\]:\s*\S")
FOOTNOTE_REFERENCE_RE = re.compile(r"\[\^([^\]\s]+)\]")

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}


class OcrPilotError(RuntimeError):
    """Base error for the OCR pilot adapter."""


class OcrValidationError(OcrPilotError):
    """Raised when a pilot request is invalid. No files are written and no API call
    is made when this is raised."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class OcrOptions:
    """Effective options for one OCR pilot run."""

    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    max_pages: int = DEFAULT_MAX_PAGES
    max_tokens: int = DEFAULT_MAX_TOKENS
    dpi: int = DEFAULT_DPI
    timeout: float = DEFAULT_TIMEOUT
    force: bool = False
    prompt_variant: str = "preservation"
    temperature: float = 0.0
    top_p: float = 0.8
    repeat_penalty: float = 1.0
    raw: bool = True
    think: bool = False

    def task_prompt(self) -> str:
        return PROMPT_VARIANTS[self.prompt_variant]

    def signature_payload(self) -> dict[str, Any]:
        """Options that invalidate a cached page response when they change."""
        return {
            "max_tokens": self.max_tokens,
            "dpi": self.dpi,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "repeat_penalty": self.repeat_penalty,
            "raw": self.raw,
            "think": self.think,
            "prompt_variant": self.prompt_variant,
            "model": self.model,
        }

    def as_provenance(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "max_pages": self.max_pages,
            "max_tokens": self.max_tokens,
            "num_ctx": OLLAMA_NUM_CTX,
            "dpi": self.dpi,
            "timeout_seconds": self.timeout,
            "force": self.force,
            "prompt_variant": self.prompt_variant,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "repeat_penalty": self.repeat_penalty,
            "raw": self.raw,
            "think": self.think,
        }


@dataclass(frozen=True)
class NativeTextReport:
    """Native text layer inspection for the selected pages."""

    per_page_characters: dict[int, int] = field(default_factory=dict)
    healthy_pages: list[int] = field(default_factory=list)
    threshold: int = NATIVE_HEALTHY_CHARS
    method: str = "pdftext"
    error: str | None = None

    def as_provenance(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "threshold_characters": self.threshold,
            "per_page_characters": {
                str(page): count for page, count in sorted(self.per_page_characters.items())
            },
            "healthy_pages": self.healthy_pages,
            "error": self.error,
        }


@dataclass
class ParsedBlock:
    """One conservatively classified block of OCR Markdown."""

    index: int
    role: str
    confidence: float
    evidence: list[str]
    text: str
    markdown: str
    inline: list[dict[str, Any]]
    heading_level: int | None = None
    footnote_labels: list[str] = field(default_factory=list)

    def as_record(self, page: int, page_id: str) -> dict[str, Any]:
        return {
            "id": f"{page_id}-b{self.index:04d}",
            "page": page,
            "page_id": page_id,
            "index": self.index,
            "role": self.role,
            "role_confidence": round(self.confidence, 3),
            "role_evidence": self.evidence,
            "role_confidence_note": (
                "Heuristic triage signal derived from Markdown structure only; "
                "it is not an acceptance criterion."
            ),
            "text": self.text,
            "markdown": self.markdown,
            "inline": self.inline,
            "heading_level": self.heading_level,
            "footnote_labels": self.footnote_labels,
            "bbox": None,
            "bbox_norm": None,
            "font": None,
            "geometry": {
                "available": False,
                "reason": "ocr_markdown_output_has_no_pdf_geometry",
            },
            "style": {
                "bold": None,
                "italic": None,
                "underline": None,
                "semantic_color": None,
                "reason": "ocr_markdown_output_has_no_font_metrics",
            },
        }


class OcrPilotResult:
    """Summary of a completed OCR pilot run."""

    def __init__(
        self,
        *,
        output_dir: Path,
        document_path: Path,
        book_path: Path,
        review_path: Path,
        pages: list[int],
        ok_pages: list[int],
        failed_pages: list[int],
        model: str,
        model_digest: str,
        telemetry: dict[str, Any],
    ) -> None:
        self.output_dir = output_dir
        self.document_path = document_path
        self.book_path = book_path
        self.review_path = review_path
        self.pages = pages
        self.ok_pages = ok_pages
        self.failed_pages = failed_pages
        self.model = model
        self.model_digest = model_digest
        self.telemetry = telemetry

    def as_dict(self) -> dict[str, Any]:
        return {
            "output_dir": str(self.output_dir),
            "document_path": str(self.document_path),
            "book_path": str(self.book_path),
            "review_path": str(self.review_path),
            "pages": self.pages,
            "ok_pages": self.ok_pages,
            "failed_pages": self.failed_pages,
            "model": self.model,
            "model_digest": self.model_digest,
            "telemetry": self.telemetry,
        }


# ---------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------


def validate_loopback_base_url(base_url: str) -> str:
    """Return a normalized loopback base URL or raise.

    OCR inference must stay on this machine. Any non-loopback host is rejected
    before a single file is written or a request is sent.
    """
    candidate = (base_url or "").strip()
    if not candidate:
        raise OcrValidationError("base_url must not be empty")
    parsed = urlparse(candidate)
    if parsed.scheme != "http":
        raise OcrValidationError(
            f"base_url must use http:// on loopback, got {base_url!r}"
        )
    host = (parsed.hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        raise OcrValidationError(
            f"base_url must point to a loopback host ({sorted(LOOPBACK_HOSTS)}), "
            f"got {host!r}; local OCR must never call an external API"
        )
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise OcrValidationError(
            f"base_url must not contain a path, query or fragment, got {base_url!r}"
        )
    return candidate.rstrip("/")


def _validate_pages(pages: Any, page_count: int, max_pages: int) -> list[int]:
    if pages is None:
        raise OcrValidationError(
            "pages must be an explicit list of page numbers; full-book mode does not exist"
        )
    if isinstance(pages, (str, bytes)):
        raise OcrValidationError("pages must be a list of integers, not a string")
    try:
        requested = list(pages)
    except TypeError as error:
        raise OcrValidationError("pages must be an iterable of integers") from error
    if not requested:
        raise OcrValidationError(
            "pages must not be empty; OCR pilots require an explicit bounded page list"
        )
    normalized: list[int] = []
    for page in requested:
        if isinstance(page, bool) or not isinstance(page, int):
            raise OcrValidationError(f"page numbers must be integers, got {page!r}")
        if page < 1 or page > page_count:
            raise OcrValidationError(
                f"page {page} is outside the document range 1..{page_count}"
            )
        normalized.append(page)
    if len(set(normalized)) != len(normalized):
        raise OcrValidationError("pages must not contain duplicates")
    if len(normalized) > max_pages:
        raise OcrValidationError(
            f"pilot requests {len(normalized)} pages but max_pages is {max_pages}; "
            "split the pilot into smaller slices"
        )
    if len(normalized) > MAX_PILOT_PAGES:
        raise OcrValidationError(
            f"pilot requests {len(normalized)} pages but the hard ceiling is "
            f"{MAX_PILOT_PAGES}"
        )
    return sorted(normalized)


def _validate_options(options: OcrOptions) -> OcrOptions:
    if options.max_pages < 1:
        raise OcrValidationError("max_pages must be at least 1")
    if options.max_tokens < 1:
        raise OcrValidationError("max_tokens must be at least 1")
    if options.dpi <= 0:
        raise OcrValidationError("dpi must be positive")
    if options.timeout <= 0:
        raise OcrValidationError("timeout must be positive")
    if options.prompt_variant not in PROMPT_VARIANTS:
        raise OcrValidationError(
            f"prompt_variant must be one of {sorted(PROMPT_VARIANTS)}, "
            f"got {options.prompt_variant!r}"
        )
    if not options.model.strip():
        raise OcrValidationError("model must not be empty")
    return OcrOptions(
        base_url=validate_loopback_base_url(options.base_url),
        model=options.model.strip(),
        max_pages=options.max_pages,
        max_tokens=options.max_tokens,
        dpi=options.dpi,
        timeout=options.timeout,
        force=options.force,
        prompt_variant=options.prompt_variant,
        temperature=options.temperature,
        top_p=options.top_p,
        repeat_penalty=options.repeat_penalty,
        raw=options.raw,
        think=options.think,
    )


def _pdf_page_count(pdf_path: Path) -> int:
    try:
        import pypdfium2 as pdfium
    except ImportError as error:  # pragma: no cover - dependency is declared
        raise OcrValidationError(
            "rendering PDF pages requires pypdfium2; install the project environment"
        ) from error
    try:
        with pdfium.PdfDocument(str(pdf_path)) as document:
            return len(document)
    except Exception as error:
        raise OcrValidationError(f"cannot open PDF {pdf_path}: {error}") from error


def inspect_native_text(pdf_path: Path, pages: Sequence[int]) -> NativeTextReport:
    """Measure the native text layer of the selected pages.

    The report is advisory: the caller decides whether a partially healthy slice
    is acceptable. A fully healthy slice is rejected unless ``force=True``.
    """
    try:
        from pdftext.extraction import dictionary_output
    except ImportError as error:  # pragma: no cover - dependency is declared
        return NativeTextReport(
            per_page_characters={page: 0 for page in pages},
            healthy_pages=[],
            error=f"pdftext unavailable: {error}",
        )
    wanted = set(pages)
    counts: dict[int, int] = {page: 0 for page in pages}
    try:
        raw_pages = dictionary_output(str(pdf_path), sort=True, keep_chars=False)
    except Exception as error:
        return NativeTextReport(
            per_page_characters=counts,
            healthy_pages=[],
            error=f"native text inspection failed: {type(error).__name__}: {error}",
        )
    for raw_page in raw_pages:
        number = int(raw_page["page"]) + 1
        if number not in wanted:
            continue
        characters = 0
        for block in raw_page.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    characters += len("".join(str(span.get("text", "")).split()))
        counts[number] = characters
    healthy = sorted(page for page, count in counts.items() if count >= NATIVE_HEALTHY_CHARS)
    return NativeTextReport(
        per_page_characters=counts,
        healthy_pages=healthy,
        threshold=NATIVE_HEALTHY_CHARS,
        method="pdftext",
    )


def enforce_native_text_policy(
    report: NativeTextReport,
    pages: Sequence[int],
    *,
    force: bool,
) -> None:
    """Reject a healthy native-text slice unless the caller forces OCR."""
    if force:
        return
    if report.error:
        raise OcrValidationError(
            f"Native text inspection failed: {report.error}. Inspect the input or use force=True."
        )
    healthy = [page for page in pages if page in set(report.healthy_pages)]
    if healthy:
        raise OcrValidationError(
            "the selected pages have a healthy native text layer "
            f"(pages {healthy}, threshold {report.threshold} characters). "
            "OCR is an escalation, not a replacement for native extraction; pass "
            "force=True to override deliberately."
        )


# ---------------------------------------------------------------------------
# image preparation
# ---------------------------------------------------------------------------


def render_page_png(pdf_path: Path, page_number: int, dpi: int) -> bytes:
    """Render one page exactly like the upstream OCR PDF pipeline."""
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(str(pdf_path)) as document:
        page = document[page_number - 1]
        try:
            bitmap = page.render(scale=dpi / 72)
            try:
                image = bitmap.to_pil().convert("RGB")
            finally:
                bitmap.close()
        finally:
            page.close()
    if image.width > MAX_RENDER_WIDTH:
        scale = MAX_RENDER_WIDTH / image.width
        image = image.resize(
            (MAX_RENDER_WIDTH, int(image.height * scale)), Image.Resampling.LANCZOS
        )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def prepare_image(png_bytes: bytes) -> bytes:
    """Apply the upstream ``fit_image`` bound: RGB, 2048 px long edge, PNG."""
    with Image.open(io.BytesIO(png_bytes)) as source:
        image = source.convert("RGB")
        if image.width > MAX_IMAGE_EDGE or image.height > MAX_IMAGE_EDGE:
            image.thumbnail(
                (MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS
            )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
    return buffer.getvalue()


def prepare_page_image(pdf_path: Path, page_number: int, dpi: int) -> bytes:
    """Render and prepare one page for inference; the result is the model input."""
    return prepare_image(render_page_png(pdf_path, page_number, dpi))


def image_dimensions(png_bytes: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(png_bytes)) as image:
        return image.size


# ---------------------------------------------------------------------------
# markdown parsing
# ---------------------------------------------------------------------------


def _inline_records(inline_token) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for child in inline_token.children or []:
        record: dict[str, Any] = {
            "type": child.type,
            "content": child.content,
            "markup": child.markup,
        }
        if child.type == "text":
            record["bold"] = None
            record["italic"] = None
            record["underline"] = None
            record["semantic_color"] = None
        records.append(record)
    return records


def _plain_text(inline_token) -> str:
    if inline_token is None:
        return ""
    parts: list[str] = []
    for child in inline_token.children or []:
        if child.type in {"text", "code_inline"}:
            parts.append(child.content)
        elif child.type == "softbreak":
            parts.append("\n")
        elif child.type == "hardbreak":
            parts.append("\n")
        elif child.type == "image":
            parts.append(child.content)
        elif child.type == "html_inline":
            parts.append(child.content)
    return "".join(parts)


def _slice_source(lines: list[str], token) -> str:
    if not token.map:
        return ""
    start, end = token.map
    return "\n".join(lines[start:end]).strip("\n")


def _mark_consumed(consumed: list[tuple[int, int]], token) -> None:
    if token.map:
        consumed.append((token.map[0], token.map[1]))


def _is_consumed(consumed: list[tuple[int, int]], token) -> bool:
    if not token.map:
        return False
    start = token.map[0]
    return any(low <= start < high for low, high in consumed)


def _next_inline(tokens, index: int, limit: int = 4):
    """Return the inline token that belongs to the block token at ``index``."""
    for candidate in tokens[index + 1 : index + 1 + limit]:
        if candidate.type == "inline":
            return candidate
    return None


def _footnote_labels(text: str) -> list[str]:
    return sorted(set(FOOTNOTE_REFERENCE_RE.findall(text)))


#: Private-use sentinel that stands in for a note mark while the Markdown is
#: parsed. Markdown would otherwise read a run of asterisks as emphasis and
#: remove it, which destroys the mark of a same-page note.
NOTE_MARK_SENTINEL = "\ue000"
NOTE_MARK_RUN_RE = re.compile(r"(?<!\*)\*{1,4}(?!\*)")


def protect_note_marks(text: str) -> tuple[str, int]:
    """Replace asterisk note marks with a sentinel before Markdown parsing.

    Scanned academic books mark their same-page notes with a per-page
    asterisk series (``*`` .. ``****``). A Markdown parser reads such a run
    as emphasis and drops it, so the mark would be lost from the delivered
    text.

    Only runs that cannot be an emphasis delimiter are protected: a run is
    left to the parser when it opens or closes a valid emphasis span (it is
    preceded by whitespace and followed by text, or the other way round).
    Every other run - the note marks, which follow a term and are followed by
    punctuation or text - is replaced by an equal number of sentinel
    characters; :func:`restore_note_marks` puts the original characters back
    after the parsing, so the delivered text is unchanged.

    Returns the protected text and the number of protected runs.
    """
    if not text:
        return text, 0
    runs = [
        (match.start(), match.end(), match.group(0))
        for match in NOTE_MARK_RUN_RE.finditer(text)
    ]
    if not runs:
        return text, 0

    def is_opening(start: int, end: int) -> bool:
        before = text[start - 1] if start > 0 else ""
        after = text[end] if end < len(text) else ""
        return (not before or before.isspace()) and bool(after) and not after.isspace()

    def is_closing(start: int, end: int) -> bool:
        before = text[start - 1] if start > 0 else ""
        after = text[end] if end < len(text) else ""
        return bool(before) and not before.isspace() and (not after or after.isspace())

    # Pair the runs that can open and close an emphasis span; a run that
    # takes part in such a pair stays with the Markdown parser.
    used = [False] * len(runs)
    for index, (start, end, run) in enumerate(runs):
        if used[index] or not is_closing(start, end):
            continue
        for earlier in range(index - 1, -1, -1):
            if used[earlier]:
                continue
            o_start, o_end, o_run = runs[earlier]
            if len(o_run) != len(run):
                continue
            if not is_opening(o_start, o_end):
                continue
            if not text[o_end:start].strip():
                continue
            used[index] = True
            used[earlier] = True
            break

    parts: list[str] = []
    cursor = 0
    count = 0
    for index, (start, end, run) in enumerate(runs):
        if used[index]:
            continue
        parts.append(text[cursor:start])
        parts.append(NOTE_MARK_SENTINEL * len(run))
        cursor = end
        count += 1
    parts.append(text[cursor:])
    return "".join(parts), count


def restore_note_marks(text: str) -> str:
    """Put the original asterisk characters back after the parsing."""
    if not text:
        return text
    return text.replace(NOTE_MARK_SENTINEL, "*")


def parse_page_markdown(raw_text: str) -> list[ParsedBlock]:
    """Classify OCR Markdown into conservative, evidence-backed blocks.

    The Markdown source is never rewritten: each block keeps the exact source
    slice it came from. Roles come from ``markdown-it`` block tokens only; there
    is no guessing about page furniture and nothing is dropped.

    Asterisk note marks are protected while the Markdown is parsed and
    restored afterwards, because Markdown would otherwise read them as
    emphasis and remove them.
    """
    protected, _marks = protect_note_marks(raw_text)
    blocks = _parse_page_markdown(protected)
    if blocks:
        for block in blocks:
            block.text = restore_note_marks(block.text)
            block.markdown = restore_note_marks(block.markdown)
            block.inline = [
                {
                    **item,
                    "text": restore_note_marks(str(item.get("text") or "")),
                    "content": restore_note_marks(str(item.get("content") or "")),
                }
                for item in block.inline
            ]
    return blocks


def _parse_page_markdown(raw_text: str) -> list[ParsedBlock]:
    if not raw_text.strip():
        return []
    parser = markdown_parser()

    def preserve_footnotes(state, start_line, end_line, silent):
        start = state.bMarks[start_line] + state.tShift[start_line]
        source_line = state.src[start:state.eMarks[start_line]]
        # CommonMark interprets [^id]: text as an ordinary link definition and
        # removes it from block tokens. Footnotes must remain textual evidence.
        if FOOTNOTE_DEFINITION_RE.match(source_line):
            return False
        return parse_reference_definition(state, start_line, end_line, silent)

    parser.block.ruler.at("reference", preserve_footnotes)
    tokens = parser.parse(raw_text)
    lines = raw_text.splitlines()
    blocks: list[ParsedBlock] = []
    consumed: list[tuple[int, int]] = []
    positions: list[int] = []
    list_depth = 0
    footnote_section = False

    def emit(
        role: str,
        evidence: list[str],
        token,
        *,
        text: str = "",
        inline=None,
        heading_level: int | None = None,
        footnote_labels: list[str] | None = None,
        source: str | None = None,
    ) -> None:
        slice_text = _slice_source(lines, token)
        blocks.append(
            ParsedBlock(
                index=len(blocks),
                role=role,
                confidence=ROLE_CONFIDENCE[role],
                evidence=evidence,
                text=text or slice_text,
                markdown=slice_text if source is None else source,
                inline=_inline_records(inline) if inline is not None else [],
                heading_level=heading_level,
                footnote_labels=footnote_labels or [],
            )
        )
        _mark_consumed(consumed, token)
        positions.append(token.map[0] if token.map else len(lines))

    index = 0
    while index < len(tokens):
        token = tokens[index]
        token_type = token.type

        # Nested blocks inside an already emitted container (list item, blockquote,
        # table) are represented by that container and are not emitted twice.
        if _is_consumed(consumed, token):
            index += 1
            continue

        if token_type in {"bullet_list_open", "ordered_list_open"}:
            list_depth += 1
        elif token_type in {"bullet_list_close", "ordered_list_close"}:
            list_depth = max(0, list_depth - 1)
        elif token_type == "heading_open":
            inline = _next_inline(tokens, index)
            text = _plain_text(inline)
            level = int(token.tag[1]) if token.tag[1:].isdigit() else None
            labels = _footnote_labels(text)
            evidence = [f"markdown_heading_token:{token.tag}"]
            if labels:
                evidence.append("footnote_reference_in_text")
            is_section = bool(FOOTNOTE_SECTION_RE.match(text.strip()))
            if is_section:
                evidence.append("footnote_section_heading")
            emit(
                "heading",
                evidence,
                token,
                text=text,
                inline=inline,
                heading_level=level,
                footnote_labels=labels,
            )
            footnote_section = is_section
        elif token_type == "list_item_open":
            inline = _next_inline(tokens, index)
            text = _plain_text(inline)
            labels = _footnote_labels(text)
            evidence = ["markdown_list_item_token"]
            if labels:
                evidence.append("footnote_reference_in_text")
            emit(
                "list_item",
                evidence,
                token,
                text=text,
                inline=inline,
                footnote_labels=labels,
            )
        elif token_type == "paragraph_open":
            inline = _next_inline(tokens, index)
            text = _plain_text(inline)
            labels = _footnote_labels(text)
            evidence = ["markdown_paragraph_token"]
            if labels:
                evidence.append("footnote_reference_in_text")
            definition = FOOTNOTE_DEFINITION_RE.match(text.strip())
            if definition:
                role = "footnote_definition"
                evidence.append("footnote_definition_syntax")
                labels = sorted(set(labels) | {definition.group(1)})
            elif footnote_section:
                role = "footnote"
                evidence.append("inside_footnote_section")
            elif list_depth:
                role = "list_item"
                evidence.append("inside_list")
            else:
                role = "body"
            emit(
                role,
                evidence,
                token,
                text=text,
                inline=inline,
                footnote_labels=labels,
            )
        elif token_type == "table_open":
            emit("table", ["markdown_table_token"], token)
        elif token_type in {"fence", "code_block"}:
            emit(
                "code_block",
                [f"markdown_{token_type}_token"],
                token,
                text=token.content,
                source=_slice_source(lines, token) or token.content,
            )
        elif token_type == "blockquote_open":
            emit("quote", ["markdown_blockquote_token"], token)
        elif token_type == "html_block":
            emit(
                "unknown",
                ["markdown_html_block_token"],
                token,
                text=token.content,
                source=token.content,
            )
        elif token_type == "hr":
            emit("separator", ["markdown_thematic_break_token"], token)
        index += 1

    # Preserve source that produces no tokens, such as ordinary link definitions.
    cursor = 0
    while cursor < len(lines):
        if not lines[cursor].strip() or any(a <= cursor < b for a, b in consumed):
            cursor += 1
            continue
        start = cursor
        while cursor < len(lines) and lines[cursor].strip() and not any(
            a <= cursor < b for a, b in consumed
        ):
            cursor += 1
        source = "\n".join(lines[start:cursor])
        blocks.append(ParsedBlock(index=0, role="unknown", confidence=ROLE_CONFIDENCE["unknown"],
            evidence=["source_lines_without_markdown_tokens"], text=source, markdown=source, inline=[]))
        positions.append(start)
    blocks = [block for _, block in sorted(zip(positions, blocks), key=lambda item: item[0])]
    for index, block in enumerate(blocks):
        block.index = index
    return blocks

# ---------------------------------------------------------------------------
# Ollama transport
# ---------------------------------------------------------------------------


def _ollama_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


def render_raw_prompt(task_prompt: str) -> str:
    """Render the prompt sent to Ollama raw mode.

    Raw mode bypasses the server-side chat template, so the adapter renders the
    canonical Qwen layout itself and closes the assistant turn with an empty think
    block so the model does not start thinking.
    """
    return QWEN_RAW_PROMPT_TEMPLATE.format(prompt=task_prompt)


def fetch_model_metadata(
    base_url: str,
    model: str,
    client: httpx.Client,
    *,
    timeout: float,
) -> dict[str, Any]:
    """Collect the model digest and template from a local Ollama server.

    Both calls are best effort: a server that does not expose them yields an
    ``unknown`` digest with a recorded reason instead of a hard failure.
    """
    metadata: dict[str, Any] = {
        "name": model,
        "digest": "unknown",
        "digest_source": "unavailable",
        "template": None,
        "template_sha256": None,
        "template_excerpt": None,
        "details": None,
        "parameters": None,
        "errors": [],
    }
    try:
        response = client.get(_ollama_url(base_url, "/api/tags"), timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        for entry in payload.get("models", []) or []:
            names = {entry.get("name"), entry.get("model")}
            names.discard(None)
            if model in names or any(name and model.startswith(name) for name in names):
                metadata["digest"] = entry.get("digest") or "unknown"
                metadata["digest_source"] = "api_tags"
                metadata["size_bytes"] = entry.get("size")
                metadata["modified_at"] = entry.get("modified_at")
                break
        else:
            metadata["errors"].append(
                f"model {model!r} not listed by /api/tags; digest unknown"
            )
    except Exception as error:
        metadata["errors"].append(
            f"api_tags failed: {type(error).__name__}: {error}"
        )

    try:
        response = client.post(
            _ollama_url(base_url, "/api/show"), json={"model": model}, timeout=timeout
        )
        response.raise_for_status()
        payload = response.json()
        template = payload.get("template") or ""
        metadata["template"] = template
        metadata["template_sha256"] = _sha256_bytes(template.encode("utf-8"))
        metadata["template_excerpt"] = template[:2000]
        metadata["parameters"] = payload.get("parameters")
        details = payload.get("details") or {}
        metadata["details"] = {
            key: details.get(key)
            for key in (
                "parent_model",
                "format",
                "family",
                "families",
                "parameter_size",
                "quantization_level",
            )
        }
    except Exception as error:
        metadata["errors"].append(
            f"api_show failed: {type(error).__name__}: {error}"
        )
    return metadata


def build_generate_payload(
    *,
    model: str,
    prompt: str,
    image_png: bytes,
    options: OcrOptions,
) -> dict[str, Any]:
    """Build the Ollama ``/api/generate`` payload (raw, non-thinking)."""
    return {
        "model": model,
        "prompt": prompt,
        "images": [base64.b64encode(image_png).decode("ascii")],
        "stream": False,
        "raw": options.raw,
        "think": options.think,
        "options": {
            "num_ctx": OLLAMA_NUM_CTX,
            "num_predict": options.max_tokens,
            "temperature": options.temperature,
            "top_p": options.top_p,
            "repeat_penalty": options.repeat_penalty,
        },
    }


@dataclass
class GenerateOutcome:
    """Outcome of one model call, including failure evidence."""

    status: str
    text: str = ""
    body: bytes = b""
    body_is_json: bool = False
    error: dict[str, Any] | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    latency_seconds: float = 0.0


def call_generate(
    *,
    client: httpx.Client,
    base_url: str,
    payload: dict[str, Any],
    timeout: float,
) -> GenerateOutcome:
    """POST one raw generation request and keep the raw body verbatim."""
    started = time.perf_counter()
    try:
        response = client.post(
            _ollama_url(base_url, "/api/generate"), json=payload, timeout=timeout
        )
    except httpx.TimeoutException as error:
        return GenerateOutcome(
            status="failed",
            error={"kind": "timeout", "message": str(error)},
            latency_seconds=round(time.perf_counter() - started, 3),
        )
    except httpx.HTTPError as error:
        return GenerateOutcome(
            status="failed",
            error={
                "kind": "transport_error",
                "message": f"{type(error).__name__}: {error}",
            },
            latency_seconds=round(time.perf_counter() - started, 3),
        )
    latency = round(time.perf_counter() - started, 3)
    body = response.content
    if response.status_code >= 400:
        return GenerateOutcome(
            status="failed",
            body=body,
            body_is_json=_looks_like_json(body),
            error={
                "kind": "http_error",
                "message": f"HTTP {response.status_code}: {body[:500]!r}",
            },
            latency_seconds=latency,
        )
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return GenerateOutcome(
            status="failed",
            body=body,
            body_is_json=False,
            error={
                "kind": "unparsable_response",
                "message": f"{type(error).__name__}: {error}",
            },
            latency_seconds=latency,
        )
    if not isinstance(data, dict) or not isinstance(data.get("response", ""), str):
        return GenerateOutcome(
            status="failed", body=body, body_is_json=True,
            error={"kind": "invalid_response_schema",
                   "message": "Ollama response must be an object with string response text"},
            latency_seconds=latency,
        )
    text = data.get("response") or ""
    if data.get("error"):
        return GenerateOutcome(
            status="failed",
            text=text,
            body=body,
            body_is_json=True,
            error={
                "kind": "server_error",
                "message": str(data.get("error")),
            },
            latency_seconds=latency,
        )
    usage = {
        "input_tokens": data.get("prompt_eval_count"),
        "output_tokens": data.get("eval_count"),
        "total_duration_ns": data.get("total_duration"),
        "load_duration_ns": data.get("load_duration"),
        "prompt_eval_duration_ns": data.get("prompt_eval_duration"),
        "model_reported": data.get("model"),
        "created_at": data.get("created_at"),
    }
    done_reason = data.get("done_reason")
    if done_reason == "length":
        return GenerateOutcome(
            status="failed",
            text=text,
            body=body,
            body_is_json=True,
            error={
                "kind": "truncated_output",
                "message": (
                    "the model stopped at the output budget (done_reason=length); "
                    "the page is incomplete and cannot be accepted"
                ),
            },
            usage=usage,
            latency_seconds=latency,
        )
    if data.get("done") is not True:
        return GenerateOutcome(
            status="failed",
            text=text,
            body=body,
            body_is_json=True,
            error={
                "kind": "incomplete_response",
                "message": "the response was not marked done; output may be partial",
            },
            usage=usage,
            latency_seconds=latency,
        )
    if not text.strip():
        return GenerateOutcome(
            status="failed",
            text=text,
            body=body,
            body_is_json=True,
            error={
                "kind": "empty_response",
                "message": "the model returned an empty response",
            },
            usage=usage,
            latency_seconds=latency,
        )
    return GenerateOutcome(
        status="ok",
        text=text,
        body=body,
        body_is_json=True,
        usage=usage,
        latency_seconds=latency,
    )


def _looks_like_json(body: bytes) -> bool:
    try:
        json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


# ---------------------------------------------------------------------------
# page records
# ---------------------------------------------------------------------------


def page_id(page_number: int) -> str:
    """Stable page identifier used across all artifacts."""
    return f"page-{page_number:04d}"


def page_signature(
    *,
    options: OcrOptions,
    model_digest: str,
    source_sha256: str,
    image_sha256: str,
    prompt_sha256: str,
) -> dict[str, Any]:
    """Signature that decides whether a stored page response may be reused."""
    return {
        "schema": OCR_PAGE_SCHEMA,
        "model": options.model,
        "model_digest": model_digest,
        "source_sha256": source_sha256,
        "image_sha256": image_sha256,
        "prompt_sha256": prompt_sha256,
        "options": options.signature_payload(),
    }


def _signature_hash(signature: dict[str, Any]) -> str:
    return _sha256_bytes(_canonical_json(signature).encode("utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _read_json(path: Path) -> Any | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# pilot run
# ---------------------------------------------------------------------------


def run_ocr_pilot(
    pdf_path: str | Path,
    output_dir: str | Path,
    pages: Sequence[int],
    *,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    dpi: int = DEFAULT_DPI,
    timeout: float = DEFAULT_TIMEOUT,
    force: bool = False,
    prompt_variant: str = "preservation",
    temperature: float = 0.0,
    top_p: float = 0.8,
    repeat_penalty: float = 1.0,
    client: httpx.Client | None = None,
) -> OcrPilotResult:
    """Run a bounded OCR pilot over an explicit list of pages.

    Parameters
    ----------
    pdf_path:
        Source PDF. It is never modified.
    output_dir:
        Artifact directory. It is created if missing and reused if present so an
        interrupted pilot can be resumed.
    pages:
        Explicit, bounded list of 1-based page numbers. Required: there is no
        full-book mode.
    base_url:
        Loopback Ollama base URL. Non-loopback hosts are rejected.
    model:
        Ollama model tag, for example ``longwayxu/xiaomi-ocr-0:bf16``.
    max_pages:
        Upper bound on the requested page count.
    max_tokens:
        Output budget per page, forwarded as Ollama ``num_predict``.
    dpi:
        Render resolution for the inference input.
    timeout:
        Per-request timeout in seconds.
    force:
        Allow OCR even when the selected pages have a healthy native text layer.
    prompt_variant:
        ``preservation`` (default) keeps all page text; ``upstream`` reproduces the
        upstream prompt byte for byte.
    client:
        Optional ``httpx.Client``. Tests inject a mock transport here.

    Returns
    -------
    OcrPilotResult
        Paths of the written artifacts plus the per-page outcome summary.
    """
    options = _validate_options(
        OcrOptions(
            base_url=base_url,
            model=model,
            max_pages=max_pages,
            max_tokens=max_tokens,
            dpi=dpi,
            timeout=timeout,
            force=force,
            prompt_variant=prompt_variant,
            temperature=temperature,
            top_p=top_p,
            repeat_penalty=repeat_penalty,
        )
    )

    source = Path(pdf_path).expanduser()
    output = Path(output_dir).expanduser()
    if not source.is_file():
        raise OcrValidationError(f"PDF not found: {source}")
    with source.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise OcrValidationError(f"not a PDF file: {source}")
    if output.exists() and not output.is_dir():
        raise OcrValidationError(f"output_dir exists and is not a directory: {output}")
    if output.resolve() == source.resolve():
        raise OcrValidationError("output_dir must not be the source PDF")
    if source.resolve().is_relative_to(output.resolve()):
        raise OcrValidationError("output_dir must not contain the source PDF")

    source_sha256 = _sha256_file(source)
    page_count = _pdf_page_count(source)
    selected = _validate_pages(pages, page_count, options.max_pages)

    native_report = inspect_native_text(source, selected)
    enforce_native_text_policy(native_report, selected, force=options.force)

    # Validation is complete: only now are files created and the API called.
    output.mkdir(parents=True, exist_ok=True)
    renders_dir = output / "renders"
    pages_dir = output / "pages"
    renders_dir.mkdir(parents=True, exist_ok=True)
    pages_dir.mkdir(parents=True, exist_ok=True)

    owns_client = client is None
    active_client = client or httpx.Client(timeout=options.timeout, follow_redirects=False)
    try:
        model_metadata = fetch_model_metadata(
            options.base_url, options.model, active_client, timeout=options.timeout
        )
        model_digest = str(model_metadata.get("digest") or "unknown")
        task_prompt = options.task_prompt()
        rendered_prompt = render_raw_prompt(task_prompt)
        prompt_sha256 = _sha256_bytes(rendered_prompt.encode("utf-8"))

        page_records: list[dict[str, Any]] = []
        telemetry_calls: list[dict[str, Any]] = []

        for page_number in selected:
            try:
                record, call = _process_page(
                    output=output,
                    pages_dir=pages_dir,
                    renders_dir=renders_dir,
                    source=source,
                    source_sha256=source_sha256,
                    page_number=page_number,
                    options=options,
                    model_digest=model_digest,
                    client=active_client,
                    rendered_prompt=rendered_prompt,
                    prompt_sha256=prompt_sha256,
                    native_characters=native_report.per_page_characters.get(page_number, 0),
                )
            except Exception as error:  # keep the pilot going, never lose the page
                record, call = _failed_page(
                    pages_dir=pages_dir,
                    page_number=page_number,
                    source_sha256=source_sha256,
                    native_characters=native_report.per_page_characters.get(page_number, 0),
                    error={
                        "kind": "unexpected_error",
                        "message": f"{type(error).__name__}: {error}",
                    },
                )
            page_records.append(record)
            telemetry_calls.append(call)

        document = _build_document(
            source=source,
            source_sha256=source_sha256,
            page_count=page_count,
            selected=selected,
            options=options,
            model_metadata=model_metadata,
            task_prompt=task_prompt,
            rendered_prompt=rendered_prompt,
            prompt_sha256=prompt_sha256,
            native_report=native_report,
            page_records=page_records,
            telemetry_calls=telemetry_calls,
        )
        document_path = output / "document.json"
        _write_json(document_path, document)

        review = _build_review(
            source=source,
            source_sha256=source_sha256,
            selected=selected,
            options=options,
            model_metadata=model_metadata,
            document=document,
        )
        review_path = output / "review.json"
        _write_json(review_path, review)

        book_path = output / "book.md"
        book_path.write_text(
            _render_book(document, page_records), encoding="utf-8"
        )

        ok_pages = [
            record["page"] for record in page_records if record["status"] == "ok"
        ]
        failed_pages = [
            record["page"] for record in page_records if record["status"] != "ok"
        ]
        return OcrPilotResult(
            output_dir=output,
            document_path=document_path,
            book_path=book_path,
            review_path=review_path,
            pages=selected,
            ok_pages=ok_pages,
            failed_pages=failed_pages,
            model=options.model,
            model_digest=model_digest,
            telemetry=document["telemetry"],
        )
    finally:
        if owns_client:
            active_client.close()


def _process_page(
    *,
    output: Path,
    pages_dir: Path,
    renders_dir: Path,
    source: Path,
    source_sha256: str,
    page_number: int,
    options: OcrOptions,
    model_digest: str,
    client: httpx.Client,
    rendered_prompt: str,
    prompt_sha256: str,
    native_characters: int = 0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Render, maybe resume, maybe call the model, and persist one page."""
    identifier = page_id(page_number)
    image_bytes = prepare_page_image(source, page_number, options.dpi)
    image_sha256 = _sha256_bytes(image_bytes)
    width, height = image_dimensions(image_bytes)
    render_path = renders_dir / f"{identifier}.png"
    render_path.write_bytes(image_bytes)

    signature = page_signature(
        options=options,
        model_digest=model_digest,
        source_sha256=source_sha256,
        image_sha256=image_sha256,
        prompt_sha256=prompt_sha256,
    )
    signature_sha256 = _signature_hash(signature)

    record_path = pages_dir / f"{identifier}.json"
    stored = _read_json(record_path)
    stored_attempts = list(stored.get("attempts", [])) if isinstance(stored, dict) else []

    reusable = (
        isinstance(stored, dict)
        and model_digest not in {None, "", "unknown"}
        and stored.get("status") == "ok"
        and stored.get("signature_sha256") == signature_sha256
        and isinstance(stored.get("blocks"), list)
    )
    if reusable:
        record = dict(stored)
        # Cache inference, then rebuild structure with the current parser so a
        # repaired role/footnote classifier cannot retain stale missing blocks.
        record["blocks"] = [block.as_record(page_number, identifier)
                            for block in parse_page_markdown(record.get("text", ""))]
        record["resumed"] = True
        record["render_path"] = _relative(render_path, output)
        record["image_sha256"] = image_sha256
        record["image_pixels"] = {"width": width, "height": height}
        _write_json(record_path, record)
        return record, {
            "page": page_number,
            "page_id": identifier,
            "attempt": record.get("attempt"),
            "status": "ok",
            "source": "resumed_from_cache",
            "cached": True,
            "input_tokens": record.get("usage", {}).get("input_tokens"),
            "output_tokens": record.get("usage", {}).get("output_tokens"),
            "latency_seconds": 0.0,
            "local_cost_usd": 0.0,
            "external_cost_usd": 0.0,
            "retries": 0,
            "image_pixels": {"width": width, "height": height},
            "error": None,
        }

    attempt = len(stored_attempts) + 1
    payload = build_generate_payload(
        model=options.model,
        prompt=rendered_prompt,
        image_png=image_bytes,
        options=options,
    )
    outcome = call_generate(
        client=client,
        base_url=options.base_url,
        payload=payload,
        timeout=options.timeout,
    )

    raw_response_path: str | None = None
    raw_text_path: str | None = None
    if outcome.body:
        raw_name = (
            f"{identifier}.attempt{attempt}.raw.json"
            if outcome.body_is_json
            else f"{identifier}.attempt{attempt}.raw.txt"
        )
        raw_file = pages_dir / raw_name
        raw_file.write_bytes(outcome.body)
        raw_response_path = _relative(raw_file, output)
    if outcome.text:
        raw_text_file = pages_dir / f"{identifier}.attempt{attempt}.model.txt"
        raw_text_file.write_text(outcome.text, encoding="utf-8")
        raw_text_path = _relative(raw_text_file, output)

    blocks = (
        parse_page_markdown(outcome.text) if outcome.status == "ok" else []
    )
    block_records = [block.as_record(page_number, identifier) for block in blocks]

    attempt_record = {
        "attempt": attempt,
        "status": outcome.status,
        "signature_sha256": signature_sha256,
        "image_sha256": image_sha256,
        "raw_response_path": raw_response_path,
        "raw_text_path": raw_text_path,
        "error": outcome.error,
        "latency_seconds": outcome.latency_seconds,
        "usage": outcome.usage,
    }
    attempts = stored_attempts + [attempt_record]

    record: dict[str, Any] = {
        "schema": OCR_PAGE_SCHEMA,
        "page": page_number,
        "page_id": identifier,
        "status": outcome.status,
        "attempt": attempt,
        "attempts": attempts,
        "resumed": False,
        "source_sha256": source_sha256,
        "image_sha256": image_sha256,
        "image_pixels": {"width": width, "height": height},
        "render_path": _relative(render_path, output),
        "render_role": "inference_evidence",
        "render_included_in_markdown": False,
        "signature": signature,
        "signature_sha256": signature_sha256,
        "raw_response_path": raw_response_path,
        "raw_text_path": raw_text_path,
        "raw_text_sha256": _sha256_bytes(outcome.text.encode("utf-8"))
        if outcome.text
        else None,
        "text": outcome.text if outcome.status == "ok" else "",
        "partial_text_retained": outcome.text if outcome.status != "ok" else "",
        "blocks": block_records,
        "error": outcome.error,
        "usage": outcome.usage,
        "latency_seconds": outcome.latency_seconds,
        "native_text_characters": native_characters,
        "accepted": False,
        "acceptance_note": (
            "OCR output is never auto-accepted; a human must verify every page "
            "against the retained render."
        ),
    }
    _write_json(record_path, record)

    call = {
        "page": page_number,
        "page_id": identifier,
        "attempt": attempt,
        "status": outcome.status,
        "source": "model_call",
        "cached": False,
        "input_tokens": outcome.usage.get("input_tokens"),
        "output_tokens": outcome.usage.get("output_tokens"),
        "latency_seconds": outcome.latency_seconds,
        "total_duration_ns": outcome.usage.get("total_duration_ns"),
        "load_duration_ns": outcome.usage.get("load_duration_ns"),
        "prompt_eval_duration_ns": outcome.usage.get("prompt_eval_duration_ns"),
        "local_cost_usd": 0.0,
        "external_cost_usd": 0.0,
        "retries": 0,
        "image_pixels": {"width": width, "height": height},
        "error": outcome.error,
    }
    return record, call


def _failed_page(
    *,
    pages_dir: Path,
    page_number: int,
    source_sha256: str,
    native_characters: int,
    error: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Record a page that could not be processed at all.

    The page is never accepted and never silently skipped: it becomes a failed
    review entry with the error evidence attached.
    """
    identifier = page_id(page_number)
    record = {
        "schema": OCR_PAGE_SCHEMA,
        "page": page_number,
        "page_id": identifier,
        "status": "failed",
        "attempt": 0,
        "attempts": [],
        "resumed": False,
        "source_sha256": source_sha256,
        "image_sha256": None,
        "image_pixels": None,
        "render_path": None,
        "render_role": "inference_evidence",
        "render_included_in_markdown": False,
        "signature": None,
        "signature_sha256": None,
        "raw_response_path": None,
        "raw_text_path": None,
        "raw_text_sha256": None,
        "text": "",
        "partial_text_retained": "",
        "blocks": [],
        "error": error,
        "usage": {},
        "latency_seconds": 0.0,
        "native_text_characters": native_characters,
        "accepted": False,
        "acceptance_note": (
            "OCR output is never auto-accepted; a human must verify every page "
            "against the retained render."
        ),
    }
    _write_json(pages_dir / f"{identifier}.json", record)
    call = {
        "page": page_number,
        "page_id": identifier,
        "attempt": 0,
        "status": "failed",
        "source": "model_call",
        "cached": False,
        "input_tokens": None,
        "output_tokens": None,
        "latency_seconds": 0.0,
        "local_cost_usd": 0.0,
        "external_cost_usd": 0.0,
        "retries": 0,
        "image_pixels": None,
        "error": error,
    }
    return record, call


def _build_document(
    *,
    source: Path,
    source_sha256: str,
    page_count: int,
    selected: list[int],
    options: OcrOptions,
    model_metadata: dict[str, Any],
    task_prompt: str,
    rendered_prompt: str,
    prompt_sha256: str,
    native_report: NativeTextReport,
    page_records: list[dict[str, Any]],
    telemetry_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    diverges = options.prompt_variant != "upstream"
    blocks: list[dict[str, Any]] = []
    for record in page_records:
        blocks.extend(record.get("blocks", []))

    totals = {
        "calls": len(telemetry_calls),
        "cached_calls": sum(1 for call in telemetry_calls if call.get("cached")),
        "failed_calls": sum(1 for call in telemetry_calls if call.get("status") != "ok"),
        "input_tokens": sum(
            int(call.get("input_tokens") or 0) for call in telemetry_calls
        ),
        "output_tokens": sum(
            int(call.get("output_tokens") or 0) for call in telemetry_calls
        ),
        "latency_seconds": round(
            sum(float(call.get("latency_seconds") or 0.0) for call in telemetry_calls), 3
        ),
        "local_cost_usd": 0.0,
        "external_cost_usd": 0.0,
        "external_api_calls": 0,
        "new_input_tokens": sum(int(call.get("input_tokens") or 0)
                                for call in telemetry_calls if not call.get("cached")),
        "new_output_tokens": sum(int(call.get("output_tokens") or 0)
                                 for call in telemetry_calls if not call.get("cached")),
        "usage_note": "input/output totals include original cached-page usage; new_* count this run only",
    }

    role_counts: dict[str, int] = {}
    for block in blocks:
        role = str(block.get("role"))
        role_counts[role] = role_counts.get(role, 0) + 1

    return {
        "schema": OCR_DOCUMENT_SCHEMA,
        "generated_by": "pdfcracker.ocr",
        "generated_at": _utc_now(),
        "mode": "pilot",
        "source": {
            "path": str(source.resolve()),
            "sha256": source_sha256,
            "page_count": page_count,
            "selected_pages": selected,
            "byte_size": source.stat().st_size,
            "modified": False,
        },
        "model": {
            "provider": "ollama",
            "transport": "http-loopback",
            "base_url": options.base_url,
            "name": options.model,
            "digest": model_metadata.get("digest"),
            "digest_source": model_metadata.get("digest_source"),
            "local": True,
            "external_api_calls": 0,
            "metadata": model_metadata,
        },
        "prompt": {
            "variant": options.prompt_variant,
            "task_prompt": task_prompt,
            "task_prompt_sha256": _sha256_bytes(task_prompt.encode("utf-8")),
            "rendered_prompt": rendered_prompt,
            "rendered_prompt_sha256": prompt_sha256,
            "raw_template": QWEN_RAW_PROMPT_TEMPLATE,
            "think_block": "<think>\n\n</think>\n\n",
            "diverges_from_upstream": diverges,
            "divergence_note": PROMPT_VARIANT_NOTES[options.prompt_variant],
        },
        "options": options.as_provenance(),
        "preprocessing": {
            "render_dpi": options.dpi,
            "render_scale": round(options.dpi / 72, 6),
            "max_render_width": MAX_RENDER_WIDTH,
            "max_image_edge": MAX_IMAGE_EDGE,
            "resample": "LANCZOS",
            "color_mode": "RGB",
            "format": "PNG",
            "renderer": "pypdfium2",
        },
        "native_text": {
            **native_report.as_provenance(),
            "forced": options.force,
            "policy": (
                "OCR is an escalation. A selected slice containing any healthy "
                "native text pages is rejected unless force=True."
            ),
        },
        "pages": page_records,
        "blocks": blocks,
        "role_counts": dict(sorted(role_counts.items())),
        "telemetry": {
            "provider": "ollama",
            "transport": "httpx-http-loopback",
            "base_url": options.base_url,
            "model": options.model,
            "model_digest": model_metadata.get("digest"),
            "options": options.as_provenance(),
            "prompt": {
                "variant": options.prompt_variant,
                "rendered_prompt_sha256": prompt_sha256,
                "diverges_from_upstream": diverges,
                "divergence_note": PROMPT_VARIANT_NOTES[options.prompt_variant],
            },
            "calls": telemetry_calls,
            "totals": totals,
            "notes": [
                "Local loopback inference only; no external API was contacted.",
                "Local monetary cost is zero; cost_usd fields are recorded as 0.0.",
                "Model-reported confidences are telemetry, not acceptance.",
            ],
        },
        "provenance": {
            "artifacts": {
                "document": "document.json",
                "book": "book.md",
                "review": "review.json",
                "renders": "renders/",
                "pages": "pages/",
            },
            "render_role": "inference_evidence",
            "render_note": (
                "Page renders are inference inputs, not extracted content images. "
                "They are never inserted into the Markdown and are never registered "
                "as document images."
            ),
            "preservation": {
                "scope": "engine transformations only; OCR omissions remain unverified",
                "headers_and_footers_deleted": False,
                "cross_page_merges": 0,
                "footnotes_dropped": False,
                "blocks_dropped": 0,
                "content_reinterpreted": False,
            },
        },
        "limits": [
            "OCR output carries no PDF geometry; bbox and font fields are null by design.",
            "Role labels and confidences are Markdown-structure heuristics for triage only.",
            "Failed or truncated pages are never accepted and require human transcription.",
            "The pilot covers only the explicitly listed pages.",
            "The model deployment is still in progress; model digest may be 'unknown'.",
        ],
        "review": {
            "review_path": "review.json",
            "pilot_approved": False,
            "human_review_required": True,
        },
    }


def _build_review(
    *,
    source: Path,
    source_sha256: str,
    selected: list[int],
    options: OcrOptions,
    model_metadata: dict[str, Any],
    document: dict[str, Any],
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    entry_number = 0

    def add_entry(
        *,
        kind: str,
        severity: str,
        message: str,
        page: int | None = None,
        page_identifier: str | None = None,
        evidence: dict[str, Any] | None = None,
        suggested_action: str = "",
    ) -> None:
        nonlocal entry_number
        entry_number += 1
        entries.append(
            {
                "id": f"review-{entry_number:04d}",
                "kind": kind,
                "severity": severity,
                "status": "open",
                "page": page,
                "page_id": page_identifier,
                "message": message,
                "evidence": evidence or {},
                "suggested_action": suggested_action,
            }
        )

    for record in document["pages"]:
        page = record["page"]
        identifier = record["page_id"]
        if record["status"] != "ok":
            error = record.get("error") or {}
            add_entry(
                kind="page_failed",
                severity="high",
                page=page,
                page_identifier=identifier,
                message=(
                    f"page {page} could not be accepted: {error.get('kind', 'unknown')} "
                    f"- {error.get('message', '')}"
                ),
                evidence={
                    "raw_response_path": record.get("raw_response_path"),
                    "raw_text_path": record.get("raw_text_path"),
                    "render_path": record.get("render_path"),
                    "attempts": record.get("attempts", []),
                    "partial_text_retained": bool(record.get("partial_text_retained")),
                },
                suggested_action=(
                    "Transcribe or repair this page manually from the retained render "
                    "and record the correction in the run profile."
                ),
            )
            continue

        native_characters = record.get("native_text_characters")
        if native_characters is None:
            native_characters = document["native_text"]["per_page_characters"].get(
                str(page)
            )
        if native_characters and native_characters >= document["native_text"][
            "threshold_characters"
        ]:
            add_entry(
                kind="ocr_over_native_text",
                severity="medium",
                page=page,
                page_identifier=identifier,
                message=(
                    f"page {page} already has {native_characters} native characters "
                    "and was processed by OCR anyway; compare both readings."
                ),
                evidence={
                    "native_text_characters": native_characters,
                    "render_path": record.get("render_path"),
                },
                suggested_action=(
                    "Prefer the native extraction for this page unless the OCR output "
                    "is demonstrably better."
                ),
            )

        roles = [block.get("role") for block in record.get("blocks", [])]
        unknown_count = sum(1 for role in roles if role == "unknown")
        if unknown_count:
            add_entry(
                kind="unknown_role_blocks",
                severity="low",
                page=page,
                page_identifier=identifier,
                message=(
                    f"page {page} contains {unknown_count} block(s) with an unknown "
                    "role; the source text is preserved verbatim."
                ),
                evidence={"block_ids": [
                    block["id"]
                    for block in record.get("blocks", [])
                    if block.get("role") == "unknown"
                ]},
                suggested_action="Assign the correct role during review.",
            )
        if "table" in roles:
            add_entry(
                kind="table_present",
                severity="info",
                page=page,
                page_identifier=identifier,
                message=(
                    f"page {page} contains a table; the OCR table markup is preserved "
                    "as-is and was not converted or validated here."
                ),
                evidence={"render_path": record.get("render_path")},
                suggested_action="Verify the table against the render.",
            )
        footnote_labels = sorted(
            {
                label
                for block in record.get("blocks", [])
                for label in block.get("footnote_labels", [])
            }
        )
        if footnote_labels:
            add_entry(
                kind="footnote_handling",
                severity="medium",
                page=page,
                page_identifier=identifier,
                message=(
                    f"page {page} carries footnote labels {footnote_labels}; footnotes "
                    "must not be dropped."
                ),
                evidence={"footnote_labels": footnote_labels},
                suggested_action=(
                    "Confirm every footnote label is present in the accepted text."
                ),
            )

    if document["prompt"]["diverges_from_upstream"]:
        add_entry(
            kind="prompt_divergence",
            severity="medium",
            message=(
                "The preservation prompt variant was used; it differs from the "
                "upstream prompt, which asks the model to ignore headers and footers."
            ),
            evidence={
                "prompt_variant": document["prompt"]["variant"],
                "task_prompt_sha256": document["prompt"]["task_prompt_sha256"],
                "divergence_note": document["prompt"]["divergence_note"],
            },
            suggested_action=(
                "Check that headers, footers, page numbers and footnotes are present "
                "in the accepted text."
            ),
        )

    role_counts = document.get("role_counts", {})
    return {
        "schema": OCR_REVIEW_SCHEMA,
        "pilot_approved": False,
        "human_review": {
            "required": True,
            "status": "pending",
            "reviewer": None,
            "reviewed_at": None,
            "instructions": [
                "Compare every page in renders/ against the OCR text in book.md.",
                "Correct roles, footnote handling and reading order in document.json.",
                "Record each correction as a structured decision in the run profile.",
                "Set pilot_approved to true only after every page is verified.",
            ],
        },
        "scope": {
            "source_path": str(source.resolve()),
            "source_sha256": source_sha256,
            "pages": selected,
            "max_pages": options.max_pages,
            "mode": "pilot",
            "model": options.model,
            "model_digest": model_metadata.get("digest"),
        },
        "summary": {
            "pages_requested": len(selected),
            "pages_ok": sum(1 for record in document["pages"] if record["status"] == "ok"),
            "pages_failed": sum(
                1 for record in document["pages"] if record["status"] != "ok"
            ),
            "block_count": len(document.get("blocks", [])),
            "role_counts": role_counts,
            "open_entries": len(entries),
            "severity_counts": {
                severity: sum(1 for entry in entries if entry["severity"] == severity)
                for severity in ("high", "medium", "low", "info")
            },
        },
        "entries": entries,
        "confidence_policy": (
            "Role confidences are Markdown-structure heuristics used for triage "
            "only. They are not acceptance criteria and never replace human review."
        ),
    }


def _render_book(document: dict[str, Any], page_records: list[dict[str, Any]]) -> str:
    """Assemble the human-review Markdown from the raw model text of each page.

    The model output is copied verbatim. Nothing is rewritten, merged across pages
    or deleted; failed pages are marked with an explicit gap notice.
    """
    lines: list[str] = [
        "<!-- pdfcracker-ocr-pilot"
        f" source_sha256={document['source']['sha256']}"
        f" model={document['model']['name']}"
        f" model_digest={document['model']['digest']}"
        f" prompt_variant={document['prompt']['variant']}"
        " pilot_approved=false human_review_required=true -->",
        "<!-- HUMAN REVIEW REQUIRED: this file is an OCR pilot artifact. Every page"
        " must be checked against renders/ before the content is trusted. -->",
        "",
    ]
    for record in page_records:
        identifier = record["page_id"]
        page = record["page"]
        if record["status"] == "ok":
            lines.append(
                f"<!-- ocr-page:{page:04d} page_id={identifier} status=ok"
                f" image_sha256={record.get('image_sha256')}"
                f" blocks={len(record.get('blocks', []))}"
                f" render={record.get('render_path')} -->"
            )
            text = record.get("text") or ""
            if text.strip():
                lines.append(text.rstrip("\n"))
            else:
                lines.append(
                    f"*[OCR gap - page {page} returned no text.]*"
                )
            lines.append("")
            continue

        error = record.get("error") or {}
        lines.append(
            f"<!-- ocr-page:{page:04d} page_id={identifier} status=failed"
            f" kind={error.get('kind', 'unknown')}"
            f" render={record.get('render_path')}"
            f" raw_response={record.get('raw_response_path')}"
            f" raw_text={record.get('raw_text_path')} -->"
        )
        lines.append(
            f"*[OCR gap - page {page} could not be accepted "
            f"({error.get('kind', 'unknown')}). Human transcription required; the raw "
            f"model output is retained in the artifacts.*]"
        )
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def summarize_run(result: OcrPilotResult) -> dict[str, Any]:
    """Return a JSON-friendly summary of a pilot run."""
    return result.as_dict()
