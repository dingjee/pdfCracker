from __future__ import annotations

import hashlib
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from pdftext.extraction import dictionary_output

from pdfcracker.models import (
    BBox,
    Block,
    BlockType,
    Document,
    FontInfo,
    Line,
    MergeDecision,
    Page,
    PageKind,
    Span,
)
from pdfcracker.styles import enrich_document_styles

TERMINAL_RE = re.compile(r'[.!?][\"\'”’)]*$')
TOC_ENTRY_RE = re.compile(r"\.{3,}\s*-?\s*\d+\s*-?\s*$")
PAGE_NUMBER_RE = re.compile(r"^(?:page\s+)?(?:[ivxlcdm]+|\d+)(?:\s+of\s+\d+)?$", re.I)
LIST_RE = re.compile(r"^(?:[•●▪◦*-]|\(?\d+[.)]|[A-Za-z][.)])\s+")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_page_spec(spec: str | None, page_count: int) -> list[int]:
    if not spec:
        return list(range(1, page_count + 1))
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
            if end < start:
                raise ValueError(f"Invalid descending page range: {part}")
            pages.update(range(start, end + 1))
        else:
            pages.add(int(part))
    invalid = sorted(page for page in pages if page < 1 or page > page_count)
    if invalid:
        raise ValueError(f"Pages outside 1..{page_count}: {invalid}")
    return sorted(pages)


def _smart_join(lines: Iterable[str]) -> str:
    result = ""
    for raw_line in lines:
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if not result:
            result = line
        elif result.endswith("-") and line[:1].islower():
            result = result[:-1] + line
        else:
            result += " " + line
    return result


def _convert_page(raw_page: dict[str, Any]) -> Page:
    page_number = int(raw_page["page"]) + 1
    width = float(raw_page["width"])
    height = float(raw_page["height"])
    blocks: list[Block] = []
    for block_index, raw_block in enumerate(raw_page.get("blocks", [])):
        lines: list[Line] = []
        for raw_line in raw_block.get("lines", []):
            spans: list[Span] = []
            for raw_span in raw_line.get("spans", []):
                font = raw_span.get("font") or {}
                spans.append(
                    Span(
                        text=str(raw_span.get("text", "")),
                        bbox=BBox.from_sequence(raw_span["bbox"]),
                        font=FontInfo(
                            name=str(font.get("name", "")),
                            size=float(font.get("size", 0.0)),
                            weight=int(font.get("weight", -1)),
                            flags=int(font.get("flags", 0)),
                        ),
                        rotation=float(raw_span.get("rotation", 0.0)),
                        url=str(raw_span.get("url", "")),
                        superscript=bool(raw_span.get("superscript", False)),
                        subscript=bool(raw_span.get("subscript", False)),
                    )
                )
            line_text = "".join(span.text for span in spans).replace("\n", "").strip()
            lines.append(
                Line(
                    text=line_text,
                    bbox=BBox.from_sequence(raw_line["bbox"]),
                    spans=spans,
                )
            )
        bbox = BBox.from_sequence(raw_block["bbox"])
        raw_text = "\n".join(line.text for line in lines if line.text)
        text = _smart_join(line.text for line in lines)
        if not text:
            continue
        blocks.append(
            Block(
                id=f"p{page_number:04d}-b{block_index:04d}",
                page=page_number,
                source_block_index=block_index,
                bbox=bbox,
                bbox_norm=bbox.normalized(width, height),
                raw_text=raw_text,
                text=text,
                lines=lines,
            )
        )
    return Page(
        number=page_number,
        source_index=page_number - 1,
        width=width,
        height=height,
        rotation=int(raw_page.get("rotation", 0)),
        blocks=blocks,
    )


def _body_font_size(pages: list[Page]) -> float:
    sizes: list[float] = []
    for page in pages:
        for block in page.blocks:
            for line in block.lines:
                for span in line.spans:
                    if span.text.strip() and span.font.size >= 6:
                        sizes.extend([span.font.size] * max(1, len(span.text.strip())))
    return round(statistics.median(sizes), 3) if sizes else 12.0


def _block_font_size(block: Block) -> float:
    weighted: list[float] = []
    for line in block.lines:
        for span in line.spans:
            if span.text.strip() and span.font.size > 0:
                weighted.extend([span.font.size] * max(1, len(span.text.strip())))
    return statistics.median(weighted) if weighted else 0.0


def _line_font_size(line: Line) -> float:
    weighted = [
        span.font.size
        for span in line.spans
        for _ in range(max(1, len(span.text.strip())))
        if span.text.strip() and span.font.size > 0
    ]
    return statistics.median(weighted) if weighted else 0.0


def _block_from_lines(original: Block, lines: list[Line], suffix: str) -> Block:
    bbox = BBox(
        x0=min(line.bbox.x0 for line in lines),
        y0=min(line.bbox.y0 for line in lines),
        x1=max(line.bbox.x1 for line in lines),
        y1=max(line.bbox.y1 for line in lines),
    )
    page_width = original.bbox.x1 / original.bbox_norm.x1 if original.bbox_norm.x1 else 1.0
    page_height = original.bbox.y1 / original.bbox_norm.y1 if original.bbox_norm.y1 else 1.0
    return Block(
        id=f"{original.id}-{suffix}",
        page=original.page,
        source_block_index=original.source_block_index,
        bbox=bbox,
        bbox_norm=bbox.normalized(page_width, page_height),
        raw_text="\n".join(line.text for line in lines if line.text),
        text=_smart_join(line.text for line in lines),
        lines=lines,
    )


def split_mixed_blocks(pages: list[Page], body_font_size: float) -> None:
    for page in pages:
        output: list[Block] = []
        for block in page.blocks:
            if len(block.lines) < 2:
                output.append(block)
                continue
            first_size = _line_font_size(block.lines[0])
            remaining_sizes = [
                size for line in block.lines[1:] if (size := _line_font_size(line)) > 0
            ]
            remaining_size = statistics.median(remaining_sizes) if remaining_sizes else first_size
            first_text = block.lines[0].text.strip()
            if (
                first_size >= body_font_size * 1.25
                and remaining_size <= body_font_size * 1.15
                and len(first_text) <= 120
            ):
                output.append(_block_from_lines(block, [block.lines[0]], "s0"))
                output.append(_block_from_lines(block, block.lines[1:], "s1"))
            else:
                output.append(block)
        page.blocks = output


def assign_toc_line_metadata(pages: list[Page]) -> None:
    toc_lines = [
        line
        for page in pages
        if page.kind == PageKind.TOC
        for block in page.blocks
        if block.type == BlockType.TOC_ENTRY
        for line in block.lines
        if line.text.strip()
    ]
    x_clusters: list[float] = []
    for x0 in sorted(line.bbox.x0 for line in toc_lines if "." * 3 in line.text):
        if not x_clusters or abs(x0 - x_clusters[-1]) > 5:
            x_clusters.append(x0)
    for line in toc_lines:
        match = re.match(r"^(.*?)(?:\.{3,})\s*-?\s*(\d+)\s*-?\s*$", line.text.strip())
        if match:
            title = match.group(1).strip(" .-\t")
            page_label = match.group(2)
        else:
            title = re.sub(r"\.{3,}", " ", line.text).strip(" .-\t")
            page_label = None
        line.role = "toc_entry"
        line.toc_title = title or None
        line.toc_page_label = page_label
        if x_clusters:
            line.toc_level = min(
                range(len(x_clusters)), key=lambda index: abs(line.bbox.x0 - x_clusters[index])
            )
        else:
            line.toc_level = 0


def _normalize_repeated(text: str) -> str:
    text = re.sub(r"\d+", "#", text.casefold())
    return re.sub(r"\s+", " ", text).strip(" -–—|\t")


def classify_blocks(pages: list[Page], body_font_size: float) -> None:
    for page in pages:
        toc_hits = sum(bool(TOC_ENTRY_RE.search(block.text)) for block in page.blocks)
        has_toc_title = any("table of contents" in block.text.casefold() for block in page.blocks)
        if has_toc_title or toc_hits >= 2:
            page.kind = PageKind.TOC
        elif page.number <= 2:
            page.kind = PageKind.FRONT_MATTER

        for block in page.blocks:
            font_size = _block_font_size(block)
            font_ratio = font_size / body_font_size if body_font_size else 1.0
            text = block.text.strip()
            if page.kind == PageKind.TOC and text.casefold() == "table of contents":
                block.type = BlockType.HEADING
                block.confidence = 0.99
                block.heading_level = 2
                block.evidence = ["explicit_toc_title"]
            elif page.kind == PageKind.TOC and TOC_ENTRY_RE.search(text):
                block.type = BlockType.TOC_ENTRY
                block.confidence = 0.98
                block.evidence = ["toc_page", "dot_leader_and_terminal_page_number"]
            elif LIST_RE.match(text):
                block.type = BlockType.LIST_ITEM
                block.confidence = 0.92
                block.evidence = ["list_prefix"]
            elif (
                len(text) <= 180
                and (
                    font_ratio >= 1.22
                    or (text.isupper() and len(text.split()) <= 12)
                    or (
                        font_ratio >= 1.10
                        and abs((block.bbox.x0 + block.bbox.x1) / 2 - page.width / 2)
                        <= page.width * 0.06
                    )
                )
            ):
                block.type = BlockType.HEADING
                block.confidence = min(0.98, 0.72 + max(0.0, font_ratio - 1.0) * 0.5)
                block.heading_level = 1 if font_ratio >= 1.7 else 2 if font_ratio >= 1.35 else 3
                block.evidence = [f"font_ratio={font_ratio:.3f}"]
                if text.isupper():
                    block.evidence.append("short_uppercase")
                if abs((block.bbox.x0 + block.bbox.x1) / 2 - page.width / 2) <= page.width * 0.06:
                    block.evidence.append("horizontally_centered")
            else:
                block.type = BlockType.PARAGRAPH
                block.confidence = 0.85
                block.evidence = [f"body_font_ratio={font_ratio:.3f}"]

        heading_groups: dict[str, list[Block]] = defaultdict(list)
        for block in page.blocks:
            if block.type == BlockType.HEADING and block.bbox_norm.y0 <= 0.20:
                key = re.sub(r"\W+", "", block.text.casefold())
                heading_groups[key].append(block)
        for duplicates in heading_groups.values():
            if len(duplicates) < 2:
                continue
            keep = max(duplicates, key=_block_font_size)
            for block in duplicates:
                if block is not keep:
                    block.removed_as = "duplicate_heading"
                    block.evidence.append(f"equivalent_to={keep.id}")

    occurrences: dict[tuple[str, str], list[Block]] = defaultdict(list)
    for page in pages:
        for block in page.blocks:
            band = None
            if block.bbox_norm.y1 <= 0.13:
                band = "top"
            elif block.bbox_norm.y0 >= 0.87:
                band = "bottom"
            if band:
                occurrences[(band, _normalize_repeated(block.text))].append(block)

    for (_band, normalized), blocks in occurrences.items():
        if not normalized:
            continue
        repeated_pages = len({block.page for block in blocks})
        for block in blocks:
            if PAGE_NUMBER_RE.fullmatch(block.text.strip()):
                block.type = BlockType.PAGE_NUMBER
                block.removed_as = "page_number"
                block.confidence = 0.99
                block.evidence.append("page_number_pattern_in_margin_band")
            elif repeated_pages >= 3 and block.type == BlockType.PARAGRAPH:
                block.type = BlockType.HEADER if block.bbox_norm.y1 <= 0.13 else BlockType.FOOTER
                block.removed_as = block.type.value
                block.confidence = 0.96
                block.evidence.extend(["margin_band", f"repeated_on_{repeated_pages}_pages"])


def score_cross_page_merge(previous: Block, following: Block, pages: dict[int, Page]) -> MergeDecision:
    reasons: list[str] = []
    score = 0.0
    if previous.type != BlockType.PARAGRAPH or following.type != BlockType.PARAGRAPH:
        return MergeDecision(
            from_block=previous.id,
            to_block=following.id,
            from_page=previous.page,
            to_page=following.page,
            decision="keep_separate",
            score=0.0,
            reasons=["non_paragraph_boundary"],
        )
    if not TERMINAL_RE.search(previous.text.rstrip()):
        score += 0.35
        reasons.append("previous_lacks_terminal_punctuation")
    if following.text[:1].islower():
        score += 0.25
        reasons.append("following_starts_lowercase")
    previous_font = _block_font_size(previous)
    following_font = _block_font_size(following)
    if abs(previous_font - following_font) <= 1.0:
        score += 0.15
        reasons.append("font_size_compatible")
    previous_page = pages[previous.page]
    following_page = pages[following.page]
    if abs(previous.bbox.x0 - following.bbox.x0) <= 12:
        score += 0.10
        reasons.append("left_edge_compatible")
    previous_width = previous.bbox.x1 - previous.bbox.x0
    following_width = following.bbox.x1 - following.bbox.x0
    if abs(previous_width - following_width) <= 70:
        score += 0.05
        reasons.append("line_width_compatible")
    if previous.bbox.y1 / previous_page.height >= 0.80 and following.bbox.y0 / following_page.height <= 0.20:
        score += 0.10
        reasons.append("page_edge_continuation")

    score = round(score, 3)
    decision = "merge" if score >= 0.75 else "review" if score >= 0.55 else "keep_separate"
    return MergeDecision(
        from_block=previous.id,
        to_block=following.id,
        from_page=previous.page,
        to_page=following.page,
        decision=decision,
        score=score,
        reasons=reasons,
    )


def find_merges(pages: list[Page], selected_pages: set[int]) -> list[MergeDecision]:
    page_map = {page.number: page for page in pages}
    decisions: list[MergeDecision] = []
    for page_number in sorted(selected_pages):
        page = page_map[page_number]
        active_blocks = [block for block in page.blocks if not block.removed_as]
        for previous, following in zip(active_blocks, active_blocks[1:], strict=False):
            if previous.type != BlockType.PARAGRAPH or following.type != BlockType.PARAGRAPH:
                continue
            vertical_gap = following.bbox.y0 - previous.bbox.y1
            same_left_edge = abs(previous.bbox.x0 - following.bbox.x0) <= 5
            previous_font = _block_font_size(previous)
            following_font = _block_font_size(following)
            if (
                0 <= vertical_gap <= 5
                and same_left_edge
                and abs(previous_font - following_font) <= 0.5
                and not TERMINAL_RE.search(previous.text.rstrip())
                and following.text[:1].islower()
            ):
                decisions.append(
                    MergeDecision(
                        from_block=previous.id,
                        to_block=following.id,
                        from_page=page_number,
                        to_page=page_number,
                        decision="merge",
                        score=0.95,
                        reasons=[
                            f"small_vertical_gap={vertical_gap:.3f}",
                            "same_left_edge",
                            "font_size_compatible",
                            "previous_lacks_terminal_punctuation",
                            "following_starts_lowercase",
                        ],
                    )
                )
        if page_number + 1 not in selected_pages:
            continue
        previous_page = page_map[page_number]
        following_page = page_map[page_number + 1]
        previous_blocks = [block for block in previous_page.blocks if not block.removed_as]
        following_blocks = [block for block in following_page.blocks if not block.removed_as]
        if previous_blocks and following_blocks:
            decisions.append(
                score_cross_page_merge(previous_blocks[-1], following_blocks[0], page_map)
            )
    return decisions


def extract_document(pdf_path: Path, selected_pages: list[int] | None = None) -> Document:
    raw_pages = dictionary_output(str(pdf_path), sort=True, keep_chars=False)
    pages = [_convert_page(raw_page) for raw_page in raw_pages]
    page_count = len(pages)
    selected_pages = selected_pages or list(range(1, page_count + 1))
    body_font_size = _body_font_size(pages)
    split_mixed_blocks(pages, body_font_size)
    classify_blocks(pages, body_font_size)
    assign_toc_line_metadata(pages)
    selected = set(selected_pages)
    kept_pages = [page for page in pages if page.number in selected]
    enrich_document_styles(pdf_path, kept_pages)
    merges = find_merges(pages, selected)
    block_counts = Counter(block.type.value for page in kept_pages for block in page.blocks)
    return Document(
        source_path=str(pdf_path.resolve()),
        source_sha256=sha256_file(pdf_path),
        source_page_count=page_count,
        selected_pages=selected_pages,
        pages=kept_pages,
        body_font_size=body_font_size,
        merges=merges,
        metadata={"block_counts": dict(sorted(block_counts.items()))},
    )
