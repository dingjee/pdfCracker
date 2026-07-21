from __future__ import annotations

import colorsys
import re
import statistics
from pathlib import Path
from typing import Any

import pdfplumber
from pypdf import PdfReader

from pdfcracker.models import BBox, BlockType, FontInfo, Line, Page, Span


def _rgb(color: Any) -> tuple[float, float, float] | None:
    if isinstance(color, (int, float)):
        value = float(color)
        return (value, value, value)
    if not isinstance(color, (tuple, list)):
        return None
    values = tuple(float(value) for value in color)
    if len(values) == 1:
        return (values[0], values[0], values[0])
    if len(values) == 3:
        return values
    if len(values) == 4:
        cyan, magenta, yellow, black = values
        return (
            1 - min(1.0, cyan + black),
            1 - min(1.0, magenta + black),
            1 - min(1.0, yellow + black),
        )
    return None


def semantic_color_name(rgb: tuple[float, float, float] | None) -> str | None:
    if rgb is None or max(rgb) - min(rgb) <= 0.08:
        return None
    hue, saturation, _value = colorsys.rgb_to_hsv(*rgb)
    if saturation < 0.18:
        return None
    degrees = hue * 360
    if degrees < 20 or degrees >= 340:
        return "red"
    if degrees < 70:
        return "yellow"
    if degrees < 165:
        return "green"
    if degrees < 255:
        return "blue"
    if degrees < 315:
        return "purple"
    return "red"


def _horizontal_objects(page: Any) -> list[dict[str, float]]:
    objects: list[dict[str, float]] = []
    for obj in [*page.lines, *page.rects]:
        width = abs(float(obj["x1"]) - float(obj["x0"]))
        thickness = abs(float(obj["bottom"]) - float(obj["top"]))
        if width >= 8 and thickness <= 3:
            objects.append(
                {
                    "x0": float(obj["x0"]),
                    "x1": float(obj["x1"]),
                    "top": float(obj["top"]),
                    "thickness": thickness,
                }
            )
    return objects


def _synthetic_bold_runs(reader_page: Any) -> list[dict[str, float | str]]:
    state: dict[str, Any] = {"render_mode": 0, "stack": []}
    runs: list[dict[str, float | str]] = []

    def before(operator: bytes, operands: list[Any], _cm: Any, _tm: Any) -> None:
        if operator == b"q":
            state["stack"].append(state["render_mode"])
        elif operator == b"Q":
            state["render_mode"] = state["stack"].pop() if state["stack"] else 0
        elif operator == b"Tr":
            state["render_mode"] = int(operands[0])

    def visit_text(text: str, _cm: Any, tm: Any, _font: Any, font_size: float) -> None:
        if state["render_mode"] not in {1, 2, 5, 6} or not text.strip():
            return
        x, baseline_y = float(tm[4]), float(tm[5])
        if x == 0 and baseline_y == 0:
            return
        runs.append(
            {
                "text": text,
                "x": x,
                "baseline_y": baseline_y,
                "font_size": float(font_size),
                "render_mode": int(state["render_mode"]),
            }
        )

    reader_page.extract_text(visitor_operand_before=before, visitor_text=visit_text)
    return runs


def _match_synthetic_indices(
    chars: list[dict[str, Any]],
    line: Line,
    page_height: float,
    runs: list[dict[str, float | str]],
) -> set[int]:
    text = "".join(str(char.get("text", "")) for char in chars)
    matched: set[int] = set()
    for run in runs:
        baseline_top = page_height - float(run["baseline_y"])
        if not (line.bbox.y0 <= baseline_top <= line.bbox.y1 + 3):
            continue
        target = str(run["text"])
        positions: list[int] = []
        start = 0
        while target and (position := text.find(target, start)) >= 0:
            positions.append(position)
            start = position + 1
        if not positions:
            target = target.strip()
            start = 0
            while target and (position := text.find(target, start)) >= 0:
                positions.append(position)
                start = position + 1
        if not positions:
            continue
        run_x = float(run["x"])
        position = min(positions, key=lambda item: abs(float(chars[item]["x0"]) - run_x))
        matched.update(range(position, min(len(chars), position + len(target))))
    return matched


def _line_chars(page: Any, line: Line) -> list[dict[str, Any]]:
    chars = [
        char
        for char in page.chars
        if line.bbox.x0 - 1 <= (float(char["x0"]) + float(char["x1"])) / 2 <= line.bbox.x1 + 1
        and line.bbox.y0 - 1 <= (float(char["top"]) + float(char["bottom"])) / 2 <= line.bbox.y1 + 1
    ]
    return sorted(chars, key=lambda char: float(char["x0"]))


def _styled_spans_for_line(
    page: Any,
    line: Line,
    horizontal: list[dict[str, float]],
    synthetic_runs: list[dict[str, float | str]],
) -> list[Span]:
    chars = _line_chars(page, line)
    if not chars:
        return line.spans
    synthetic_indices = _match_synthetic_indices(chars, line, float(page.height), synthetic_runs)
    styled_chars: list[dict[str, Any]] = []
    for index, char in enumerate(chars):
        font_name = str(char.get("fontname", ""))
        folded_font = font_name.casefold()
        center_x = (float(char["x0"]) + float(char["x1"])) / 2
        underlined = any(
            vector["x0"] <= center_x <= vector["x1"]
            and float(char["top"]) <= vector["top"] <= float(char["bottom"]) + 2.5
            for vector in horizontal
        )
        rgb = _rgb(char.get("non_stroking_color"))
        color_name = semantic_color_name(rgb)
        evidence: list[str] = []
        bold = index in synthetic_indices or any(
            token in folded_font for token in ("bold", "black", "semibold", "demi")
        )
        italic = any(token in folded_font for token in ("italic", "oblique"))
        if index in synthetic_indices:
            evidence.append("pdf_text_render_mode_stroke")
        elif bold:
            evidence.append("font_name_bold")
        if italic:
            evidence.append("font_name_italic")
        if underlined:
            evidence.append("vector_underline_at_baseline")
        if color_name:
            evidence.append("non_gray_text_fill")
        styled_chars.append(
            {
                "text": str(char.get("text", "")),
                "bbox": BBox(
                    x0=float(char["x0"]),
                    y0=float(char["top"]),
                    x1=float(char["x1"]),
                    y1=float(char["bottom"]),
                ),
                "font": FontInfo(
                    name=font_name,
                    size=float(char.get("size", 0.0)),
                    weight=700 if bold else 400,
                    flags=0,
                ),
                "bold": bold,
                "italic": italic,
                "underline": underlined,
                "semantic_color": color_name,
                "color_rgb": rgb if color_name else None,
                "style_evidence": evidence,
            }
        )

    word: list[dict[str, Any]] = []
    for item in [*styled_chars, {"text": " "}]:
        if str(item["text"]).isspace():
            if word:
                alpha_items = [part for part in word if any(char.isalnum() for char in str(part["text"]))]
                for attribute in ("bold", "italic", "underline"):
                    styled_count = sum(bool(part[attribute]) for part in alpha_items)
                    if alpha_items and styled_count:
                        promoted = list(alpha_items)
                        for index, part in enumerate(word[1:-1], start=1):
                            if (
                                not any(char.isalnum() for char in str(part["text"]))
                                and any(char.isalnum() for char in str(word[index - 1]["text"]))
                                and any(char.isalnum() for char in str(word[index + 1]["text"]))
                            ):
                                promoted.append(part)
                        for part in promoted:
                            part[attribute] = True
                            evidence = part["style_evidence"]
                            marker = f"word_boundary_normalized_{attribute}"
                            if marker not in evidence:
                                evidence.append(marker)
                word = []
        else:
            word.append(item)

    spans: list[Span] = []
    for item in styled_chars:
        style_key = (
            item["bold"],
            item["italic"],
            item["underline"],
            item["semantic_color"],
            item["color_rgb"],
            tuple(item["style_evidence"]),
        )
        if spans:
            previous = spans[-1]
            previous_key = (
                previous.bold,
                previous.italic,
                previous.underline,
                previous.semantic_color,
                previous.color_rgb,
                tuple(previous.style_evidence),
            )
        else:
            previous_key = None
        if spans and previous_key == style_key:
            previous.text += str(item["text"])
            previous.bbox.x1 = max(previous.bbox.x1, item["bbox"].x1)
            previous.bbox.y0 = min(previous.bbox.y0, item["bbox"].y0)
            previous.bbox.y1 = max(previous.bbox.y1, item["bbox"].y1)
        else:
            spans.append(Span(**item))
    return spans


def _nonwhite_fill(rect: dict[str, Any]) -> bool:
    if not rect.get("fill"):
        return False
    rgb = _rgb(rect.get("non_stroking_color"))
    return rgb is not None and statistics.mean(rgb) < 0.97


def _is_boxed_callout(block: Any, page: Any) -> bool:
    relevant = [
        rect
        for rect in page.rects
        if _nonwhite_fill(rect)
        and abs(float(rect["bottom"]) - float(rect["top"])) > 3
        and float(rect["x1"]) >= block.bbox.x1 - 5
        and float(rect["x0"]) <= block.bbox.x0 + 5
        and float(rect["bottom"]) >= block.bbox.y0
        and float(rect["top"]) <= block.bbox.y1
    ]
    if not relevant:
        return False
    return (
        min(float(rect["top"]) for rect in relevant) <= block.bbox.y0 + 2
        and max(float(rect["bottom"]) for rect in relevant) >= block.bbox.y1 - 2
    )


def _is_indented_quote_block(block: Any, body_left: float) -> bool:
    lines = [line.text.strip() for line in block.lines if line.text.strip()]
    return (
        block.bbox.x0 >= body_left + 24
        and bool(lines)
        and all(re.match(r'^[“\"].+[”\"]$', line) for line in lines)
    )


def enrich_page_styles(model_page: Page, plumber_page: Any, reader_page: Any) -> None:
    horizontal = _horizontal_objects(plumber_page)
    synthetic_runs = _synthetic_bold_runs(reader_page)
    body_left_candidates = [
        block.bbox.x0
        for block in model_page.blocks
        if block.type == BlockType.PARAGRAPH and block.bbox.x0 < model_page.width * 0.35
    ]
    body_left = statistics.median(body_left_candidates) if body_left_candidates else model_page.width * 0.18
    for block in model_page.blocks:
        for line in block.lines:
            line.spans = _styled_spans_for_line(plumber_page, line, horizontal, synthetic_runs)
        visible_spans = [
            span for line in block.lines for span in line.spans if span.text.strip()
        ]
        visible_chars = sum(len(span.text.strip()) for span in visible_spans)
        bold_chars = sum(len(span.text.strip()) for span in visible_spans if span.bold)
        if block.type == BlockType.HEADING and visible_chars and bold_chars / visible_chars >= 0.65:
            for span in visible_spans:
                span.bold = True
                if "heading_majority_bold_promoted" not in span.style_evidence:
                    span.style_evidence.append("heading_majority_bold_promoted")
        if block.type == BlockType.PARAGRAPH and _is_boxed_callout(block, plumber_page):
            block.type = BlockType.QUOTE
            block.quote_kind = "callout"
            block.confidence = 0.98
            block.evidence.append("enclosed_by_nonwhite_pdf_rectangle")
        elif block.type == BlockType.PARAGRAPH and _is_indented_quote_block(block, body_left):
            block.type = BlockType.QUOTE
            block.quote_kind = "line_quotes"
            block.confidence = 0.96
            block.evidence.extend(["indented_from_body", "each_line_is_quoted"])


def enrich_document_styles(pdf_path: Path, pages: list[Page]) -> None:
    reader = PdfReader(str(pdf_path))
    with pdfplumber.open(pdf_path) as plumber:
        for page in pages:
            enrich_page_styles(page, plumber.pages[page.number - 1], reader.pages[page.number - 1])
