"""Tests for the opt-in scanned-page figure cropping module.

Rendering is mocked: ``pdfcracker.scan_figures.render_page_png`` is replaced
with an in-memory PIL page generator so no real PDF is ever touched.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from PIL import Image

import pdfcracker.scan_figures as scan_figures
from pdfcracker.scan_figures import ScanFigure, crop_scan_figures

PAGE_W = 100
PAGE_H = 200


def make_page_png(width=PAGE_W, height=PAGE_H, seed=0):
    """Deterministic page: each pixel encodes its (x, y) position."""
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = ((x + seed) % 256, y % 256, (x * 3 + seed) % 256)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class FakeRenderer:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, pdf_path, page_number, dpi):
        self.calls.append((pdf_path, page_number, dpi))
        return self.pages[page_number]


@pytest.fixture()
def renderer(monkeypatch):
    fake = FakeRenderer({1: make_page_png(seed=1), 2: make_page_png(seed=2)})
    monkeypatch.setattr(scan_figures, "render_page_png", fake)
    return fake


def test_crop_pixels_and_provenance(tmp_path, renderer):
    figures = [
        ScanFigure(page=1, printed_page=12, number="3-1", bbox_norm=(0.1, 0.2, 0.3, 0.25)),
        ScanFigure(page=2, printed_page=13, number="3-2", bbox_norm=(0.5, 0.0, 1.0, 1.0)),
    ]
    results = crop_scan_figures(Path("book.pdf"), tmp_path, figures, dpi=228)

    assert [r["asset_path"] for r in results] == [
        "images/p0012-fig-3-1.png",
        "images/p0013-fig-3-2.png",
    ]
    left, top, right, bottom = 10, 40, 30, 50
    assert results[0]["bbox_pixels"] == (left, top, right, bottom)
    assert results[0]["render_pixels"] == (PAGE_W, PAGE_H)
    assert results[0]["dpi"] == 228
    assert results[0]["page"] == 1
    assert results[0]["printed_page"] == 12
    assert results[0]["number"] == "3-1"
    assert results[0]["bbox_norm"] == (0.1, 0.2, 0.3, 0.25)

    # Pixel-exact crop: recompute the expected region from the raw page PNG.
    with Image.open(io.BytesIO(renderer.pages[1])) as page:
        expected = page.crop((left, top, right, bottom)).convert("RGB")
    asset = tmp_path / results[0]["asset_path"]
    with Image.open(asset) as got:
        assert got.tobytes() == expected.tobytes()

    assert results[0]["sha256"] == scan_figures._sha256(asset.read_bytes())
    assert results[0]["source_render_sha256"] == scan_figures._sha256(
        renderer.pages[1]
    )
    assert len(results[0]["source_render_sha256"]) == 64


def test_each_page_rendered_once(tmp_path, renderer):
    figures = [
        ScanFigure(page=1, printed_page=1, number="a", bbox_norm=(0.0, 0.0, 0.1, 0.1)),
        ScanFigure(page=1, printed_page=1, number="b", bbox_norm=(0.2, 0.2, 0.3, 0.3)),
        ScanFigure(page=1, printed_page=1, number="c", bbox_norm=(0.4, 0.4, 0.5, 0.5)),
        ScanFigure(page=2, printed_page=2, number="a", bbox_norm=(0.0, 0.0, 0.5, 0.5)),
    ]
    crop_scan_figures(Path("book.pdf"), tmp_path, figures)
    rendered_pages = [call[1] for call in renderer.calls]
    assert sorted(rendered_pages) == [1, 2]
    assert len(renderer.calls) == 2


@pytest.mark.parametrize(
    "bbox",
    [
        (-0.1, 0.0, 0.5, 0.5),
        (0.0, 0.0, 1.01, 0.5),
        (0.5, 0.0, 0.4, 0.5),  # x1 > x2
        (0.0, 0.6, 0.5, 0.6),  # degenerate height
        (0.0, 0.0, 1.0,),  # wrong length
        (0.0, 0.0, True, 0.5),  # bool is not a number
    ],
)
def test_invalid_bbox_rejected(tmp_path, renderer, bbox):
    figure = ScanFigure(page=1, printed_page=1, number="a", bbox_norm=bbox)
    with pytest.raises((ValueError, TypeError)):
        crop_scan_figures(Path("book.pdf"), tmp_path, [figure])


@pytest.mark.parametrize(
    "number",
    [
        "../escape",
        "a/b",
        "a\\b",
        "..",
        ".hidden-lead",
        "",
        "图3-1",
        "a b",
    ],
)
def test_dangerous_number_rejected(tmp_path, renderer, number):
    figure = ScanFigure(page=1, printed_page=1, number=number, bbox_norm=(0.0, 0.0, 0.5, 0.5))
    with pytest.raises(ValueError):
        crop_scan_figures(Path("book.pdf"), tmp_path, [figure])
    assert not list(tmp_path.rglob("*.png"))


def test_duplicate_target_rejected(tmp_path, renderer):
    figures = [
        ScanFigure(page=1, printed_page=1, number="a", bbox_norm=(0.0, 0.0, 0.5, 0.5)),
        ScanFigure(page=2, printed_page=1, number="a", bbox_norm=(0.0, 0.0, 0.5, 0.5)),
    ]
    with pytest.raises(ValueError, match="duplicate"):
        crop_scan_figures(Path("book.pdf"), tmp_path, figures)


def test_rerun_reuses_identical_asset_and_refuses_overwrite(tmp_path, renderer):
    figure = ScanFigure(page=1, printed_page=7, number="x", bbox_norm=(0.0, 0.0, 0.5, 0.5))
    first = crop_scan_figures(Path("book.pdf"), tmp_path, [figure])
    mtime = (tmp_path / first[0]["asset_path"]).stat().st_mtime_ns

    second = crop_scan_figures(Path("book.pdf"), tmp_path, [figure])
    assert second == first
    assert (tmp_path / first[0]["asset_path"]).stat().st_mtime_ns == mtime

    # Same target name but different pixels -> refuse to overwrite.
    renderer.pages[1] = make_page_png(seed=9)
    with pytest.raises(FileExistsError):
        crop_scan_figures(Path("book.pdf"), tmp_path, [figure])


def test_argument_validation(tmp_path, renderer):
    figure = ScanFigure(page=1, printed_page=1, number="a", bbox_norm=(0.0, 0.0, 0.5, 0.5))
    with pytest.raises(TypeError):
        crop_scan_figures("book.pdf", tmp_path, [figure])
    with pytest.raises(TypeError):
        crop_scan_figures(Path("book.pdf"), "out", [figure])
    with pytest.raises(TypeError):
        crop_scan_figures(Path("book.pdf"), tmp_path, "not-a-list")
    with pytest.raises(ValueError):
        crop_scan_figures(Path("book.pdf"), tmp_path, [figure], dpi=0)
    with pytest.raises(ValueError):
        crop_scan_figures(Path("book.pdf"), tmp_path, [figure], dpi=True)
    with pytest.raises(ValueError):
        crop_scan_figures(
            Path("book.pdf"),
            tmp_path,
            [ScanFigure(page=0, printed_page=1, number="a", bbox_norm=(0.0, 0.0, 0.5, 0.5))],
        )
    with pytest.raises(TypeError):
        crop_scan_figures(Path("book.pdf"), tmp_path, [object()])
    # No assets may be produced by any rejected call.
    assert not list(tmp_path.rglob("*.png"))


def test_number_pattern_accepts_expected_forms(tmp_path, renderer):
    assert _ASSET_NUMBER_PATTERN().match("3-1")
    figures = [
        ScanFigure(page=1, printed_page=3, number="Fig.2a", bbox_norm=(0.0, 0.0, 0.1, 0.1)),
        ScanFigure(page=2, printed_page=3, number="plate_IV", bbox_norm=(0.0, 0.0, 0.1, 0.1)),
    ]
    results = crop_scan_figures(Path("book.pdf"), tmp_path, figures)
    assert [r["asset_path"] for r in results] == [
        "images/p0003-fig-Fig.2a.png",
        "images/p0003-fig-plate_IV.png",
    ]


def _ASSET_NUMBER_PATTERN():
    return re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
