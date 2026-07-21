from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any


def is_semantic_color(color: Any, channel_delta: float = 0.08) -> bool:
    return (
        isinstance(color, (tuple, list))
        and len(color) >= 3
        and max(float(value) for value in color[:3]) - min(float(value) for value in color[:3])
        > channel_delta
    )


def _group_colored_runs(chars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = [char for char in chars if char.get("text", "").strip() and is_semantic_color(char.get("non_stroking_color"))]
    selected.sort(key=lambda char: (round(float(char["top"]), 1), float(char["x0"])))
    runs: list[dict[str, Any]] = []
    for char in selected:
        color = tuple(float(value) for value in char["non_stroking_color"][:3])
        if (
            runs
            and runs[-1]["color"] == color
            and abs(runs[-1]["top"] - float(char["top"])) <= 1.0
            and float(char["x0"]) - runs[-1]["x1"] <= 8.0
        ):
            runs[-1]["text"] += char["text"]
            runs[-1]["x1"] = float(char["x1"])
        else:
            runs.append(
                {
                    "text": char["text"],
                    "color": color,
                    "x0": float(char["x0"]),
                    "x1": float(char["x1"]),
                    "top": float(char["top"]),
                    "bottom": float(char["bottom"]),
                }
            )
    return runs


def _horizontal_objects(page: Any) -> list[dict[str, float]]:
    output: list[dict[str, float]] = []
    for obj in [*page.lines, *page.rects]:
        width = abs(float(obj["x1"]) - float(obj["x0"]))
        height = abs(float(obj["bottom"]) - float(obj["top"]))
        if width >= 20 and height <= 3:
            output.append(
                {
                    "x0": float(obj["x0"]),
                    "x1": float(obj["x1"]),
                    "top": float(obj["top"]),
                    "thickness": height,
                }
            )
    return output


def _underline_evidence(page: Any, horizontal: list[dict[str, float]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for line in horizontal:
        chars = [
            char
            for char in page.chars
            if char.get("text", "").strip()
            and float(char["x1"]) >= line["x0"]
            and float(char["x0"]) <= line["x1"]
            and float(char["top"]) <= line["top"] <= float(char["bottom"]) + 2.5
        ]
        if not chars:
            continue
        chars.sort(key=lambda char: float(char["x0"]))
        evidence.append(
            {
                "text": "".join(char["text"] for char in chars).strip(),
                "line": line,
                "reason": "horizontal_vector_overlaps_text_baseline",
            }
        )
    return evidence


def probe_styles(pdf_path: Path, pages: list[int]) -> list[dict[str, Any]]:
    try:
        import pdfplumber
    except ImportError as error:
        raise RuntimeError("Install locked project dependencies with: uv sync --locked") from error

    results: list[dict[str, Any]] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_number in pages:
            page = pdf.pages[page_number - 1]
            font_counts = Counter(
                (
                    str(char.get("fontname", "")),
                    round(float(char.get("size", 0.0)), 3),
                    str(char.get("non_stroking_color")),
                )
                for char in page.chars
                if char.get("text", "").strip()
            )
            deduped = page.dedupe_chars(tolerance=1, extra_attrs=("fontname", "size"))
            horizontal = _horizontal_objects(page)
            results.append(
                {
                    "page": page_number,
                    "raw_char_count": len(page.chars),
                    "deduped_char_count": len(deduped.chars),
                    "duplicate_char_count": len(page.chars) - len(deduped.chars),
                    "font_color_counts": [
                        {
                            "font": key[0],
                            "size": key[1],
                            "color": key[2],
                            "chars": count,
                        }
                        for key, count in font_counts.most_common()
                    ],
                    "semantic_color_runs": _group_colored_runs(page.chars),
                    "horizontal_objects": horizontal,
                    "underline_evidence": _underline_evidence(page, horizontal),
                }
            )
    return results
