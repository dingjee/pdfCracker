from pdfcracker.models import BBox, Block, BlockType, Document, Line, Page
from pdfcracker.risk import build_risk_plan


def test_risk_plan_includes_cluster_representative_and_table() -> None:
    bbox = BBox(x0=0, y0=0, x1=100, y1=20)
    block = Block(
        id="p0001-b0001",
        page=1,
        source_block_index=0,
        bbox=bbox,
        bbox_norm=BBox(x0=0, y0=0, x1=1, y1=0.2),
        raw_text="Heading",
        text="Heading",
        lines=[Line(text="Heading", bbox=bbox)],
        type=BlockType.HEADING,
        confidence=0.9,
        heading_level=2,
    )
    document = Document(
        source_path="sample.pdf",
        source_sha256="a" * 64,
        source_page_count=1,
        selected_pages=[1],
        pages=[Page(number=1, source_index=0, width=100, height=100, blocks=[block])],
        body_font_size=12,
    )
    plan = build_risk_plan(document, {1})
    assert plan["summary"]["cluster_count"] == 1
    assert plan["visual_sample"][0]["page"] == 1
    assert "ruled_table_candidate" in plan["visual_sample"][0]["reasons"]
