#!/usr/bin/env python3
"""Human-approved full-book OCR runner.

This is the explicit approval wrapper around ``pdfcracker.ocr`` (see AGENTS.md):
``ocr.run_ocr_pilot`` stays bounded, while this script may process a full book
*only* when a human approval file (``human-approval.json``) records
``pilot_approved`` and ``full_book_allowed`` and the source hash matches.

Safety properties
-----------------
* Loopback Ollama only (inherited from ``pdfcracker.ocr``); no external API.
* PDFium rendering is serialized with a lock; only HTTP calls run concurrently.
* ``--max-calls`` hard-caps *actual* HTTP generate calls (metadata calls to
  ``/api/tags`` and ``/api/show`` do not count).
* ``--base-url`` (repeatable) and ``--base-urls`` accept one or more loopback
  endpoints; every endpoint must report the same model digest, each worker
  thread is bound to one endpoint, and every telemetry call records its
  endpoint. Single-endpoint serial operation is the default path.
* Compatible page records/renders from the approved pilot cache are reused by
  COPY; the approved pilot directory is never modified. Already-accepted page
  records already sitting in ``--output/pages`` (including a handoff from
  another runner, and blank pages with ink evidence) are consumed as-is.
* ``--aggregate-only`` rebuilds ``document.json``/``book.raw.md``/``review.json``
  from the page cache without any model call.
* Failed pages are retried at most once; output tokens are raised only for
  truncated pages. Failed, blank and capped pages are never auto-accepted.
* ``progress.json`` is rewritten after every page so an interrupted run leaves
  partial, resumable artifacts behind.
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import itertools
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Sequence

import httpx

import pdfcracker.ocr as ocr


class BookRunError(RuntimeError):
    """Fatal validation or execution error. No artifacts are written."""


# ---------------------------------------------------------------------------
# approval + native-text gates
# ---------------------------------------------------------------------------


def load_approval(path: Path, source_sha256: str) -> dict[str, Any]:
    approval = json.loads(path.read_text(encoding="utf-8"))
    if not approval.get("pilot_approved"):
        raise BookRunError("approval pilot_approved is not true")
    if not approval.get("full_book_allowed"):
        raise BookRunError("approval full_book_allowed is not true")
    if approval.get("source_sha256") != source_sha256:
        raise BookRunError(
            "approval source_sha256 does not match the --pdf file; "
            "the approval belongs to a different document"
        )
    chapter = approval.get("approved_chapter_path")
    if chapter:
        chapter_path = Path(chapter)
        if not chapter_path.is_file():
            raise BookRunError(f"approved chapter missing: {chapter_path}")
        expected = approval.get("approved_chapter_sha256")
        if expected and ocr._sha256_file(chapter_path) != expected:
            raise BookRunError("approved chapter sha256 does not match approval record")
    return approval


def pdfium_native_characters(pdf_path: Path, page_count: int) -> dict[int, int]:
    """Count non-whitespace native characters per page using pdfium textpages."""
    import pypdfium2 as pdfium

    counts: dict[int, int] = {}
    with pdfium.PdfDocument(str(pdf_path)) as document:
        for number in range(1, page_count + 1):
            page = document[number - 1]
            try:
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_range() or ""
                finally:
                    textpage.close()
            finally:
                page.close()
            counts[number] = len("".join(text.split()))
    return counts


# ---------------------------------------------------------------------------
# blank page detection (explicit scan, evidence retained)
# ---------------------------------------------------------------------------

#: A render counts as blank only when even its darkest grayscale pixel is
#: near-white, i.e. there is no ink anywhere. An extrema *spread* test is not
#: enough: a solid-black page also has spread 0 and must NOT be called blank.
BLANK_MIN_GRAY = 250


def is_blank_render(png_bytes: bytes) -> bool:
    """True only when the page has no ink: darkest grayscale pixel >= 250."""
    from PIL import Image

    with Image.open(io.BytesIO(png_bytes)) as image:
        low, _high = image.convert("L").getextrema()
    return low >= BLANK_MIN_GRAY


def blank_page_record(
    *,
    pages_dir: Path,
    renders_dir: Path,
    output: Path,
    source_sha256: str,
    page_number: int,
    options: ocr.OcrOptions,
    model_digest: str,
    prompt_sha256: str,
    native_characters: int,
    png_bytes: bytes,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Retain a record for a scanned-blank page without calling the model."""
    identifier = ocr.page_id(page_number)
    image_sha256 = ocr._sha256_bytes(png_bytes)
    render_path = renders_dir / f"{identifier}.png"
    render_path.write_bytes(png_bytes)
    width, height = ocr.image_dimensions(png_bytes)
    signature = ocr.page_signature(
        options=options,
        model_digest=model_digest,
        source_sha256=source_sha256,
        image_sha256=image_sha256,
        prompt_sha256=prompt_sha256,
    )
    record: dict[str, Any] = {
        "schema": ocr.OCR_PAGE_SCHEMA,
        "page": page_number,
        "page_id": identifier,
        "status": "ok",
        "attempt": 0,
        "attempts": [],
        "resumed": False,
        "source_sha256": source_sha256,
        "image_sha256": image_sha256,
        "image_pixels": {"width": width, "height": height},
        "render_path": ocr._relative(render_path, output),
        "render_role": "inference_evidence",
        "render_included_in_markdown": False,
        "signature": signature,
        "signature_sha256": ocr._signature_hash(signature),
        "raw_response_path": None,
        "raw_text_path": None,
        "raw_text_sha256": None,
        "text": "",
        "partial_text_retained": "",
        "blocks": [],
        "error": None,
        "usage": {},
        "latency_seconds": 0.0,
        "native_text_characters": native_characters,
        "blank_page": True,
        "blank_evidence": {
            "detector": "render_grayscale_min_pixel",
            "min_gray_at_or_above": BLANK_MIN_GRAY,
            "native_text_characters": 0,
        },
        "accepted": False,
        "acceptance_note": (
            "Blank scanned page retained without a model call; a human must "
            "confirm the page is truly blank."
        ),
    }
    ocr._write_json(pages_dir / f"{identifier}.json", record)
    call = {
        "page": page_number,
        "page_id": identifier,
        "attempt": 0,
        "status": "ok",
        "source": "blank_page_skipped",
        "cached": False,
        "input_tokens": 0,
        "output_tokens": 0,
        "latency_seconds": 0.0,
        "local_cost_usd": 0.0,
        "external_cost_usd": 0.0,
        "retries": 0,
        "image_pixels": {"width": width, "height": height},
        "error": None,
    }
    return record, call


# ---------------------------------------------------------------------------
# pilot cache reuse (copy only, never modify the approved pilot)
# ---------------------------------------------------------------------------


def copy_compatible_pilot_pages(
    *,
    pilot_cache: Path | None,
    pages_dir: Path,
    renders_dir: Path,
    target_pages: Sequence[int],
    options: ocr.OcrOptions,
    model_digest: str,
    source_sha256: str,
    prompt_sha256: str,
) -> list[int]:
    """Copy pilot page records whose signature still matches; return page numbers."""
    reused: list[int] = []
    if pilot_cache is None:
        return reused
    pilot_pages = pilot_cache / "pages"
    pilot_renders = pilot_cache / "renders"
    if not pilot_pages.is_dir():
        raise BookRunError(f"pilot cache has no pages/ directory: {pilot_cache}")
    for page_number in target_pages:
        identifier = ocr.page_id(page_number)
        stored = ocr._read_json(pilot_pages / f"{identifier}.json")
        render_file = pilot_renders / f"{identifier}.png"
        if (
            not isinstance(stored, dict)
            or stored.get("status") != "ok"
            or stored.get("source_sha256") != source_sha256
            or not render_file.is_file()
        ):
            continue
        signature = stored.get("signature") or {}
        if (
            signature.get("model") != options.model
            or signature.get("model_digest") != model_digest
            or model_digest in {None, "", "unknown"}
            or signature.get("prompt_sha256") != prompt_sha256
            or signature.get("options") != options.signature_payload()
            or signature.get("image_sha256") != ocr._sha256_bytes(render_file.read_bytes())
            or ocr._signature_hash(signature) != stored.get("signature_sha256")
        ):
            continue
        # Signature is identical: the copy is byte-for-byte the approved evidence.
        renders_dir.mkdir(parents=True, exist_ok=True)
        (renders_dir / f"{identifier}.png").write_bytes(render_file.read_bytes())
        ocr._write_json(pages_dir / f"{identifier}.json", stored)
        reused.append(page_number)
    return reused


# ---------------------------------------------------------------------------
# full-book run
# ---------------------------------------------------------------------------


def resolve_endpoints(base_url: str | Sequence[str] | None) -> list[str]:
    """Normalize one or more loopback Ollama endpoints, dedup preserving order."""
    raw = [base_url] if isinstance(base_url, (str, type(None))) else list(base_url)
    endpoints: list[str] = []
    for entry in raw:
        for part in str(entry).split(","):
            part = part.strip()
            if part and part not in endpoints:
                endpoints.append(ocr.validate_loopback_base_url(part))
    return endpoints or [ocr.DEFAULT_BASE_URL]


def run_approved_book(
    *,
    pdf_path: str | Path,
    approval_path: str | Path,
    output_dir: str | Path,
    pilot_cache: str | Path | None,
    workers: int = 2,
    dpi: int = 192,
    max_tokens: int = 4096,
    max_calls: int = 300,
    base_url: str | Sequence[str] = ocr.DEFAULT_BASE_URL,
    aggregate_only: bool = False,
    model: str = ocr.DEFAULT_MODEL,
    timeout: float = ocr.DEFAULT_TIMEOUT,
    prompt_variant: str = "preservation",
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    source = Path(pdf_path).expanduser()
    output = Path(output_dir).expanduser()
    if not source.is_file():
        raise BookRunError(f"PDF not found: {source}")
    with source.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise BookRunError(f"not a PDF file: {source}")
    if output.exists() and not output.is_dir():
        raise BookRunError(f"output_dir exists and is not a directory: {output}")

    source_sha256 = ocr._sha256_file(source)
    approval = load_approval(Path(approval_path).expanduser(), source_sha256)
    max_pages = int(approval.get("max_pages") or 500)
    page_count = ocr._pdf_page_count(source)

    target_pages = list(range(1, min(page_count, max_pages) + 1))
    # Check the approved OCR scope before writing any page records.
    native_counts = pdfium_native_characters(source, page_count)
    healthy = [page for page in target_pages if native_counts[page] >= ocr.NATIVE_HEALTHY_CHARS]
    if healthy:
        raise BookRunError(
            f"pages {healthy[:20]} have a healthy native text layer "
            f"(>= {ocr.NATIVE_HEALTHY_CHARS} chars); use native extraction, not full-book OCR"
        )
    endpoints = resolve_endpoints(base_url)

    options = ocr._validate_options(
        ocr.OcrOptions(
            base_url=endpoints[0],
            model=model,
            max_pages=max_pages,
            max_tokens=max_tokens,
            dpi=dpi,
            timeout=timeout,
            prompt_variant=prompt_variant,
        )
    )
    task_prompt = options.task_prompt()
    rendered_prompt = ocr.render_raw_prompt(task_prompt)
    prompt_sha256 = ocr._sha256_bytes(rendered_prompt.encode("utf-8"))

    output.mkdir(parents=True, exist_ok=True)
    renders_dir = output / "renders"
    pages_dir = output / "pages"
    renders_dir.mkdir(parents=True, exist_ok=True)
    pages_dir.mkdir(parents=True, exist_ok=True)

    owns_client = client is None
    active_client = client or httpx.Client(timeout=options.timeout, follow_redirects=False)

    # PDFium is not thread-safe across concurrent calls: serialize rendering
    # with a lock. Network requests to /api/generate stay concurrent.
    render_lock = threading.Lock()
    render_cache: dict[int, bytes] = {}
    original_prepare = ocr.prepare_page_image

    def locked_prepare(pdf: Path, page_number: int, prepare_dpi: int) -> bytes:
        with render_lock:
            return original_prepare(pdf, page_number, prepare_dpi)

    def cached_prepare(pdf: Path, page_number: int, prepare_dpi: int) -> bytes:
        # Always preceded by locked_prepare in the same worker: no pdfium here.
        return render_cache[page_number]

    ocr.prepare_page_image = cached_prepare

    call_lock = threading.Lock()
    call_state = {"started": 0}

    class _MaxCallsExceeded(Exception):
        pass

    original_call = ocr.call_generate

    def counting_call(**kwargs: Any) -> ocr.GenerateOutcome:
        with call_lock:
            if call_state["started"] >= max_calls:
                raise _MaxCallsExceeded(f"max_calls budget {max_calls} exhausted")
            call_state["started"] += 1
        return original_call(**kwargs)

    ocr.call_generate = counting_call

    # Each worker thread is bound to exactly one endpoint, round-robin, so two
    # independent local servers can be driven concurrently if desired.
    endpoint_local = threading.local()
    endpoint_counter = itertools.count()
    endpoint_assign_lock = threading.Lock()

    def worker_endpoint() -> str:
        if not hasattr(endpoint_local, "url"):
            with endpoint_assign_lock:
                endpoint_local.url = endpoints[next(endpoint_counter) % len(endpoints)]
        return endpoint_local.url

    def process_page(page_number: int, active_options: ocr.OcrOptions):
        endpoint = worker_endpoint()
        page_options = dataclasses.replace(active_options, base_url=endpoint)
        try:
            png = render_cache.get(page_number) or locked_prepare(
                source, page_number, page_options.dpi
            )
            render_cache[page_number] = png
            # A page may only be declared blank with BOTH evidence kinds:
            # the pdfium text scan found no text AND the render has no ink.
            if native_counts.get(page_number, 0) == 0 and is_blank_render(png):
                result = blank_page_record(
                    pages_dir=pages_dir,
                    renders_dir=renders_dir,
                    output=output,
                    source_sha256=source_sha256,
                    page_number=page_number,
                    options=page_options,
                    model_digest=model_digest,
                    prompt_sha256=prompt_sha256,
                    native_characters=0,
                    png_bytes=png,
                )
            else:
                result = ocr._process_page(
                    output=output,
                    pages_dir=pages_dir,
                    renders_dir=renders_dir,
                    source=source,
                    source_sha256=source_sha256,
                    page_number=page_number,
                    options=page_options,
                    model_digest=model_digest,
                    client=active_client,
                    rendered_prompt=rendered_prompt,
                    prompt_sha256=prompt_sha256,
                    native_characters=native_counts.get(page_number, 0),
                )
        except _MaxCallsExceeded as error:
            result = ocr._failed_page(
                pages_dir=pages_dir,
                page_number=page_number,
                source_sha256=source_sha256,
                native_characters=native_counts.get(page_number, 0),
                error={"kind": "max_calls_cap", "message": str(error)},
            )
        except Exception as error:  # keep the book going, never lose the page
            result = ocr._failed_page(
                pages_dir=pages_dir,
                page_number=page_number,
                source_sha256=source_sha256,
                native_characters=native_counts.get(page_number, 0),
                error={"kind": "unexpected_error", "message": f"{type(error).__name__}: {error}"},
            )
        record, call = result
        call["endpoint"] = endpoint
        return record, call

    progress_path = output / "progress.json"
    page_records: dict[int, dict[str, Any]] = {}
    telemetry_calls: list[dict[str, Any]] = []
    interrupted = False
    try:
        model_metadata: dict[str, Any] = {}
        endpoint_digests: dict[str, str] = {}
        for endpoint in endpoints:
            metadata = ocr.fetch_model_metadata(
                endpoint, options.model, active_client, timeout=options.timeout
            )
            endpoint_digests[endpoint] = str(metadata.get("digest") or "unknown")
            model_metadata = metadata
        known_digests = {d for d in endpoint_digests.values() if d != "unknown"}
        if len(known_digests) > 1:
            raise BookRunError(
                f"endpoints serve different model digests: {endpoint_digests}"
            )
        model_digest = str(model_metadata.get("digest") or "unknown")

        reused = copy_compatible_pilot_pages(
            pilot_cache=Path(pilot_cache).expanduser() if pilot_cache else None,
            pages_dir=pages_dir,
            renders_dir=renders_dir,
            target_pages=target_pages,
            options=options,
            model_digest=model_digest,
            source_sha256=source_sha256,
            prompt_sha256=prompt_sha256,
        )
        for page_number in reused:
            record = ocr._read_json(pages_dir / f"{ocr.page_id(page_number)}.json")
            page_records[page_number] = record
            telemetry_calls.append({
                "page": page_number,
                "page_id": ocr.page_id(page_number),
                "attempt": record.get("attempt"),
                "status": "ok",
                "source": "resumed_from_pilot_cache",
                "cached": True,
                "input_tokens": (record.get("usage") or {}).get("input_tokens"),
                "output_tokens": (record.get("usage") or {}).get("output_tokens"),
                "latency_seconds": 0.0,
                "local_cost_usd": 0.0,
                "external_cost_usd": 0.0,
                "retries": 0,
                "image_pixels": record.get("image_pixels"),
                "error": None,
            })

        # Consume already-accepted page records from a previous run (including
        # handoff from another runner): ok records — blank pages included — are
        # reused as-is, without re-rendering or re-calling the model. Failed
        # records are left for reprocessing, which appends attempts.
        for page_number in target_pages:
            if page_number in page_records:
                continue
            stored = ocr._read_json(pages_dir / f"{ocr.page_id(page_number)}.json")
            if isinstance(stored, dict) and stored.get("status") == "ok":
                page_records[page_number] = stored
                telemetry_calls.append({
                    "page": page_number,
                    "page_id": ocr.page_id(page_number),
                    "attempt": stored.get("attempt"),
                    "status": "ok",
                    "source": "resumed_from_page_cache",
                    "cached": True,
                    "input_tokens": (stored.get("usage") or {}).get("input_tokens"),
                    "output_tokens": (stored.get("usage") or {}).get("output_tokens"),
                    "latency_seconds": 0.0,
                    "local_cost_usd": 0.0,
                    "external_cost_usd": 0.0,
                    "retries": 0,
                    "image_pixels": stored.get("image_pixels"),
                    "error": None,
                    "endpoint": None,
                })

        pending = [page for page in target_pages if page not in page_records]

        def write_progress() -> None:
            done = sorted(page_records)
            ocr._write_json(progress_path, {
                "updated_at": ocr._utc_now(),
                "total_pages": len(target_pages),
                "completed_pages": done,
                "failed_pages": [p for p in done if page_records[p].get("status") != "ok"],
                "blank_pages": [p for p in done if page_records[p].get("blank_page")],
                "http_calls": call_state["started"],
                "max_calls": max_calls,
                "telemetry": telemetry_calls,
            })

        def run_pass(pages: list[int], active_options: ocr.OcrOptions, label: str) -> None:
            nonlocal interrupted
            if not pages:
                return
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                futures = {pool.submit(process_page, p, active_options): p for p in pages}
                for future in as_completed(futures):
                    page_number = futures[future]
                    try:
                        record, call = future.result()
                    except Exception as error:  # defensive: process_page already catches
                        record, call = ocr._failed_page(
                            pages_dir=pages_dir,
                            page_number=page_number,
                            source_sha256=source_sha256,
                            native_characters=native_counts.get(page_number, 0),
                            error={"kind": "unexpected_error",
                                   "message": f"{type(error).__name__}: {error}"},
                        )
                    page_records[page_number] = record
                    telemetry_calls.append(call)
                    status = record.get("status")
                    print(f"[{label} {len(page_records)}/{len(target_pages)}] "
                          f"page {page_number}: {status} "
                          f"(http_calls={call_state['started']}/{max_calls})", flush=True)
                    write_progress()

        try:
            if not aggregate_only:
                run_pass(pending, options, "page")
                # One bounded retry for failed pages; raise tokens only when truncated.
                with call_lock:
                    budget_left = call_state["started"] < max_calls
                retry_pages = sorted(
                    page for page, record in page_records.items()
                    if record.get("status") != "ok"
                    and int(record.get("attempt") or 0) < 2
                    and (record.get("error") or {}).get("kind") != "max_calls_cap"
                ) if budget_left else []
                if retry_pages:
                    print(f"retrying {len(retry_pages)} failed page(s) once", flush=True)
                    for page_number in retry_pages:
                        record = page_records[page_number]
                        if (record.get("error") or {}).get("kind") == "truncated_output":
                            retry_options = ocr._validate_options(ocr.OcrOptions(
                                **{**options.__dict__, "max_tokens": options.max_tokens * 2}
                            ))
                        else:
                            retry_options = options
                        run_pass([page_number], retry_options, "retry")
        except KeyboardInterrupt:
            interrupted = True
            print("interrupted: keeping partial artifacts", flush=True)

        # Pages never attempted (interruption or cap) keep an honest record.
        for page_number in target_pages:
            if page_number not in page_records:
                record, call = ocr._failed_page(
                    pages_dir=pages_dir,
                    page_number=page_number,
                    source_sha256=source_sha256,
                    native_characters=native_counts.get(page_number, 0),
                    error={"kind": "interrupted" if interrupted else "not_attempted",
                           "message": "page was never attempted by this run"},
                )
                page_records[page_number] = record
                telemetry_calls.append(call)
        write_progress()

        ordered_pages = [page_records[p] for p in target_pages]
        native_report = ocr.NativeTextReport(
            per_page_characters={p: native_counts.get(p, 0) for p in target_pages},
            healthy_pages=[],
            method="pdfium",
        )
        document = ocr._build_document(
            source=source,
            source_sha256=source_sha256,
            page_count=page_count,
            selected=target_pages,
            options=options,
            model_metadata=model_metadata,
            task_prompt=task_prompt,
            rendered_prompt=rendered_prompt,
            prompt_sha256=prompt_sha256,
            native_report=native_report,
            page_records=ordered_pages,
            telemetry_calls=telemetry_calls,
        )
        document["mode"] = "full_book"
        document["model"]["endpoints"] = endpoint_digests
        document["pilot_approval"] = {
            "approval_file": str(Path(approval_path).expanduser()),
            **approval,
        }
        document["limits"].append(
            "Full-book run under human approval; output is NOT auto-approved and "
            "requires the same page-by-page review as the pilot."
        )
        document_path = output / "document.json"
        ocr._write_json(document_path, document)

        review = ocr._build_review(
            source=source,
            source_sha256=source_sha256,
            selected=target_pages,
            options=options,
            model_metadata=model_metadata,
            document=document,
        )
        review["scope"]["mode"] = "full_book"
        blank_pages = [p for p in target_pages if page_records[p].get("blank_page")]
        for page_number in blank_pages:
            review["entries"].append({
                "id": f"review-blank-{ocr.page_id(page_number)}",
                "kind": "blank_page_detected",
                "severity": "low",
                "status": "open",
                "page": page_number,
                "page_id": ocr.page_id(page_number),
                "message": (
                    f"page {page_number} was detected as a scanned blank page and "
                    "skipped without a model call; confirm it is truly blank."
                ),
                "evidence": {
                    "render_path": page_records[page_number].get("render_path"),
                    **page_records[page_number].get("blank_evidence", {}),
                },
                "suggested_action": "Verify against the render that the page is blank.",
            })
        review_path = output / "review.json"
        ocr._write_json(review_path, review)

        book_path = output / "book.raw.md"
        book_path.write_text(ocr._render_book(document, ordered_pages), encoding="utf-8")

        failed_pages = sorted(p for p in target_pages if page_records[p].get("status") != "ok")
        return {
            "document_path": str(document_path),
            "book_path": str(book_path),
            "review_path": str(review_path),
            "progress_path": str(progress_path),
            "pages": target_pages,
            "ok_pages": sorted(p for p in target_pages if page_records[p].get("status") == "ok"),
            "failed_pages": failed_pages,
            "blank_pages": sorted(blank_pages),
            "reused_pilot_pages": reused,
            "http_calls": call_state["started"],
            "interrupted": interrupted,
            "telemetry": document["telemetry"],
        }
    finally:
        ocr.prepare_page_image = original_prepare
        ocr.call_generate = original_call
        render_cache.clear()
        if owns_client:
            active_client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pdf", required=True, help="source PDF (never modified)")
    parser.add_argument("--approval", required=True, help="human-approval.json path")
    parser.add_argument("--output", required=True, help="full-book artifact directory")
    parser.add_argument("--pilot-cache", default=None,
                        help="approved pilot run dir whose compatible pages are copied")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--dpi", type=int, default=192)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--max-calls", type=int, default=300,
                        help="hard cap on actual HTTP /api/generate calls")
    parser.add_argument("--base-url", action="append", default=None,
                        help="loopback Ollama endpoint; repeatable")
    parser.add_argument("--base-urls", default=None,
                        help="comma-separated loopback Ollama endpoints")
    parser.add_argument("--aggregate-only", action="store_true",
                        help="only consume the existing page cache and rebuild "
                             "document.json/book.raw.md/review.json; no model calls")
    parser.add_argument("--model", default=ocr.DEFAULT_MODEL)
    parser.add_argument("--timeout", type=float, default=ocr.DEFAULT_TIMEOUT)
    parser.add_argument("--prompt-variant", default="preservation",
                        choices=sorted(ocr.PROMPT_VARIANTS))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_urls = list(args.base_url or [])
    if args.base_urls:
        base_urls.extend(args.base_urls.split(","))
    started = time.perf_counter()
    try:
        summary = run_approved_book(
            pdf_path=args.pdf,
            approval_path=args.approval,
            output_dir=args.output,
            pilot_cache=args.pilot_cache,
            workers=args.workers,
            dpi=args.dpi,
            max_tokens=args.max_tokens,
            max_calls=args.max_calls,
            base_url=base_urls or ocr.DEFAULT_BASE_URL,
            aggregate_only=args.aggregate_only,
            model=args.model,
            timeout=args.timeout,
            prompt_variant=args.prompt_variant,
        )
    except (BookRunError, ocr.OcrValidationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    elapsed = round(time.perf_counter() - started, 1)
    print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
    print(f"full-book OCR finished in {elapsed}s: "
          f"{len(summary['ok_pages'])} ok, {len(summary['failed_pages'])} failed, "
          f"{summary['http_calls']} HTTP calls. Review is NOT auto-approved; "
          f"verify every page against renders/ before acceptance.", flush=True)
    return 130 if summary["interrupted"] else 0


if __name__ == "__main__":
    sys.exit(main())
