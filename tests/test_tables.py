from pdfcracker.markdown import render_markdown
from pdfcracker.models import BBox, Block, BlockType, Document, Line, NativeTable, Page
from pdfcracker.tables import materialize_table


def test_materialized_native_table_replaces_overlapping_text() -> None:
    bbox = BBox(x0=0, y0=0, x1=100, y1=60)
    block = Block(
        id="p0001-b0001",
        page=1,
        source_block_index=0,
        bbox=bbox,
        bbox_norm=BBox(x0=0, y0=0, x1=1, y1=0.6),
        raw_text="Left Right",
        text="Left Right",
        lines=[Line(text="Left Right", bbox=bbox)],
        type=BlockType.PARAGRAPH,
        confidence=0.9,
    )
    table = NativeTable(
        id="p0001-table001",
        page=1,
        bbox=bbox,
        bbox_norm=BBox(x0=0, y0=0, x1=1, y1=0.6),
        raw_rows=[["Left", "", "Right", ""], ["A", "", "B", ""]],
        source_column_count=4,
    )
    document = Document(
        source_path="sample.pdf",
        source_sha256="a" * 64,
        source_page_count=1,
        selected_pages=[1],
        pages=[Page(number=1, source_index=0, width=100, height=100, blocks=[block])],
        body_font_size=12,
        tables=[table],
    )
    materialize_table(document, table, 2)
    markdown = render_markdown(document)
    assert block.removed_as == "table_text"
    assert "| Left | Right |" in markdown
    assert "| A | B |" in markdown


def test_parallel_bullet_continuations_are_joined_per_column() -> None:
    table = NativeTable(
        id="p0001-table001",
        page=1,
        bbox=BBox(x0=0, y0=0, x1=100, y1=100),
        bbox_norm=BBox(x0=0, y0=0, x1=1, y1=1),
        raw_rows=[
            ["", "Left", "", "", "Right", ""],
            ["", "\uf0b7 First left", "", "", "\uf0b7 First right", ""],
            ["", "left continuation", "", "", "right continuation", ""],
            ["", "\uf0b7 Second-", "", "", "\uf0b7 Second right", ""],
            ["", "left", "", "", "", ""],
        ],
        source_column_count=6,
    )
    document = Document(
        source_path="sample.pdf",
        source_sha256="a" * 64,
        source_page_count=1,
        selected_pages=[1],
        pages=[Page(number=1, source_index=0, width=100, height=100)],
        body_font_size=12,
        tables=[table],
    )
    materialize_table(document, table, 3)
    assert table.rows == [
        ["Left", "Right"],
        ["First left left continuation", "First right right continuation"],
        ["Second-left", "Second right"],
    ]
