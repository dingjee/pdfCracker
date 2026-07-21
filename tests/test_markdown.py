from pdfcracker.markdown import render_inline
from pdfcracker.models import BBox, Block, FontInfo, Line, Span


def test_nested_styles_use_semantic_html() -> None:
    bbox = BBox(x0=0, y0=0, x1=10, y1=10)
    span = Span(
        text="important",
        bbox=bbox,
        font=FontInfo(name="Example", size=12, weight=700),
        bold=True,
        italic=True,
        underline=True,
        semantic_color="red",
        color_rgb=(0.75, 0.0, 0.0),
    )
    block = Block(
        id="test",
        page=1,
        source_block_index=0,
        bbox=bbox,
        bbox_norm=bbox,
        raw_text="important",
        text="important",
        lines=[Line(text="important", bbox=bbox, spans=[span])],
    )
    assert render_inline(block) == (
        '<strong><em><u><span class="text-red">important</span></u></em></strong>'
    )
