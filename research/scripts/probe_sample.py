from __future__ import annotations

import json
import math
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import pdfplumber


def normalize_running_text(text: str) -> str:
    text = re.sub(r"\d+", "#", text.casefold())
    return re.sub(r"\s+", " ", text).strip(" -–—|\t")


def group_lines(words: list[dict], tolerance: float = 2.5) -> list[dict]:
    groups: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (float(w["top"]), float(w["x0"]))):
        for group in groups:
            if abs(statistics.median(float(w["top"]) for w in group) - float(word["top"])) <= tolerance:
                group.append(word)
                break
        else:
            groups.append([word])
    lines = []
    for group in groups:
        ordered = sorted(group, key=lambda w: float(w["x0"]))
        lines.append(
            {
                "text": " ".join(w["text"] for w in ordered),
                "x0": min(float(w["x0"]) for w in ordered),
                "x1": max(float(w["x1"]) for w in ordered),
                "top": min(float(w["top"]) for w in ordered),
                "bottom": max(float(w["bottom"]) for w in ordered),
            }
        )
    return sorted(lines, key=lambda line: (line["top"], line["x0"]))


def patch_count(width: int, height: int) -> int:
    return math.ceil(width / 32) * math.ceil(height / 32)


def main(pdf_path: Path, out_path: Path) -> None:
    page_rows = []
    band_occurrences: dict[str, list[dict]] = defaultdict(list)
    with pdfplumber.open(pdf_path) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(
                x_tolerance=2,
                y_tolerance=2,
                keep_blank_chars=False,
                use_text_flow=False,
                extra_attrs=["fontname", "size"],
            )
            lines = group_lines(words)
            height = float(page.height)
            top_lines = [line for line in lines if line["top"] <= height * 0.14]
            bottom_lines = [line for line in lines if line["bottom"] >= height * 0.86]
            for band, candidates in (("top", top_lines), ("bottom", bottom_lines)):
                for line in candidates:
                    normalized = normalize_running_text(line["text"])
                    if normalized:
                        band_occurrences[f"{band}:{normalized}"].append(
                            {"page": page_no, "top_ratio": line["top"] / height, "text": line["text"]}
                        )
            body_lines = [line for line in lines if line not in top_lines and line not in bottom_lines]
            page_rows.append(
                {
                    "page": page_no,
                    "width_pt": float(page.width),
                    "height_pt": height,
                    "chars": len(page.chars),
                    "words": len(words),
                    "lines": len(lines),
                    "images": len(page.images),
                    "top_lines": [line["text"] for line in top_lines],
                    "bottom_lines": [line["text"] for line in bottom_lines],
                    "first_body_line": body_lines[0]["text"] if body_lines else "",
                    "last_body_line": body_lines[-1]["text"] if body_lines else "",
                }
            )

    repeated = []
    for key, occurrences in band_occurrences.items():
        pages = sorted({item["page"] for item in occurrences})
        if len(pages) >= 3:
            repeated.append(
                {
                    "pattern": key,
                    "page_count": len(pages),
                    "page_fraction": round(len(pages) / len(page_rows), 4),
                    "example": occurrences[0]["text"],
                    "pages": pages[:20],
                    "top_ratio_median": round(statistics.median(item["top_ratio"] for item in occurrences), 4),
                }
            )
    repeated.sort(key=lambda item: (-item["page_count"], item["pattern"]))

    nonempty = [row for row in page_rows if row["chars"]]
    dense = sorted(nonempty, key=lambda row: row["chars"], reverse=True)
    image_pages = [row for row in page_rows if row["images"]]
    low_text = sorted(nonempty, key=lambda row: row["chars"])
    candidate_pages = []
    for row in (
        page_rows[:8]
        + dense[:5]
        + image_pages[:5]
        + low_text[:5]
        + [page_rows[len(page_rows) // 2], page_rows[-2], page_rows[-1]]
    ):
        if row["page"] not in candidate_pages:
            candidate_pages.append(row["page"])

    result = {
        "pdf": str(pdf_path),
        "page_count": len(page_rows),
        "text_layer_pages": sum(bool(row["chars"]) for row in page_rows),
        "empty_text_pages": [row["page"] for row in page_rows if not row["chars"]],
        "total_chars": sum(row["chars"] for row in page_rows),
        "total_words": sum(row["words"] for row in page_rows),
        "median_chars_per_nonempty_page": statistics.median(row["chars"] for row in nonempty),
        "median_words_per_nonempty_page": statistics.median(row["words"] for row in nonempty),
        "pages_with_images": [row["page"] for row in image_pages],
        "repeated_band_candidates": repeated[:30],
        "candidate_visual_pages": candidate_pages,
        "cross_page_boundaries": [
            {
                "from_page": page_rows[i]["page"],
                "last": page_rows[i]["last_body_line"],
                "to_page": page_rows[i + 1]["page"],
                "first": page_rows[i + 1]["first_body_line"],
            }
            for i in range(len(page_rows) - 1)
        ],
        "page_rows": page_rows,
        "vision_patch_examples_letter": [
            {
                "dpi": dpi,
                "width_px": round(612 * dpi / 72),
                "height_px": round(792 * dpi / 72),
                "gpt_5_6_original_patch_units": patch_count(
                    round(612 * dpi / 72), round(792 * dpi / 72)
                ),
            }
            for dpi in (72, 96, 120, 144, 168, 180, 216, 300)
        ],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
