from pathlib import Path

import pdfplumber
from PIL import Image

from pdfcracker.images import _classify_image, extract_embedded_images
from pdfcracker.markdown import render_markdown
from pdfcracker.models import BBox, Block, BlockType, Document, Line, Page


def test_extracts_embedded_object_without_raster_fallback(tmp_path) -> None:
    pdf_path = tmp_path / "embedded.pdf"
    Image.new("RGB", (240, 120), "navy").save(pdf_path, "PDF")
    with pdfplumber.open(pdf_path) as pdf:
        width = float(pdf.pages[0].width)
        height = float(pdf.pages[0].height)
    document = Document(
        source_path=str(pdf_path),
        source_sha256="a" * 64,
        source_page_count=1,
        selected_pages=[1],
        pages=[Page(number=1, source_index=0, width=width, height=height)],
        body_font_size=12,
    )

    images = extract_embedded_images(pdf_path, document, tmp_path / "assets" / "images")

    assert len(images) == 1
    assert Path(tmp_path, images[0].asset_path).is_file()
    assert document.metadata["image_extraction"]["policy"] == (
        "embedded_objects_only_no_raster_fallback"
    )


def test_markdown_inserts_content_image_before_anchor() -> None:
    bbox = BBox(x0=0, y0=20, x1=100, y1=40)
    block = Block(
        id="p0001-b0001",
        page=1,
        source_block_index=0,
        bbox=bbox,
        bbox_norm=BBox(x0=0, y0=0.2, x1=1, y1=0.4),
        raw_text="Body",
        text="Body",
        lines=[Line(text="Body", bbox=bbox)],
        type=BlockType.PARAGRAPH,
        confidence=0.9,
    )
    document = Document(
        source_path="sample.pdf",
        source_sha256="a" * 64,
        source_page_count=1,
        selected_pages=[1],
        pages=[Page(number=1, source_index=0, width=100, height=100, blocks=[block])],
        body_font_size=12,
        images=[
            {
                "id": "p0001-img001",
                "page": 1,
                "object_name": "Image1",
                "asset_path": "assets/images/example.png",
                "sha256": "b" * 64,
                "bbox": {"x0": 0, "y0": 0, "x1": 100, "y1": 20},
                "bbox_norm": {"x0": 0, "y0": 0, "x1": 1, "y1": 0.2},
                "pixel_width": 100,
                "pixel_height": 20,
                "format": "png",
                "role": "content",
                "include_in_markdown": True,
                "confidence": 0.9,
                "anchor_before_block_id": block.id,
            }
        ],
    )

    markdown = render_markdown(document)
    assert markdown.startswith("![Embedded image](assets/images/example.png)\n\nBody")


def test_translucent_low_information_layer_is_decorative() -> None:
    page = Page(number=1, source_index=0, width=100, height=100)
    bbox = BBox(x0=10, y0=10, x1=80, y1=80)
    shadow = Image.new("RGBA", (80, 80), (20, 20, 20, 120))
    role, include, confidence, evidence = _classify_image(
        {}, bbox, page, 80, 80, shadow
    )
    assert role == "decorative"
    assert include is False
    assert confidence >= 0.9
    assert "translucent_low_information_layer" in evidence
