from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from pdfcracker.markdown import render_inline
from pdfcracker.models import BlockType, Document


AuditLevel = Literal["fatal", "needs_review", "warning", "information"]


class AuditIssue(BaseModel):
    level: AuditLevel
    code: str
    message: str
    page: int | None = None
    object_id: str | None = None
    evidence: list[str] = Field(default_factory=list)


class AuditReport(BaseModel):
    schema_version: str = "0.1.0"
    publishable: bool
    status: Literal["blocked", "review_recommended", "publishable"]
    counts: dict[str, int]
    quality: dict[str, float | int]
    ledger: dict[str, int]
    issues: list[AuditIssue]


def _add(
    issues: list[AuditIssue],
    level: AuditLevel,
    code: str,
    message: str,
    *,
    page: int | None = None,
    object_id: str | None = None,
    evidence: list[str] | None = None,
) -> None:
    issues.append(
        AuditIssue(
            level=level,
            code=code,
            message=message,
            page=page,
            object_id=object_id,
            evidence=evidence or [],
        )
    )


def _audit_html(markdown: str, issues: list[AuditIssue]) -> None:
    stack: list[str] = []
    for match in re.finditer(r"</?(strong|em|u|span)\b[^>]*>", markdown):
        token = match.group(0)
        name = match.group(1)
        if token.startswith("</"):
            if not stack or stack[-1] != name:
                _add(
                    issues,
                    "fatal",
                    "unbalanced_semantic_html",
                    f"Unexpected closing tag {token}",
                )
                return
            stack.pop()
        else:
            stack.append(name)
    if stack:
        _add(
            issues,
            "fatal",
            "unbalanced_semantic_html",
            f"Unclosed semantic HTML tags: {stack}",
        )


def _audit_markdown(markdown: str, issues: list[AuditIssue]) -> None:
    if "\ufffd" in markdown:
        _add(issues, "fatal", "replacement_character", "Markdown contains U+FFFD.")
    controls = sorted({ord(char) for char in markdown if ord(char) < 32 and char not in "\n\r\t"})
    if controls:
        _add(
            issues,
            "fatal",
            "control_characters",
            "Markdown contains unexpected control characters.",
            evidence=[str(value) for value in controls],
        )
    if re.search(r"\w(?:\*\*|\*)\w", markdown):
        _add(
            issues,
            "needs_review",
            "midword_emphasis_boundary",
            "Markdown emphasis marker occurs inside an alphanumeric word.",
        )
    headings: list[tuple[int, str]] = []
    for line in markdown.splitlines():
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            headings.append((len(match.group(1)), match.group(2).strip()))
    for level, text in headings:
        if not text:
            _add(issues, "fatal", "empty_heading", "Markdown contains an empty heading.")
    for previous, following in zip(headings, headings[1:], strict=False):
        if following[0] - previous[0] > 1:
            _add(
                issues,
                "warning",
                "heading_level_jump",
                f"Heading level jumps from H{previous[0]} to H{following[0]}.",
                evidence=[previous[1][:100], following[1][:100]],
            )
    _audit_html(markdown, issues)


def _safe_asset(root: Path, relative: str) -> Path | None:
    resolved_root = root.resolve()
    candidate = (root / relative).resolve()
    return candidate if candidate.is_relative_to(resolved_root) else None


def audit_document(
    document: Document,
    markdown: str,
    artifact_root: Path,
    *,
    require_full: bool = False,
) -> AuditReport:
    issues: list[AuditIssue] = []
    page_numbers = [page.number for page in document.pages]
    selected = document.selected_pages
    if len(page_numbers) != len(set(page_numbers)):
        _add(issues, "fatal", "duplicate_pages", "Document contains duplicate page models.")
    if page_numbers != selected:
        _add(
            issues,
            "fatal",
            "selected_page_mismatch",
            "Materialized pages do not exactly match selected_pages.",
        )
    if require_full and selected != list(range(1, document.source_page_count + 1)):
        _add(
            issues,
            "fatal",
            "incomplete_full_book_coverage",
            "Full-book processing does not cover every source page exactly once.",
        )

    block_ids = {block.id for page in document.pages for block in page.blocks}
    ledger = Counter()
    for page in document.pages:
        for block in page.blocks:
            if block.removed_as:
                ledger[f"removed:{block.removed_as}"] += 1
                if not block.evidence:
                    _add(
                        issues,
                        "needs_review",
                        "removal_without_evidence",
                        "A removed block has no recorded evidence.",
                        page=page.number,
                        object_id=block.id,
                    )
                continue
            if block.type == BlockType.UNKNOWN:
                ledger["unexplained"] += 1
                _add(
                    issues,
                    "fatal",
                    "unexplained_source_block",
                    "An active source block has no semantic disposition.",
                    page=page.number,
                    object_id=block.id,
                )
                continue
            rendered = render_inline(block) or block.text
            if not rendered.strip():
                ledger["unexplained"] += 1
                _add(
                    issues,
                    "fatal",
                    "empty_active_block",
                    "An active source block produces no readable content.",
                    page=page.number,
                    object_id=block.id,
                )
            else:
                ledger[f"emitted:{block.type.value}"] += 1
            if block.confidence < 0.65:
                _add(
                    issues,
                    "needs_review",
                    "low_block_confidence",
                    f"Block confidence is {block.confidence:.3f}.",
                    page=page.number,
                    object_id=block.id,
                    evidence=block.evidence,
                )

    for decision in document.merges:
        if decision.from_block not in block_ids or decision.to_block not in block_ids:
            _add(
                issues,
                "fatal",
                "invalid_merge_reference",
                "Merge decision refers to a missing source block.",
                object_id=f"{decision.from_block}->{decision.to_block}",
            )
        if decision.decision == "review":
            _add(
                issues,
                "needs_review",
                "ambiguous_paragraph_merge",
                f"Paragraph boundary score {decision.score:.3f} needs review.",
                page=decision.from_page,
                object_id=f"{decision.from_block}->{decision.to_block}",
                evidence=decision.reasons,
            )

    referenced_assets = set(re.findall(r"!\[[^\]]*]\(([^)]+)\)", markdown))
    for image in document.images:
        asset = _safe_asset(artifact_root, image.asset_path)
        if asset is None:
            _add(
                issues,
                "fatal",
                "unsafe_asset_path",
                "Image asset path escapes the artifact root.",
                page=image.page,
                object_id=image.id,
            )
        elif not asset.is_file():
            _add(
                issues,
                "fatal",
                "missing_embedded_image_asset",
                f"Extracted image asset is missing: {image.asset_path}",
                page=image.page,
                object_id=image.id,
            )
        if image.include_in_markdown and image.asset_path not in referenced_assets:
            _add(
                issues,
                "fatal",
                "content_image_not_referenced",
                "A content image is absent from Markdown.",
                page=image.page,
                object_id=image.id,
            )
        if image.role == "review":
            _add(
                issues,
                "needs_review",
                "ambiguous_embedded_image",
                "Embedded image may be content or decoration.",
                page=image.page,
                object_id=image.id,
                evidence=image.evidence,
            )
        page = next(page for page in document.pages if page.number == image.page)
        if (
            image.include_in_markdown
            and page.kind.value != "front_matter"
            and not (
            image.anchor_before_block_id or image.anchor_after_block_id
            )
        ):
            _add(
                issues,
                "needs_review",
                "unanchored_content_image",
                "Content image has no nearby text anchor.",
                page=image.page,
                object_id=image.id,
            )

    unmatched = document.metadata.get("image_extraction", {}).get("unmatched", [])
    for item in unmatched:
        _add(
            issues,
            "needs_review",
            "embedded_image_decode_unmatched",
            "A PDF image object could not be decoded without raster fallback.",
            page=item.get("page"),
            evidence=[str(item.get("object_name", "")), str(item.get("reason", ""))],
        )

    for table in document.tables:
        if table.role == "review":
            _add(
                issues,
                "needs_review",
                "ambiguous_native_table",
                "A native cell topology may be a semantic table or a layout box.",
                page=table.page,
                object_id=table.id,
                evidence=table.evidence,
            )
        if table.include_in_markdown and (not table.rows or not table.anchor_before_block_id):
            _add(
                issues,
                "fatal",
                "invalid_materialized_table",
                "A Markdown table lacks rows or a source anchor.",
                page=table.page,
                object_id=table.id,
            )

    _audit_markdown(markdown, issues)
    counts = Counter(issue.level for issue in issues)
    publishable = counts["fatal"] == 0
    status = (
        "blocked"
        if not publishable
        else "review_recommended"
        if counts["needs_review"]
        else "publishable"
    )
    active_blocks = sum(value for key, value in ledger.items() if key.startswith("emitted:"))
    explained_blocks = active_blocks + sum(
        value for key, value in ledger.items() if key.startswith("removed:")
    )
    total_blocks = sum(ledger.values())
    text_integrity = explained_blocks / total_blocks if total_blocks else 1.0
    included_images = [image for image in document.images if image.include_in_markdown]
    page_kinds = {page.number: page.kind.value for page in document.pages}
    anchored_images = sum(
        bool(image.anchor_before_block_id or image.anchor_after_block_id)
        or page_kinds.get(image.page) == "front_matter"
        for image in included_images
    )
    return AuditReport(
        publishable=publishable,
        status=status,
        counts={level: counts[level] for level in ("fatal", "needs_review", "warning", "information")},
        quality={
            "text_integrity": round(text_integrity, 6),
            "page_coverage": round(len(page_numbers) / max(1, len(selected)), 6),
            "image_anchor_coverage": round(
                anchored_images / max(1, len(included_images)), 6
            ),
            "unresolved_reviews": counts["needs_review"],
        },
        ledger=dict(sorted(ledger.items())),
        issues=issues,
    )
