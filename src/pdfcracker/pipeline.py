from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from pdfcracker.audit import AuditReport, audit_document
from pdfcracker.cache import cleanup_source_cache
from pdfcracker.extract import extract_document
from pdfcracker.images import extract_embedded_images
from pdfcracker.markdown import render_markdown
from pdfcracker.profile import DocumentProfile, apply_profile
from pdfcracker.render import render_pages
from pdfcracker.risk import build_risk_plan
from pdfcracker.tables import extract_native_table_candidates


class ProcessResult(BaseModel):
    output: str | None
    staging: str | None
    published: bool
    audit: AuditReport
    report: dict[str, Any]


def _write_json(path: Path, value: Any) -> None:
    if isinstance(value, BaseModel):
        path.write_text(value.model_dump_json(indent=2), encoding="utf-8")
    else:
        path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def process_pdf(
    pdf_path: Path,
    output_dir: Path,
    *,
    pages: list[int] | None = None,
    title: str | None = None,
    cache_dir: Path | None = None,
    require_full: bool = False,
    cleanup_cache_on_success: bool = True,
    keep_review_renders: bool = False,
    visual_sample_limit: int = 32,
    profile: DocumentProfile | None = None,
) -> ProcessResult:
    if output_dir.exists():
        raise ValueError(f"Output already exists; choose a new run directory: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.parent / f".{output_dir.name}.staging-{uuid.uuid4().hex[:10]}"
    staging.mkdir(parents=False, exist_ok=False)
    effective_cache = cache_dir or output_dir.parent / ".cache" / "pdfcracker"
    stages: list[dict[str, Any]] = []
    started_all = time.perf_counter()

    started = time.perf_counter()
    document = extract_document(pdf_path, pages, cache_dir=effective_cache)
    stages.append(
        {
            "stage": "native_text_and_structure",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "cache_hit": bool(document.metadata.get("cache", {}).get("hit")),
            "cost_usd": 0.0,
        }
    )

    started = time.perf_counter()
    extract_embedded_images(pdf_path, document, staging / "assets" / "images")
    stages.append(
        {
            "stage": "embedded_image_extraction",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "objects": len(document.images),
            "cost_usd": 0.0,
        }
    )

    started = time.perf_counter()
    extract_native_table_candidates(pdf_path, document)
    stages.append(
        {
            "stage": "native_table_candidates",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "candidates": len(document.tables),
            "cost_usd": 0.0,
        }
    )

    if profile is not None:
        started = time.perf_counter()
        profile_counts = apply_profile(document, profile)
        stages.append(
            {
                "stage": "document_profile",
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "overrides": profile_counts,
                "cost_usd": 0.0,
            }
        )

    started = time.perf_counter()
    markdown = render_markdown(document, title=title)
    debug_markdown = render_markdown(document, title=title, debug=True)
    (staging / "book.md").write_text(markdown, encoding="utf-8")
    (staging / "book.debug.md").write_text(debug_markdown, encoding="utf-8")
    stages.append(
        {
            "stage": "markdown_render",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "cost_usd": 0.0,
        }
    )

    started = time.perf_counter()
    table_pages = {table.page for table in document.tables}
    risk_plan = build_risk_plan(document, table_pages, visual_sample_limit)
    if keep_review_renders:
        rendered: list[str] = []
        for dpi in (96, 144):
            render_page_numbers = [
                row["page"]
                for row in risk_plan["visual_sample"]
                if row["recommended_dpi"] == dpi
            ]
            if render_page_numbers:
                outputs = render_pages(
                    pdf_path,
                    staging / "qa" / "renders",
                    render_page_numbers,
                    dpi,
                )
                rendered.extend(path.relative_to(staging).as_posix() for path in outputs)
        risk_plan["rendered_review_pages"] = rendered
    (staging / "qa").mkdir(parents=True, exist_ok=True)
    _write_json(staging / "qa" / "risk-plan.json", risk_plan)
    stages.append(
        {
            "stage": "risk_planning",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "risk_pages": risk_plan["summary"]["risk_page_count"],
            "visual_sample_pages": risk_plan["summary"]["visual_sample_count"],
            "cost_usd": 0.0,
        }
    )

    _write_json(staging / "document.json", document)
    started = time.perf_counter()
    audit = audit_document(document, markdown, staging, require_full=require_full)
    _write_json(staging / "qa" / "audit.json", audit)
    stages.append(
        {
            "stage": "automatic_audit",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "fatal": audit.counts["fatal"],
            "needs_review": audit.counts["needs_review"],
            "cost_usd": 0.0,
        }
    )

    report = {
        "source_sha256": document.source_sha256,
        "source_page_count": document.source_page_count,
        "selected_pages": document.selected_pages,
        "schema_version": document.schema_version,
        "profile": document.metadata.get("profile"),
        "block_counts": document.metadata.get("block_counts", {}),
        "merge_counts": {
            decision: sum(item.decision == decision for item in document.merges)
            for decision in ("merge", "review", "keep_separate")
        },
        "image_counts": document.metadata.get("image_extraction", {}),
        "table_counts": document.metadata.get("table_extraction", {}),
        "audit": {
            "publishable": audit.publishable,
            "status": audit.status,
            "counts": audit.counts,
            "quality": audit.quality,
        },
        "costs": {
            "total_usd": round(sum(float(stage["cost_usd"]) for stage in stages), 8),
            "note": "Local deterministic stages only; model calls are logged by vision-benchmark.",
        },
        "stages": stages,
        "elapsed_seconds": round(time.perf_counter() - started_all, 3),
    }
    _write_json(staging / "report.json", report)

    if not audit.publishable:
        return ProcessResult(
            output=None,
            staging=str(staging),
            published=False,
            audit=audit,
            report=report,
        )

    staging.rename(output_dir)
    cache_cleaned = False
    if cleanup_cache_on_success:
        cache_cleaned = cleanup_source_cache(effective_cache, document.source_sha256)
    report["cache_cleaned_on_success"] = cache_cleaned
    _write_json(output_dir / "report.json", report)
    return ProcessResult(
        output=str(output_dir),
        staging=None,
        published=True,
        audit=audit,
        report=report,
    )
