"""Tests for the local Xiaomi-OCR-0 pilot adapter.

No live model is contacted: every HTTP interaction goes through an
``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from pdfcracker.ocr import (
    DEFAULT_MODEL,
    OCR_DOCUMENT_SCHEMA,
    OCR_REVIEW_SCHEMA,
    PRESERVATION_DOCUMENT_PROMPT,
    UPSTREAM_DOCUMENT_PROMPT,
    OcrValidationError,
    NativeTextReport,
    build_generate_payload,
    inspect_native_text,
    enforce_native_text_policy,
    parse_page_markdown,
    render_raw_prompt,
    run_ocr_pilot,
    summarize_run,
    validate_loopback_base_url,
)




PAGE_MARKDOWN = (
    "# Chapter One\n"
    "\n"
    "Body paragraph with a footnote marker[^1].\n"
    "\n"
    "## Section 1.1\n"
    "\n"
    "- first item\n"
    "- second item\n"
    "\n"
    "Running header text\n"
)


@pytest.mark.parametrize("payload", [[], None, {"response": {}, "done": True}, {"response": "partial"}])
def test_invalid_response_schema_is_failed_and_raw_evidence_survives(tmp_path: Path, payload) -> None:
    def handler(request):
        if request.url.path == "/api/generate":
            return httpx.Response(200, content=json.dumps(payload).encode())
        return default_handler(request)

    result = run_ocr_pilot(write_blank_pdf(tmp_path / "source.pdf"), tmp_path / "run", [1],
                           client=make_client(handler))
    assert result.failed_pages == [1]
    record = json.loads((tmp_path / "run/pages/page-0001.json").read_text())
    raw = tmp_path / "run" / record["raw_response_path"]
    assert json.loads(raw.read_text()) == payload


def test_unknown_model_digest_never_reuses_a_previous_response(tmp_path: Path) -> None:
    generations = []

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        if request.url.path == "/api/generate":
            generations.append(request)
        return default_handler(request)

    pdf = write_blank_pdf(tmp_path / "source.pdf")
    for _ in range(2):
        run_ocr_pilot(pdf, tmp_path / "run", [1], client=make_client(handler))
    assert len(generations) == 2


TEXT_BODY = (
    "Hello OCR world. This page carries a healthy native text layer with enough "
    "characters for the escalation policy to detect it and to refuse an OCR run "
    "unless the caller forces one. The paragraph repeats some words so that the "
    "character count clearly exceeds the healthy threshold used by the adapter "
    "and the page is treated as native text rather than as a scanned page."
)


def test_mixed_native_slice_requires_explicit_override() -> None:
    report = NativeTextReport(per_page_characters={1: 300, 2: 0}, healthy_pages=[1])
    with pytest.raises(OcrValidationError, match="healthy native text"):
        enforce_native_text_policy(report, [1, 2], force=False)
    enforce_native_text_policy(report, [1, 2], force=True)


def test_footnote_definition_survives_commonmark_link_parsing() -> None:
    blocks = parse_page_markdown("正文[^n]\n\n[^n]: 注释\n")
    assert [b.role for b in blocks] == ["body", "footnote_definition"]
    assert blocks[0].footnote_labels == ["n"]
    assert blocks[1].markdown == "[^n]: 注释"
    assert blocks[1].footnote_labels == ["n"]


def test_reference_definitions_remain_in_sidecar_source_order() -> None:
    blocks = parse_page_markdown("正文 [链接][target]\n\n[target]: https://example.com\n\n尾段")
    assert [b.role for b in blocks] == ["body", "unknown", "body"]
    assert blocks[1].markdown == "[target]: https://example.com"


def test_failed_native_inspection_does_not_imply_a_scanned_page() -> None:
    report = NativeTextReport(error="parser failed")
    with pytest.raises(OcrValidationError, match="inspection failed"):
        enforce_native_text_policy(report, [1], force=False)


def _text_pdf_bytes() -> bytes:
    stream = f"BT /F1 12 Tf 72 700 Td ({TEXT_BODY}) Tj ET".encode("ascii")
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Resources<</Font<</F1 5 0 R>>>>"
        b"/Contents 4 0 R>>",
        b"<</Length "
        + str(len(stream)).encode("ascii")
        + b">>stream\n"
        + stream
        + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    for number, body in enumerate(objects, start=1):
        output += str(number).encode("ascii") + b" 0 obj" + body + b"endobj\n"
    output += b"trailer<</Root 1 0 R/Size 6>>\n"
    return bytes(output)


def write_text_pdf(path: Path) -> Path:
    path.write_bytes(_text_pdf_bytes())
    return path


def write_blank_pdf(path: Path, pages: int = 2) -> Path:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def make_client(
    handler,
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def default_handler(
    request: httpx.Request,
    *,
    markdown: str = PAGE_MARKDOWN,
    calls: list[httpx.Request] | None = None,
    generate_response: dict | None = None,
) -> httpx.Response:
    if calls is not None:
        calls.append(request)
    path = request.url.path
    if path == "/api/tags":
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": DEFAULT_MODEL,
                        "model": DEFAULT_MODEL,
                        "digest": "sha256:0123456789abcdef",
                        "size": 1800000000,
                    }
                ]
            },
        )
    if path == "/api/show":
        return httpx.Response(
            200,
            json={
                "template": "{{ .Prompt }}",
                "details": {"family": "qwen3vl", "parameter_size": "8B"},
                "parameters": "stop=<|im_end|>",
            },
        )
    if path == "/api/generate":
        body = generate_response or {
            "model": DEFAULT_MODEL,
            "created_at": "2026-01-01T00:00:00Z",
            "response": markdown,
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 1234,
            "eval_count": 567,
            "total_duration": 2_500_000_000,
            "load_duration": 100_000_000,
            "prompt_eval_duration": 900_000_000,
        }
        return httpx.Response(200, json=body)
    raise AssertionError(f"unexpected request: {request.method} {request.url}")


def pilot_client(
    *,
    markdown: str = PAGE_MARKDOWN,
    generate_response: dict | None = None,
    calls: list[httpx.Request] | None = None,
) -> httpx.Client:
    return make_client(
        lambda request: default_handler(
            request,
            markdown=markdown,
            calls=calls,
            generate_response=generate_response,
        )
    )


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_loopback_base_url_is_required() -> None:
    assert validate_loopback_base_url("http://localhost:11434") == "http://localhost:11434"
    assert validate_loopback_base_url("http://127.0.0.1:11434/") == "http://127.0.0.1:11434"
    with pytest.raises(OcrValidationError):
        validate_loopback_base_url("http://example.com:11434")
    with pytest.raises(OcrValidationError):
        validate_loopback_base_url("https://localhost:11434")


def test_external_base_url_is_rejected_before_any_file_is_written(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf")
    output = tmp_path / "run"
    with pytest.raises(OcrValidationError):
        run_ocr_pilot(
            pdf,
            output,
            [1],
            base_url="http://ocr.example.com:11434",
            client=pilot_client(),
        )
    assert not output.exists()


def test_pages_must_be_explicit_and_bounded(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf")
    output = tmp_path / "run"
    with pytest.raises(OcrValidationError):
        run_ocr_pilot(pdf, output, [], client=pilot_client())
    with pytest.raises(OcrValidationError):
        run_ocr_pilot(pdf, output, None, client=pilot_client())  # type: ignore[arg-type]
    assert not output.exists()


def test_page_numbers_are_validated_against_the_document(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf", pages=2)
    output = tmp_path / "run"
    with pytest.raises(OcrValidationError):
        run_ocr_pilot(pdf, output, [3], client=pilot_client())
    with pytest.raises(OcrValidationError):
        run_ocr_pilot(pdf, output, [1, 1], client=pilot_client())
    assert not output.exists()


def test_max_pages_cap_is_enforced(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf", pages=4)
    output = tmp_path / "run"
    with pytest.raises(OcrValidationError):
        run_ocr_pilot(pdf, output, [1, 2, 3], max_pages=2, client=pilot_client())
    assert not output.exists()


def test_healthy_native_text_pdf_is_rejected_without_force(tmp_path: Path) -> None:
    pdf = write_text_pdf(tmp_path / "text.pdf")
    output = tmp_path / "run"
    with pytest.raises(OcrValidationError) as error:
        run_ocr_pilot(pdf, output, [1], client=pilot_client())
    assert "native text layer" in str(error.value)
    assert not output.exists()


def test_force_allows_ocr_over_a_native_text_pdf(tmp_path: Path) -> None:
    pdf = write_text_pdf(tmp_path / "text.pdf")
    output = tmp_path / "run"
    result = run_ocr_pilot(pdf, output, [1], force=True, client=pilot_client())
    assert result.ok_pages == [1]
    document = json.loads(result.document_path.read_text(encoding="utf-8"))
    assert document["native_text"]["forced"] is True
    assert document["native_text"]["healthy_pages"] == ["1"] or document["native_text"][
        "healthy_pages"
    ] == [1]


def test_native_text_inspection_reports_characters(tmp_path: Path) -> None:
    pdf = write_text_pdf(tmp_path / "text.pdf")
    report = inspect_native_text(pdf, [1])
    assert report.per_page_characters[1] > 0
    assert report.healthy_pages == [1]


def test_source_pdf_is_never_modified(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf")
    before = pdf.read_bytes()
    run_ocr_pilot(pdf, tmp_path / "run", [1], client=pilot_client())
    assert pdf.read_bytes() == before


# ---------------------------------------------------------------------------
# successful pilot
# ---------------------------------------------------------------------------


def test_successful_pilot_writes_reviewable_artifacts(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf", pages=2)
    output = tmp_path / "run"
    calls: list[httpx.Request] = []
    result = run_ocr_pilot(pdf, output, [1, 2], client=pilot_client(calls=calls))

    assert result.ok_pages == [1, 2]
    assert result.failed_pages == []
    assert result.book_path.is_file()
    assert result.review_path.is_file()

    document = json.loads(result.document_path.read_text(encoding="utf-8"))
    assert document["schema"] == OCR_DOCUMENT_SCHEMA
    assert document["mode"] == "pilot"
    assert document["source"]["selected_pages"] == [1, 2]
    assert document["model"]["digest"] == "sha256:0123456789abcdef"
    assert document["model"]["external_api_calls"] == 0
    assert document["provenance"]["preservation"]["cross_page_merges"] == 0
    assert document["provenance"]["preservation"]["headers_and_footers_deleted"] is False
    assert document["provenance"]["render_role"] == "inference_evidence"

    review = json.loads(result.review_path.read_text(encoding="utf-8"))
    assert review["schema"] == OCR_REVIEW_SCHEMA
    assert review["pilot_approved"] is False
    assert review["human_review"]["required"] is True
    assert review["human_review"]["status"] == "pending"


def test_telemetry_records_tokens_latency_and_zero_local_cost(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf")
    result = run_ocr_pilot(pdf, tmp_path / "run", [1], client=pilot_client())
    telemetry = result.telemetry
    assert telemetry["provider"] == "ollama"
    assert telemetry["model_digest"] == "sha256:0123456789abcdef"
    assert telemetry["options"]["max_tokens"] == 4096
    assert telemetry["totals"]["input_tokens"] == 1234
    assert telemetry["totals"]["output_tokens"] == 567
    assert telemetry["totals"]["local_cost_usd"] == 0.0
    assert telemetry["totals"]["external_cost_usd"] == 0.0
    assert telemetry["totals"]["external_api_calls"] == 0
    assert telemetry["calls"][0]["latency_seconds"] >= 0.0
    assert telemetry["calls"][0]["image_pixels"]["width"] > 0


def test_summary_is_json_serializable_for_the_cli(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf")
    result = run_ocr_pilot(pdf, tmp_path / "run", [1], client=pilot_client())
    summary = summarize_run(result)
    assert json.loads(json.dumps(summary))["ok_pages"] == [1]
    assert summary["model_digest"] == "sha256:0123456789abcdef"


def test_only_loopback_requests_are_issued(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf")
    calls: list[httpx.Request] = []
    run_ocr_pilot(pdf, tmp_path / "run", [1], client=pilot_client(calls=calls))
    assert calls
    for request in calls:
        assert request.url.host in {"localhost", "127.0.0.1", "::1"}


def test_book_keeps_pages_separate_and_preserves_content(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf", pages=2)
    result = run_ocr_pilot(pdf, tmp_path / "run", [1, 2], client=pilot_client())
    book = result.book_path.read_text(encoding="utf-8")
    assert "<!-- ocr-page:0001 page_id=page-0001 status=ok" in book
    assert "<!-- ocr-page:0002 page_id=page-0002 status=ok" in book
    assert "# Chapter One" in book
    assert "Running header text" in book
    assert "![image](" not in book


def test_renders_are_inference_evidence_and_not_content_images(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf")
    result = run_ocr_pilot(pdf, tmp_path / "run", [1], client=pilot_client())
    document = json.loads(result.document_path.read_text(encoding="utf-8"))
    page = document["pages"][0]
    assert page["render_role"] == "inference_evidence"
    assert page["render_included_in_markdown"] is False
    assert (result.output_dir / page["render_path"]).is_file()


def test_no_cross_page_merge_is_recorded(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf", pages=2)
    result = run_ocr_pilot(pdf, tmp_path / "run", [1, 2], client=pilot_client())
    document = json.loads(result.document_path.read_text(encoding="utf-8"))
    first = document["pages"][0]["blocks"][-1]["text"]
    second = document["pages"][1]["blocks"][0]["text"]
    assert first != second
    assert document["provenance"]["preservation"]["cross_page_merges"] == 0


# ---------------------------------------------------------------------------
# failures, truncation and raw evidence
# ---------------------------------------------------------------------------


def test_truncated_output_is_failed_and_never_accepted(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf")
    output = tmp_path / "run"
    truncated = {
        "response": "# Chapter One\n\npartial body text",
        "done": True,
        "done_reason": "length",
        "prompt_eval_count": 10,
        "eval_count": 20,
    }
    result = run_ocr_pilot(
        pdf, output, [1], client=pilot_client(generate_response=truncated)
    )
    assert result.failed_pages == [1]
    assert result.ok_pages == []

    review = json.loads(result.review_path.read_text(encoding="utf-8"))
    failed = [entry for entry in review["entries"] if entry["kind"] == "page_failed"]
    assert failed and failed[0]["severity"] == "high"
    assert failed[0]["status"] == "open"

    book = result.book_path.read_text(encoding="utf-8")
    assert "status=failed" in book
    assert "OCR gap" in book
    assert "partial body text" not in book

    page_record = json.loads(
        (output / "pages" / "page-0001.json").read_text(encoding="utf-8")
    )
    assert page_record["status"] == "failed"
    assert page_record["error"]["kind"] == "truncated_output"
    assert page_record["partial_text_retained"]
    raw_text = (output / page_record["raw_text_path"]).read_text(encoding="utf-8")
    assert "partial body text" in raw_text


def test_http_error_is_retained_as_failed_page_evidence(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf")
    output = tmp_path / "run"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/generate":
            return httpx.Response(500, text="model exploded")
        return default_handler(request)

    result = run_ocr_pilot(pdf, output, [1], client=make_client(handler))
    assert result.failed_pages == [1]
    page_record = json.loads(
        (output / "pages" / "page-0001.json").read_text(encoding="utf-8")
    )
    assert page_record["error"]["kind"] == "http_error"
    raw = (output / page_record["raw_response_path"]).read_bytes()
    assert b"model exploded" in raw


def test_unparsable_response_keeps_the_raw_body(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf")
    output = tmp_path / "run"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/generate":
            return httpx.Response(200, content=b"<<not json>>")
        return default_handler(request)

    result = run_ocr_pilot(pdf, output, [1], client=make_client(handler))
    assert result.failed_pages == [1]
    page_record = json.loads(
        (output / "pages" / "page-0001.json").read_text(encoding="utf-8")
    )
    assert page_record["error"]["kind"] == "unparsable_response"
    raw = (output / page_record["raw_response_path"]).read_bytes()
    assert raw == b"<<not json>>"


def test_server_error_body_is_reported_as_a_failure(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf")
    output = tmp_path / "run"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/generate":
            return httpx.Response(200, json={"error": "model 'x' not found"})
        return default_handler(request)

    result = run_ocr_pilot(pdf, output, [1], client=make_client(handler))
    assert result.failed_pages == [1]
    page_record = json.loads(
        (output / "pages" / "page-0001.json").read_text(encoding="utf-8")
    )
    assert page_record["error"]["kind"] == "server_error"
    assert page_record["raw_response_path"]


def test_failed_page_is_reported_in_the_review_file(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf")
    output = tmp_path / "run"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/generate":
            return httpx.Response(200, json={"response": "", "done": True})
        return default_handler(request)

    result = run_ocr_pilot(pdf, output, [1], client=make_client(handler))
    review = json.loads(result.review_path.read_text(encoding="utf-8"))
    assert review["summary"]["pages_failed"] == 1
    assert review["summary"]["severity_counts"]["high"] >= 1


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


def test_resume_reuses_a_matching_page_response(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf")
    output = tmp_path / "run"
    calls: list[httpx.Request] = []
    run_ocr_pilot(pdf, output, [1], client=pilot_client(calls=calls))
    generate_calls = [call for call in calls if call.url.path == "/api/generate"]
    assert len(generate_calls) == 1

    calls.clear()
    result = run_ocr_pilot(pdf, output, [1], client=pilot_client(calls=calls))
    generate_calls = [call for call in calls if call.url.path == "/api/generate"]
    assert generate_calls == []
    assert result.ok_pages == [1]
    assert result.telemetry["totals"]["cached_calls"] == 1
    assert result.telemetry["totals"]["input_tokens"] == 1234
    assert result.telemetry["totals"]["output_tokens"] == 567
    assert result.telemetry["totals"]["new_input_tokens"] == 0
    assert result.telemetry["totals"]["new_output_tokens"] == 0


def test_resume_rebuilds_structured_blocks_without_repeating_ocr(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf")
    output = tmp_path / "run"
    calls = []
    run_ocr_pilot(pdf, output, [1], client=pilot_client())
    record_path = output / "pages/page-0001.json"
    record = json.loads(record_path.read_text())
    record["blocks"] = []  # Simulate an older parser that omitted structure.
    record_path.write_text(json.dumps(record))
    run_ocr_pilot(pdf, output, [1], client=pilot_client(calls=calls))
    assert json.loads(record_path.read_text())["blocks"]
    assert not any(request.url.path == "/api/generate" for request in calls)


def test_resume_is_invalidated_when_options_change(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf")
    output = tmp_path / "run"
    calls: list[httpx.Request] = []
    run_ocr_pilot(pdf, output, [1], client=pilot_client(calls=calls))
    calls.clear()
    run_ocr_pilot(
        pdf,
        output,
        [1],
        max_tokens=2048,
        client=pilot_client(calls=calls),
    )
    generate_calls = [call for call in calls if call.url.path == "/api/generate"]
    assert len(generate_calls) == 1


def test_resume_is_invalidated_when_the_image_changes(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf")
    output = tmp_path / "run"
    calls: list[httpx.Request] = []
    run_ocr_pilot(pdf, output, [1], client=pilot_client(calls=calls))
    calls.clear()
    run_ocr_pilot(
        pdf, output, [1], dpi=96, client=pilot_client(calls=calls)
    )
    generate_calls = [call for call in calls if call.url.path == "/api/generate"]
    assert len(generate_calls) == 1


# ---------------------------------------------------------------------------
# prompt handling
# ---------------------------------------------------------------------------


def test_generate_payload_uses_raw_non_thinking_mode() -> None:
    payload = build_generate_payload(
        model=DEFAULT_MODEL,
        prompt="PROMPT",
        image_png=b"png-bytes",
        options=_options(),
    )
    assert payload["raw"] is True
    assert payload["think"] is False
    assert payload["stream"] is False
    assert payload["options"]["num_predict"] == 4096
    assert payload["options"]["num_ctx"] == 16384
    assert payload["images"] and payload["images"][0]


def _options():
    from pdfcracker.ocr import OcrOptions

    return OcrOptions()


def test_raw_prompt_uses_the_canonical_qwen_layout_with_an_empty_think_block() -> None:
    rendered = render_raw_prompt("TASK")
    # Live Ollama 0.40.14 rejected <image> with zero media markers; [img-0]
    # produced recognition of the actual scan. Keep this transport contract.
    assert rendered.startswith("<|im_start|>user\n[img-0]\nTASK<|im_end|>\n")
    assert rendered.count("[img-0]") == 1
    assert "<|im_start|>assistant\n" in rendered
    assert rendered.endswith("<think>\n\n</think>\n\n")
    assert "<think>" in rendered
    assert rendered.count("<think>") == 1


def test_upstream_prompt_variant_is_byte_exact() -> None:
    assert UPSTREAM_DOCUMENT_PROMPT.startswith(
        "Extract all information from the main body"
    )
    assert "ignoring headers and footers" in UPSTREAM_DOCUMENT_PROMPT
    assert "including headers, footers, page numbers" in PRESERVATION_DOCUMENT_PROMPT
    assert "footnotes" in PRESERVATION_DOCUMENT_PROMPT
    assert PRESERVATION_DOCUMENT_PROMPT != UPSTREAM_DOCUMENT_PROMPT


def test_prompt_divergence_is_flagged_in_telemetry_and_review(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf")
    output = tmp_path / "run"
    result = run_ocr_pilot(pdf, output, [1], client=pilot_client())
    document = json.loads(result.document_path.read_text(encoding="utf-8"))
    assert document["prompt"]["diverges_from_upstream"] is True
    assert document["telemetry"]["prompt"]["diverges_from_upstream"] is True
    review = json.loads(result.review_path.read_text(encoding="utf-8"))
    assert any(entry["kind"] == "prompt_divergence" for entry in review["entries"])


def test_upstream_prompt_variant_is_not_flagged_as_divergence(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf")
    output = tmp_path / "run"
    result = run_ocr_pilot(
        pdf, output, [1], prompt_variant="upstream", client=pilot_client()
    )
    document = json.loads(result.document_path.read_text(encoding="utf-8"))
    assert document["prompt"]["diverges_from_upstream"] is False
    assert document["prompt"]["task_prompt"] == UPSTREAM_DOCUMENT_PROMPT


def test_upstream_prompt_is_transcribed_verbatim() -> None:
    assert UPSTREAM_DOCUMENT_PROMPT.startswith(
        "Extract all information from the main body"
    )
    assert "ignoring headers and footers" in UPSTREAM_DOCUMENT_PROMPT
    assert "Tables should be expressed in OTSL format" in UPSTREAM_DOCUMENT_PROMPT
    assert "according to the reading order" in UPSTREAM_DOCUMENT_PROMPT


def test_generate_request_carries_the_raw_prompt_and_the_image(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/generate":
            captured["body"] = json.loads(request.content)
        return default_handler(request)

    run_ocr_pilot(pdf, tmp_path / "run", [1], client=make_client(handler))
    body = captured["body"]
    assert body["model"] == DEFAULT_MODEL
    assert body["raw"] is True
    assert body["think"] is False
    assert body["stream"] is False
    assert "<|im_start|>assistant" in body["prompt"]
    assert "<think>\n\n</think>" in body["prompt"]
    assert body["options"]["num_ctx"] == 16384
    assert body["options"]["num_predict"] == 4096
    assert body["images"] and body["images"][0]


def test_unexpected_page_error_becomes_a_failed_review_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pdfcracker.ocr as ocr

    pdf = write_blank_pdf(tmp_path / "blank.pdf")

    def boom(*args, **kwargs):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(ocr, "prepare_page_image", boom)
    result = ocr.run_ocr_pilot(pdf, tmp_path / "run", [1], client=pilot_client())
    assert result.failed_pages == [1]
    assert result.ok_pages == []
    review = json.loads(result.review_path.read_text(encoding="utf-8"))
    failed = [entry for entry in review["entries"] if entry["kind"] == "page_failed"]
    assert failed and failed[0]["severity"] == "high"
    assert "render exploded" in failed[0]["message"]


# ---------------------------------------------------------------------------
# markdown role parsing
# ---------------------------------------------------------------------------


def test_roles_are_classified_conservatively() -> None:
    markdown = (
        "# Chapter One\n"
        "\n"
        "Body paragraph.\n"
        "\n"
        "## Section\n"
        "\n"
        "- item one\n"
        "- item two\n"
        "\n"
        "| a | b |\n"
        "| --- | --- |\n"
        "| 1 | 2 |\n"
        "\n"
        "> quoted line\n"
    )
    blocks = parse_page_markdown(markdown)
    roles = [block.role for block in blocks]
    assert roles == ["heading", "body", "heading", "list_item", "list_item", "table", "quote"]
    assert blocks[0].heading_level == 1
    assert blocks[2].heading_level == 2
    assert all(block.evidence for block in blocks)
    assert all(0.0 < block.confidence <= 1.0 for block in blocks)


def test_footnote_definitions_are_kept_and_classified() -> None:
    markdown = (
        "Body with a marker[^7].\n"
        "\n"
        "## Footnotes\n"
        "\n"
        "[^7]: The note body.\n"
    )
    blocks = parse_page_markdown(markdown)
    roles = [block.role for block in blocks]
    assert "footnote_definition" in roles
    definition = next(block for block in blocks if block.role == "footnote_definition")
    assert definition.text.startswith("[^7]:")
    assert definition.footnote_labels == ["7"]
    assert any(block.footnote_labels == ["7"] for block in blocks)


def test_unknown_blocks_are_preserved_verbatim() -> None:
    markdown = "<div class='x'>raw html</div>\n"
    blocks = parse_page_markdown(markdown)
    assert [block.role for block in blocks] == ["unknown"]
    assert blocks[0].markdown.strip() == "<div class='x'>raw html</div>"


def test_inline_content_is_preserved() -> None:
    blocks = parse_page_markdown("A *word* and `code` in text.\n")
    assert len(blocks) == 1
    types = {child["type"] for child in blocks[0].inline}
    assert {"text", "em_open", "code_inline"} <= types


def test_blocks_never_invent_geometry(tmp_path: Path) -> None:
    pdf = write_blank_pdf(tmp_path / "blank.pdf")
    result = run_ocr_pilot(pdf, tmp_path / "run", [1], client=pilot_client())
    document = json.loads(result.document_path.read_text(encoding="utf-8"))
    assert document["blocks"]
    for block in document["blocks"]:
        assert block["bbox"] is None
        assert block["bbox_norm"] is None
        assert block["font"] is None
        assert block["geometry"]["available"] is False
        assert block["geometry"]["reason"]
        assert block["style"]["bold"] is None
        assert block["role_confidence_note"]


def test_empty_page_markdown_yields_no_blocks() -> None:
    assert parse_page_markdown("") == []
    assert parse_page_markdown("   \n") == []
