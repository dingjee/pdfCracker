"""Tests for ink-line measurement and seam support."""

from PIL import Image, ImageDraw

from pdfcracker.line_geometry import measure_lines, seam_support


def render_page(path, lines: list[tuple[int, int, int, int]]) -> None:
    image = Image.new("L", (400, 260), 255)
    draw = ImageDraw.Draw(image)
    for x0, y0, x1, y1 in lines:
        draw.rectangle((x0, y0, x1, y1), fill=0)
    image.save(path)


def test_measure_lines_finds_full_width_lines(tmp_path) -> None:
    page = tmp_path / "page.png"
    render_page(page, [(55, 55, 345, 64), (55, 75, 345, 84), (55, 95, 345, 104)])
    lines = measure_lines(page, (50, 50, 350, 120))
    assert len(lines) == 3
    first, _middle, last = lines
    assert 52 <= first[0] <= 60 and 340 <= last[1] <= 350


def spans(rows: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Turn (x0, y0, x1, y1) boxes into (x_start, x_end, y_start, y_end) spans."""
    return [(x0, x1, y0, y1) for x0, y0, x1, y1 in rows]


def test_seam_supported_by_full_line_then_unindented_line() -> None:
    end_lines = spans([(55, 55, 345, 64), (55, 75, 345, 84), (55, 95, 345, 104)])
    start_lines = spans([(55, 135, 345, 144), (55, 160, 345, 169)])
    support = seam_support(end_lines, 300, start_lines)
    assert support.supported
    assert support.left_line_fill >= 0.91
    assert abs(support.next_first_line_indent_px) <= 11


def test_seam_refused_when_the_next_first_line_is_indented() -> None:
    end_lines = spans([(55, 95, 345, 104)])
    start_lines = spans([(105, 135, 345, 144), (55, 160, 345, 169)])
    support = seam_support(end_lines, 300, start_lines)
    assert not support.supported
    assert support.next_first_line_indent_px > 11


def test_seam_refused_when_the_last_line_is_short() -> None:
    end_lines = spans([(55, 95, 150, 104)])  # a closing line, only half full
    start_lines = spans([(55, 135, 345, 144), (55, 160, 345, 169)])
    support = seam_support(end_lines, 300, start_lines)
    assert not support.supported
    assert support.left_line_fill < 0.91


def test_seam_refused_without_measurable_lines() -> None:
    assert not seam_support([], 300, spans([(55, 135, 345, 144)])).supported
