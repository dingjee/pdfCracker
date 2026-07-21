from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import pdfplumber

from pdfcracker.models import BlockType, Document


def detect_ruled_table_candidates(pdf_path: Path, pages: list[int]) -> set[int]:
    candidates: set[int] = set()
    with pdfplumber.open(pdf_path) as pdf:
        for page_number in pages:
            page = pdf.pages[page_number - 1]
            horizontal = 0
            vertical = 0
            for item in [*page.lines, *page.rects]:
                width = abs(float(item["x1"]) - float(item["x0"]))
                height = abs(float(item["bottom"]) - float(item["top"]))
                if width >= 30 and height <= 3:
                    horizontal += 1
                if height >= 20 and width <= 3:
                    vertical += 1
            if horizontal >= 4 and vertical >= 3:
                candidates.add(page_number)
    return candidates


def build_risk_plan(
    document: Document,
    table_pages: set[int] | None = None,
    visual_sample_limit: int = 32,
) -> dict[str, Any]:
    table_pages = table_pages or set()
    images_by_page: dict[int, list[Any]] = defaultdict(list)
    for image in document.images:
        images_by_page[image.page].append(image)
    merge_by_page: dict[int, list[Any]] = defaultdict(list)
    for decision in document.merges:
        merge_by_page[decision.from_page].append(decision)
        if decision.to_page != decision.from_page:
            merge_by_page[decision.to_page].append(decision)

    clusters: dict[str, list[int]] = defaultdict(list)
    rows: list[dict[str, Any]] = []
    for page in document.pages:
        active = [block for block in page.blocks if not block.removed_as]
        page_images = images_by_page[page.number]
        has_underline = any(
            span.underline
            for block in active
            for line in block.lines
            for span in line.spans
        )
        has_color = any(
            span.semantic_color
            for block in active
            for line in block.lines
            for span in line.spans
        )
        has_italic = any(
            span.italic for block in active for line in block.lines for span in line.spans
        )
        minimum_font = min(
            (
                span.font.size
                for block in active
                for line in block.lines
                for span in line.spans
                if span.text.strip() and span.font.size > 0
            ),
            default=document.body_font_size,
        )
        roles = sorted({block.type.value for block in active})
        signature = "|".join(
            [
                f"{round(page.width)}x{round(page.height)}",
                page.kind.value,
                ",".join(roles),
                "images" if page_images else "no-images",
                "table" if page.number in table_pages else "no-table",
                "styled" if has_underline or has_color or has_italic else "plain",
            ]
        )
        clusters[signature].append(page.number)
        reasons: list[str] = []
        if page.kind.value != "body":
            reasons.append(f"page_kind={page.kind.value}")
        if any(block.type == BlockType.HEADING for block in active):
            reasons.append("heading")
        if any(block.type == BlockType.QUOTE for block in active):
            reasons.append("quote_or_callout")
        if page_images:
            reasons.append("embedded_image")
        if any(image.role == "review" for image in page_images):
            reasons.append("ambiguous_image_role")
        if page.number in table_pages:
            reasons.append("ruled_table_candidate")
        if has_underline:
            reasons.append("underline")
        if has_color:
            reasons.append("semantic_color")
        if has_italic:
            reasons.append("italic")
        if any(block.removed_as for block in page.blocks):
            reasons.append("removed_furniture")
        if any(decision.decision == "review" for decision in merge_by_page[page.number]):
            reasons.append("ambiguous_merge")
        if active and min(block.confidence for block in active) < 0.75:
            reasons.append("low_confidence_block")
        recommended_dpi = 144 if has_underline or minimum_font < 9 else 96
        rows.append(
            {
                "page": page.number,
                "cluster": signature,
                "reasons": reasons,
                "recommended_dpi": recommended_dpi,
                "minimum_font_size": round(minimum_font, 3),
            }
        )

    representatives = {pages[0] for pages in clusters.values()}
    selected: list[dict[str, Any]] = []
    for row in rows:
        reasons = list(row["reasons"])
        if row["page"] in representatives:
            reasons.append("cluster_representative")
        if reasons:
            selected.append({**row, "reasons": sorted(set(reasons))})
    weights = {
        "ambiguous_merge": 12,
        "ambiguous_image_role": 11,
        "ruled_table_candidate": 10,
        "underline": 8,
        "cluster_representative": 7,
        "embedded_image": 6,
        "low_confidence_block": 6,
        "quote_or_callout": 5,
        "semantic_color": 4,
        "heading": 3,
        "italic": 2,
        "removed_furniture": 1,
    }
    visual_sample = sorted(
        selected,
        key=lambda row: (
            -sum(weights.get(reason, 0) for reason in row["reasons"]),
            row["page"],
        ),
    )[:visual_sample_limit]
    return {
        "strategy": "cluster_representatives_plus_structural_and_semantic_risks",
        "clusters": [
            {"signature": signature, "pages": pages, "representative": pages[0]}
            for signature, pages in sorted(clusters.items())
        ],
        "pages": selected,
        "visual_sample": visual_sample,
        "summary": {
            "cluster_count": len(clusters),
            "risk_page_count": len(selected),
            "table_candidate_count": len(table_pages),
            "dpi_144_pages": sum(row["recommended_dpi"] == 144 for row in selected),
            "visual_sample_count": len(visual_sample),
        },
    }
