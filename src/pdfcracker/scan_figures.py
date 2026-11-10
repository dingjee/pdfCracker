"""Crop scanned-page figures into reusable image assets.

This module is **explicit opt-in only**: it is called solely when a human
has authorised figure extraction from scanned pages for a given book. The
native ``extract``/``process`` entry points never invoke it.

Each :class:`ScanFigure` describes one figure by its normalised ``[0, 1]``
xyxy bounding box relative to the whole page (origin at the top-left of the
page, before cropping). Pages are rendered exactly once each via
:func:`pdfcracker.ocr.render_page_png`, cropped with PIL, and written
atomically under ``output_root/images/``.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from pdfcracker.ocr import render_page_png

__all__ = ["ScanFigure", "crop_scan_figures"]

_ASSET_NUMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_IMAGE_DIR = "images"


@dataclass(frozen=True)
class ScanFigure:
    """One scanned-page figure to crop out.

    ``page`` is the 1-based PDF page number (as accepted by
    ``pdfcracker.ocr.render_page_png``). ``printed_page`` is the printed
    page label used in the asset filename. ``bbox_norm`` is the xyxy
    bounding box relative to the whole page, normalised to ``[0, 1]``,
    origin at the page top-left.
    """

    page: int
    printed_page: int
    number: str
    bbox_norm: tuple[float, float, float, float]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_path(name: str, value: Path) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{name} must be a pathlib.Path, got {type(value).__name__}")
    return value


def _validate_figure(figure: ScanFigure) -> None:
    if not isinstance(figure, ScanFigure):
        raise TypeError(f"figures must contain ScanFigure, got {type(figure).__name__}")
    if isinstance(figure.page, bool) or not isinstance(figure.page, int) or figure.page < 1:
        raise ValueError(f"figure.page must be an integer >= 1, got {figure.page!r}")
    if (
        isinstance(figure.printed_page, bool)
        or not isinstance(figure.printed_page, int)
        or figure.printed_page < 1
    ):
        raise ValueError(
            f"figure.printed_page must be an integer >= 1, got {figure.printed_page!r}"
        )
    if not isinstance(figure.number, str) or not _ASSET_NUMBER_RE.fullmatch(figure.number):
        raise ValueError(
            "figure.number must be non-empty and contain only "
            f"[A-Za-z0-9._-], starting with an alphanumeric, got {figure.number!r}"
        )
    bbox = figure.bbox_norm
    if (
        not isinstance(bbox, tuple)
        or len(bbox) != 4
        or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in bbox)
    ):
        raise ValueError(f"bbox_norm must be a tuple of 4 numbers, got {bbox!r}")
    x1, y1, x2, y2 = (float(v) for v in bbox)
    if not all(0.0 <= v <= 1.0 for v in (x1, y1, x2, y2)):
        raise ValueError(f"bbox_norm values must lie in [0, 1], got {bbox!r}")
    if not x1 < x2 or not y1 < y2:
        raise ValueError(f"bbox_norm must be non-degenerate (x1<x2, y1<y2), got {bbox!r}")


def _pixel_box(
    bbox: tuple[float, float, float, float], width: int, height: int
) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox
    left = math.floor(x1 * width)
    top = math.floor(y1 * height)
    right = math.ceil(x2 * width)
    bottom = math.ceil(y2 * height)
    left = max(0, min(left, width - 1))
    top = max(0, min(top, height - 1))
    right = max(left + 1, min(right, width))
    bottom = max(top + 1, min(bottom, height))
    return left, top, right, bottom


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        try:
            os.link(tmp_name, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != data:
                raise FileExistsError(
                    f"refusing to overwrite existing asset: {path}"
                ) from None
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def crop_scan_figures(
    pdf_path: Path,
    output_root: Path,
    figures: Sequence[ScanFigure],
    *,
    dpi: int = 228,
) -> list[dict]:
    """Crop ``figures`` from scanned pages and write PNG assets.

    Every page referenced by ``figures`` is rendered exactly once via
    :func:`pdfcracker.ocr.render_page_png`. Crops use floor for the
    left/top edge and ceil for the right/bottom edge, clamped to the page.
    Assets land in ``output_root/images/`` and are written atomically.
    Re-running with identical content reuses the existing asset; a path
    collision with *different* content raises ``FileExistsError``.

    Returns one dict per figure with provenance fields: ``page``,
    ``printed_page``, ``number``, ``bbox_norm``, ``bbox_pixels``,
    ``render_pixels``, ``dpi``, ``source_render_sha256``, ``asset_path``
    (POSIX, relative to ``output_root``) and ``sha256``.
    """
    _validate_path("pdf_path", pdf_path)
    _validate_path("output_root", output_root)
    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi < 1:
        raise ValueError(f"dpi must be an integer >= 1, got {dpi!r}")
    if isinstance(figures, (str, bytes)) or not isinstance(figures, Sequence):
        raise TypeError("figures must be a sequence of ScanFigure")
    for figure in figures:
        _validate_figure(figure)

    # Refuse duplicate target filenames up front.
    targets: set[str] = set()
    for figure in figures:
        key = f"{_IMAGE_DIR}/p{figure.printed_page:04d}-fig-{figure.number}.png"
        if key.casefold() in targets:
            raise ValueError(f"duplicate figure target asset: {key}")
        targets.add(key.casefold())

    from PIL import Image

    renders: dict[int, tuple[bytes, int, int]] = {}
    results: list[dict] = []
    image_dir = output_root / _IMAGE_DIR
    if image_dir.is_symlink():
        raise ValueError(f"image directory must not be a symlink: {image_dir}")
    image_dir.mkdir(parents=True, exist_ok=True)

    for figure in figures:
        if figure.page not in renders:
            png = render_page_png(pdf_path, figure.page, dpi)
            with Image.open(io.BytesIO(png)) as page_image:
                renders[figure.page] = (png, page_image.width, page_image.height)
        png, width, height = renders[figure.page]
        left, top, right, bottom = _pixel_box(figure.bbox_norm, width, height)
        with Image.open(io.BytesIO(png)) as page_image:
            crop = page_image.crop((left, top, right, bottom)).convert("RGB")
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        data = buffer.getvalue()

        relative = Path(_IMAGE_DIR) / (
            f"p{figure.printed_page:04d}-fig-{figure.number}.png"
        )
        asset_path = output_root / relative
        digest = _sha256(data)
        _atomic_write(asset_path, data)

        results.append(
            {
                "page": figure.page,
                "printed_page": figure.printed_page,
                "number": figure.number,
                "bbox_norm": figure.bbox_norm,
                "bbox_pixels": (left, top, right, bottom),
                "render_pixels": (width, height),
                "dpi": dpi,
                "source_render_sha256": _sha256(png),
                "asset_path": relative.as_posix(),
                "sha256": digest,
            }
        )
    return results
