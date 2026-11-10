"""Ink-line measurement on rendered pages for paragraph-seam evidence.

A seam between two blocks is supported by geometry when the left block's final
line runs nearly the full column width (the sentence is unfinished) and the
right block's first line is not indented (no new paragraph starts).  Both
facts are measured from ink, not from OCR text, so they stay evidence when the
text itself is uncertain.  Thresholds are the values validated on a scanned
book at 192 dpi renders; pass overrides for other render scales.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

#: One ink line as (x_start, x_end, y_start, y_end) in page pixels.
LineSpan = tuple[int, int, int, int]

#: Ink darker than this grayscale value counts as text.
INK_THRESHOLD = 150
#: Ink-row/-column activation thresholds, in pixels at the validated scale.
MIN_ROW_INK = 7
MIN_COLUMN_INK = 3
MIN_ACTIVE_COLUMNS = 22
#: Plausible text-line height range in pixels at the validated scale.
MIN_LINE_HEIGHT = 9
MAX_LINE_HEIGHT = 46
#: Rows further apart than this belong to different lines.
MAX_ROW_GAP = 4
#: Vertical padding around a region crop, in pixels.
CROP_PADDING = 7


def measure_lines(image_path: Path, bbox: tuple[int, int, int, int]) -> list[LineSpan]:
    """Detect ink text lines inside a pixel ``bbox`` (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = bbox
    with Image.open(image_path) as image:
        crop = image.convert("L").crop((x0, max(0, y0 - CROP_PADDING), x1, min(image.height, y1 + CROP_PADDING)))
    ink = np.asarray(crop) < INK_THRESHOLD
    active = np.flatnonzero(ink.sum(axis=1) >= max(MIN_ROW_INK, round((x1 - x0) * 0.015)))
    if not len(active):
        return []
    intervals = np.split(active, np.flatnonzero(np.diff(active) > MAX_ROW_GAP) + 1)
    lines: list[LineSpan] = []
    for rows in intervals:
        start, end = int(rows[0]), int(rows[-1]) + 1
        if not MIN_LINE_HEIGHT <= end - start <= MAX_LINE_HEIGHT:
            continue
        columns = np.flatnonzero(ink[start:end].sum(axis=0) >= MIN_COLUMN_INK)
        if len(columns) >= MIN_ACTIVE_COLUMNS:
            lines.append((x0 + int(columns[0]), x0 + int(columns[-1]), y0 - CROP_PADDING + start, y0 - CROP_PADDING + end))
    return lines


@dataclass(frozen=True)
class SeamSupport:
    """Geometric evidence for one paragraph seam."""

    left_line_fill: float
    next_first_line_indent_px: float
    supported: bool


def seam_support(
    end_lines: list[LineSpan],
    end_region_width: int,
    start_lines: list[LineSpan],
    *,
    min_fill: float = 0.91,
    max_abs_indent: float = 11.0,
) -> SeamSupport:
    """Judge a seam from the ink lines of its two boundary regions.

    ``end_lines`` are the measured lines of the left (earlier) region and
    ``start_lines`` those of the right (later) region; ``end_region_width`` is
    the pixel width of the left region's column.
    """
    if len(end_lines) < 1 or len(start_lines) < 2:
        return SeamSupport(0.0, 0.0, False)
    final_width = end_lines[-1][1] - end_lines[-1][0]
    indent = float(start_lines[0][0] - float(np.median([line[0] for line in start_lines[1:3]])))
    fill = final_width / max(1, end_region_width)
    return SeamSupport(round(fill, 4), indent, bool(fill >= min_fill and abs(indent) <= max_abs_indent))
